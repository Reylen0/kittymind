"""SQLite 版本链迁移执行器。

背景：旧实现（session/store.py 的 _migrate）把每个步骤都用
`contextlib.suppress(Exception)` 包着，然后**无条件**执行
`PRAGMA user_version=N`。任何一步真的失败（FTS5 未编译进本机 SQLite、磁盘
只读、中途断电、语句写错）都会被静默吞掉，而库已经被标成"最新版本"，下次
启动不会再重试 —— 半截 schema 永久固化，直到某天某个功能读表时报错，那时
现场已经离事故原因很远。

本模块把迁移变成可审计的版本链：

  - 每个步骤显式声明目标版本与名称，只对 `version > 当前版本` 的步骤执行；
  - `schema_migrations` 表落盘"哪些步骤真的成功过"，作为审计依据；
  - **只有步骤成功返回，才写记录并提升 `user_version`**；失败立即抛出，
    `user_version` 停在上一个成功的版本，下次启动自然重试；
  - 库的版本高于本代码已知的版本时拒绝继续（用户降级运行旧代码），
    否则旧代码会按旧 schema 去写新库，损坏数据。

已知局限（接受）：SQLite 的事务化 DDL 只在不开 `executescript` 时成立，而
"建 FTS 表 + 触发器"这类步骤必须走脚本（触发器体里含分号，拆不开）。
因此**迁移步骤必须幂等**：版本号是唯一真相来源，重复执行不得产生副作用。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

MIGRATIONS_TABLE = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    """一个迁移步骤：把库从 version-1（或更早）带到 version。

    `apply` 必须幂等——见模块 docstring 的"已知局限"。
    """

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass
class MigrationOutcome:
    """一次迁移的执行结果（供日志与测试断言）。"""

    from_version: int
    to_version: int
    applied: list[Migration] = field(default_factory=list)
    baselined: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def describe(self) -> str:
        if not self.changed:
            return f"schema v{self.to_version}（无需迁移）"
        steps = " → ".join(f"v{m.version} {m.name}" for m in self.applied)
        return f"schema v{self.from_version} → v{self.to_version}：{steps}"


class SchemaTooNewError(RuntimeError):
    """库的 schema 版本高于本代码已知版本（多半是用旧版本代码跑了新库）。"""

    def __init__(self, db_version: int, known_version: int) -> None:
        super().__init__(
            f"数据库 schema 版本 {db_version} 高于本代码已知的 {known_version}，"
            "拒绝继续写入以避免损坏数据。请升级程序，或改用新的库文件。"
        )
        self.db_version = db_version
        self.known_version = known_version


class MigrationFailedError(RuntimeError):
    """某个迁移步骤执行失败。user_version 未被提升，下次启动会重试。"""

    def __init__(self, migration: Migration, cause: Exception) -> None:
        super().__init__(
            f"迁移步骤 v{migration.version} {migration.name} 执行失败：{cause}"
        )
        self.migration = migration
        self.cause = cause


def user_version(conn: sqlite3.Connection) -> int:
    """读当前 schema 版本（PRAGMA user_version）。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def applied_versions(
    conn: sqlite3.Connection, table: str = MIGRATIONS_TABLE
) -> dict[int, str]:
    """已成功应用的步骤（version → applied_at）。表不存在时返回空。"""
    try:
        rows = conn.execute(f"SELECT version, applied_at FROM {table}").fetchall()
    except sqlite3.Error:
        return {}
    return {int(r[0]): str(r[1]) for r in rows}


@contextlib.contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """用 SAVEPOINT 而不是 BEGIN：连接可能是 autocommit（isolation_level=None），
    也可能处于隐式事务中，SAVEPOINT 在两种情况下都成立。"""
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"ROLLBACK TO {name}")
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def _ensure_ledger(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        " version    INTEGER PRIMARY KEY,"
        " name       TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )


def _write_baseline(conn: sqlite3.Connection, table: str, version: int) -> bool:
    """接管旧库时补一条基线记录。

    旧库可能已经有 user_version=7 但没有本表（迁移链是后来才引入的）。补一条
    version 等于当前值的基线记录，看表就知道"这个库在接管前就已经是 vN 了"，
    从而能区分"没跑过任何步骤"和"跑过但记录被清空"。
    """
    if version <= 0:
        return False
    with _savepoint(conn, "migrate_baseline"):
        conn.execute(
            f"INSERT OR IGNORE INTO {table}(version, name, applied_at) VALUES (?, ?, ?)",
            (version, "baseline（迁移链接管前的版本）", _now()),
        )
    return True


def _record(conn: sqlite3.Connection, table: str, migration: Migration) -> None:
    """把"这一步成功了"这件事原子地写进记录表并提升 user_version。"""
    with _savepoint(conn, "migrate_record"):
        conn.execute(
            f"INSERT OR REPLACE INTO {table}(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, _now()),
        )
        # PRAGMA 不是普通语句，但 user_version 写在 DB header 上并随事务提交，
        # 与记录表同生共死——要么都生效，要么都不生效。
        conn.execute(f"PRAGMA user_version={int(migration.version)}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    table: str = MIGRATIONS_TABLE,
    allow_future: bool = False,
) -> MigrationOutcome:
    """把库按版本链补齐到最新。返回执行结果；失败抛 MigrationFailedError。

    `allow_future=True` 时不检查"库版本高于代码版本"（只读场景可用）。
    """
    ordered = sorted(migrations, key=lambda m: m.version)
    start = user_version(conn)
    if not ordered:
        return MigrationOutcome(start, start)

    latest = ordered[-1].version
    if start > latest and not allow_future:
        raise SchemaTooNewError(start, latest)

    _ensure_ledger(conn, table)
    baselined = _write_baseline(conn, table, start)

    current = start
    applied: list[Migration] = []
    for migration in ordered:
        if migration.version <= current:
            continue
        try:
            migration.apply(conn)
        except Exception as e:  # 原样包装后抛出，保留原因链
            raise MigrationFailedError(migration, e) from e
        _record(conn, table, migration)
        current = migration.version
        applied.append(migration)

    return MigrationOutcome(start, current, applied, baselined)
