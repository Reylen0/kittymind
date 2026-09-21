"""Phase 17 数据维护与健壮性测试。

覆盖三块：
  1. 版本链迁移执行器（storage/migrations.py）：逐级应用、跳号、**失败不提升
     版本**、库版本高于代码时拒绝写入、记录表与基线；
  2. 维护工具（storage/maintenance.py）：自检、在线备份、滚动清理、WAL 截断、
     VACUUM 阈值判定、损坏库隔离；
  3. 会话库 / 记忆库 / 审计库把这两块接进生命周期后的端到端行为。
"""

import json
import os as _os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kittymind.session import store as session_store
from kittymind.session.store import SqliteSessionStore
from kittymind.storage import (
    DbStats,
    Migration,
    MigrationFailedError,
    SchemaTooNewError,
    applied_versions,
    backup_name,
    backup_to,
    checkpoint,
    latest_backup_age_hours,
    list_backups,
    prune_backups,
    quarantine_db,
    quick_check,
    read_stats,
    run_migrations,
    should_vacuum,
    user_version,
    vacuum,
)


def _mem_conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def _file_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class _Recorder:
    """迁移步骤的测试替身：记录调用顺序，可选地抛出异常。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def step(self, version: int, *, fail: bool = False, ddl: str | None = None):
        def _apply(conn: sqlite3.Connection) -> None:
            self.calls.append(version)
            if ddl:
                conn.execute(ddl)
            if fail:
                raise sqlite3.OperationalError(f"步骤 v{version} 故意失败")
        return Migration(version, f"step{version}", _apply)


# ── 版本链迁移执行器 ──────────────────────────────────────────────

def test_migrations_apply_in_order_and_are_recorded():
    conn = _mem_conn()
    rec = _Recorder()
    outcome = run_migrations(conn, [rec.step(1), rec.step(2), rec.step(3)])

    assert rec.calls == [1, 2, 3]
    assert outcome.from_version == 0
    assert outcome.to_version == 3
    assert [m.version for m in outcome.applied] == [1, 2, 3]
    assert user_version(conn) == 3
    assert sorted(applied_versions(conn)) == [1, 2, 3]


def test_migrations_skip_already_applied():
    """重开库时不重复执行（幂等入口，不必靠步骤自身幂等兜底）。"""
    conn = _mem_conn()
    run_migrations(conn, [_Recorder().step(1), _Recorder().step(2)])

    rec = _Recorder()
    outcome = run_migrations(conn, [rec.step(1), rec.step(2)])
    assert rec.calls == []
    assert outcome.changed is False
    assert outcome.to_version == 2


def test_migrations_allow_gapped_versions():
    """版本号允许跳号：历史语义固定在某个号上，不为连续而插空步骤。"""
    conn = _mem_conn()
    rec = _Recorder()
    outcome = run_migrations(conn, [rec.step(3), rec.step(5), rec.step(7)])
    assert rec.calls == [3, 5, 7]
    assert outcome.to_version == 7
    assert user_version(conn) == 7


def test_failed_migration_does_not_advance_version():
    """核心语义：步骤失败 → 不写记录、不提升 user_version → 下次启动重试。

    旧实现是 suppress 掉异常后无条件标成最新版本，半截 schema 就此永久固化。
    """
    conn = _mem_conn()
    rec = _Recorder()
    migrations = [rec.step(1), rec.step(2, fail=True), rec.step(3)]

    with pytest.raises(MigrationFailedError) as ei:
        run_migrations(conn, migrations)

    assert ei.value.migration.version == 2
    assert user_version(conn) == 1, "失败后版本必须停在最后一个成功的步骤"
    assert sorted(applied_versions(conn)) == [1], "失败步骤不得写入记录表"
    assert rec.calls == [1, 2], "失败之后不应继续执行后续步骤"


def test_retry_after_failure_resumes_from_last_good_version():
    """修好问题后重跑：只补做没成功的步骤。"""
    conn = _mem_conn()
    failing = _Recorder()
    with pytest.raises(MigrationFailedError):
        run_migrations(conn, [failing.step(1), failing.step(2, fail=True)])

    rec = _Recorder()
    run_migrations(conn, [rec.step(1), rec.step(2), rec.step(3)])
    assert rec.calls == [2, 3]


def test_schema_newer_than_code_is_rejected():
    """库比代码新 → 拒绝写入（旧代码会按旧 schema 写新库，损坏数据）。"""
    conn = _mem_conn()
    conn.execute("PRAGMA user_version=99")
    with pytest.raises(SchemaTooNewError) as ei:
        run_migrations(conn, [_Recorder().step(1)])
    assert ei.value.db_version == 99
    assert user_version(conn) == 99


def test_allow_future_permits_read_only_open():
    conn = _mem_conn()
    conn.execute("PRAGMA user_version=99")
    outcome = run_migrations(conn, [_Recorder().step(1)], allow_future=True)
    assert outcome.changed is False


def test_baseline_recorded_when_taking_over_legacy_db():
    """接管老库（有 user_version 没有记录表）时写一条基线，便于审计区分。"""
    conn = _mem_conn()
    conn.execute("PRAGMA user_version=5")
    outcome = run_migrations(conn, [_Recorder().step(7)])
    assert outcome.baselined is True
    assert 5 in applied_versions(conn)
    # 基线与真实步骤都记录在案，且基线不冒充迁移步骤（版本 7 是新跑的）
    assert user_version(conn) == 7


def test_migration_ddl_is_applied():
    conn = _mem_conn()
    rec = _Recorder()
    run_migrations(conn, [rec.step(1, ddl="CREATE TABLE t1(x INTEGER)")])
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t1'").fetchone()


# ── 维护工具 ──────────────────────────────────────────────────────

def test_quick_check_passes_on_healthy_db(tmp_path):
    db = tmp_path / "ok.db"
    conn = _file_conn(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    assert quick_check(conn) == "ok"
    conn.close()


def test_backup_copies_rows(tmp_path):
    src = tmp_path / "src.db"
    conn = _file_conn(src)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    dest = tmp_path / "copies" / "b.db"
    backup_to(conn, dest)
    conn.close()

    assert dest.is_file()
    check = sqlite3.connect(str(dest))
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    check.close()


def test_backup_includes_uncheckpointed_wal_data(tmp_path):
    """WAL 里尚未 checkpoint 的数据也要进备份（直接拷文件会漏）。"""
    src = tmp_path / "src.db"
    conn = _file_conn(src)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    assert Path(str(src) + "-wal").exists()
    dest = tmp_path / "b.db"
    backup_to(conn, dest)
    conn.close()

    check = sqlite3.connect(str(dest))
    assert check.execute("SELECT x FROM t").fetchone()[0] == 42
    check.close()


def test_prune_backups_keeps_newest(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    made = []
    for i in range(5):
        p = d / backup_name(Path("sessions.db"), f"2026010{i + 1}-000000")
        p.write_text("x", encoding="utf-8")
        # 修改时间决定"新"的顺序
        _os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        made.append(p)

    removed = prune_backups(d, 2)
    left = list_backups(d)
    assert len(left) == 2
    assert len(removed) == 3
    assert {p.name for p in left} == {made[4].name, made[3].name}


def test_prune_backups_ignores_foreign_files(tmp_path):
    """只清理自己产出的命名，别误删用户放在同目录的东西。"""
    d = tmp_path / "backups"
    d.mkdir()
    foreign = d / "important-notes.txt"
    foreign.write_text("keep me", encoding="utf-8")
    (d / backup_name(Path("sessions.db"))).write_text("x", encoding="utf-8")

    prune_backups(d, 0)  # keep=0 表示不清理
    assert foreign.exists()
    prune_backups(d, 1)
    assert foreign.exists()


def test_prune_backups_scopes_to_one_db(tmp_path):
    """滚动清理按库隔离：两个库共用一个备份目录时不能互相删。"""
    d = tmp_path / "backups"
    d.mkdir()
    for i in range(4):
        (d / backup_name(Path("sessions.db"), f"2026010{i + 1}-000000")).write_text("a", encoding="utf-8")
        (d / backup_name(Path("tool_audit.db"), f"2026010{i + 1}-000000")).write_text("b", encoding="utf-8")

    prune_backups(d, 1, stem="sessions")
    assert len(list_backups(d, "sessions")) == 1
    assert len(list_backups(d, "tool_audit")) == 4, "清理另一个库时不能连坐"


def test_latest_backup_age_none_when_no_backups(tmp_path):
    assert latest_backup_age_hours(tmp_path / "nope") is None


def test_checkpoint_truncates_wal(tmp_path):
    """TRUNCATE checkpoint 要把 WAL 文件截回 0——这是"WAL 从不缩小"的正解。"""
    db = tmp_path / "w.db"
    conn = _file_conn(db)
    conn.execute("CREATE TABLE t(x TEXT)")
    for _ in range(400):
        conn.execute("INSERT INTO t VALUES (?)", ("y" * 200,))
    assert conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone() is not None

    busy, _log_frames, _done = checkpoint(conn, "TRUNCATE")
    assert busy == 0
    assert Path(str(db) + "-wal").stat().st_size == 0
    conn.close()


def test_should_vacuum_respects_ratio_and_size():
    high = DbStats(page_count=100, page_size=4096, freelist_count=60,
                   size_bytes=10_000_000, wal_bytes=0)
    assert high.freelist_ratio == 0.6
    assert should_vacuum(high, min_ratio=0.4, min_bytes=1_000_000) is True
    # 库太小 → 不值得重写
    assert should_vacuum(high, min_ratio=0.4, min_bytes=100_000_000) is False
    # 空闲页不多 → 没必要
    low = DbStats(page_count=100, page_size=4096, freelist_count=5,
                  size_bytes=10_000_000, wal_bytes=0)
    assert should_vacuum(low, min_ratio=0.4, min_bytes=1_000_000) is False
    # 空库不炸
    empty = DbStats(0, 0, 0, 0, 0)
    assert should_vacuum(empty, min_ratio=0.4, min_bytes=0) is False


def test_vacuum_reclaims_freelist(tmp_path):
    """删数据不会自动回收文件空间，VACUUM 之后空闲页要归零。"""
    db = tmp_path / "v.db"
    conn = _file_conn(db)
    conn.execute("CREATE TABLE t(x TEXT)")
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO t VALUES (?)", [("z" * 1000,) for _ in range(3000)])
    conn.execute("COMMIT")
    conn.execute("DELETE FROM t")

    before = read_stats(conn, db)
    assert before.freelist_count > 0
    assert vacuum(conn) is True
    after = read_stats(conn, db)
    assert after.freelist_count == 0
    conn.close()


def test_quarantine_db_moves_db_with_sidecars(tmp_path):
    db = tmp_path / "sessions.db"
    conn = _file_conn(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()

    target = quarantine_db(db, "20260101-000000")
    assert not db.exists()
    assert target.exists()
    # -wal / -shm 一并改名，否则新库启动时会把旧日志重放进来
    for suffix in ("-wal", "-shm"):
        stray = Path(str(db) + suffix)
        assert not stray.exists()


def test_read_stats_reports_sizes(tmp_path):
    db = tmp_path / "s.db"
    conn = _file_conn(db)
    conn.execute("CREATE TABLE t(x TEXT)")
    conn.execute("INSERT INTO t VALUES ('abc')")
    stats = read_stats(conn, db)
    assert stats.page_count > 0
    assert stats.page_size > 0
    assert stats.size_bytes > 0
    conn.close()


# ── 会话库接线 ────────────────────────────────────────────────────

def test_store_writes_version_and_ledger(tmp_path):
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db)
    assert user_version(store._conn) == session_store._SCHEMA_VERSION
    ledger = applied_versions(store._conn)
    assert ledger, "迁移过程必须留下记录，否则无从审计"
    store.close()


def test_migration_chain_matches_schema_constant():
    """_SCHEMA_VERSION 与迁移链最大版本必须一致——改一边漏一边会静默漂移。"""
    store = SqliteSessionStore.__new__(SqliteSessionStore)
    highest = max(m.version for m in store._migrations())
    assert highest == session_store._SCHEMA_VERSION


def test_store_backup_created_next_to_db(tmp_path):
    """备份落在库同级 backups/ 下，内容是"上次运行结束"的完整快照。"""
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db)
    # 空库不备份：第一份快照要等到库里有内容
    store.close()
    assert list_backups(tmp_path / "backups", "s") == []

    store = SqliteSessionStore(db)
    store.write_header("s1", {"title": "t", "created_at": "2026-01-01T00:00:00+00:00"})
    store.close()

    store = SqliteSessionStore(db)          # 重开 → 距上次备份已过期 → 备份
    store.close()
    backups = list_backups(tmp_path / "backups", "s")
    assert len(backups) == 1
    conn = sqlite3.connect(str(backups[0]))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()


def test_store_backup_interval_skips_recent_backup(tmp_path):
    """24h 内不重复备份（否则每次启动都写一份，备份目录很快堆满）。"""
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db)
    store.write_header("s1", {"title": "t", "created_at": "2026-01-01T00:00:00+00:00"})
    store.close()

    # 库里有内容 + 已有备份很新 → 第二次打开不再备份
    SqliteSessionStore(db).close()
    assert len(list_backups(tmp_path / "backups", "s")) == 1


def test_maintenance_can_be_disabled(tmp_path):
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db, maintenance=False)
    store.close()
    assert not (tmp_path / "backups").exists()


def test_corrupt_db_is_quarantined_and_rebuilt(tmp_path):
    """库损坏 → 隔离留档（不删）+ 以空库继续启动。"""
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db)
    store.write_header("s1", {"title": "t", "created_at": "2026-01-01T00:00:00+00:00"})
    store.append("s1", {"seq": 0, "role": "user", "content": "hello"})
    store.close()

    # 破坏 SQLite 文件头（模拟磁盘损坏）
    raw = bytearray(db.read_bytes())
    raw[:16] = b"\x00" * 16
    db.write_bytes(bytes(raw))

    store2 = SqliteSessionStore(db)
    try:
        # 新库可用
        store2.write_header("s2", {"title": "new", "created_at": "2026-01-01T00:00:00+00:00"})
        assert [h["id"] for h in store2.list_headers()] == ["s2"]
    finally:
        store2.close()

    quarantined = list(tmp_path.glob("s.db.corrupt-*"))
    assert quarantined, "损坏的库必须被隔离留档，绝不能删掉"
    # 隔离文件是坏的（本来就坏），但至少留在了磁盘上供手工抢救
    assert quarantined[0].stat().st_size > 0


def test_orphan_messages_are_purged_on_every_open(tmp_path):
    """孤儿行清理不属于版本链：旧库升上来之后新产生的孤儿也要清掉。"""
    db = tmp_path / "o.db"
    SqliteSessionStore(db).close()          # 先建好 v7 库
    raw = sqlite3.connect(str(db))
    raw.execute(
        "INSERT INTO messages(session_id, seq, role, content, ts)"
        " VALUES ('ghost', 0, 'user', 'orphan', 0)"
    )
    raw.commit()
    raw.close()

    store = SqliteSessionStore(db)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='ghost'"
    ).fetchone()[0] == 0
    store.close()


def test_manual_compact_reclaims_space(tmp_path):
    """小库自动维护会跳过 VACUUM（不值得重写），但手动入口要能立刻回收。"""
    db = tmp_path / "c.db"
    store = SqliteSessionStore(db)
    store.write_header("s1", {"title": "t", "created_at": "2026-01-01T00:00:00+00:00"})
    store.append("s1", {"seq": 0, "role": "user", "content": "x" * 5000})
    store._conn.execute(
        "UPDATE messages SET content=NULL WHERE session_id='s1'"
    )
    before = store.stats()
    assert before["freelist_count"] > 0

    assert store.compact() is True
    after = store.stats()
    assert after["freelist_count"] == 0
    assert after["wal_bytes"] == 0, "手动回收顺带把 WAL 截回 0"
    store.close()


def test_store_stats_reports_physical_usage(tmp_path):
    db = tmp_path / "st.db"
    store = SqliteSessionStore(db)
    stats = store.stats()
    assert stats["path"].endswith("st.db")
    assert stats["page_count"] > 0
    assert stats["freelist_ratio"] >= 0.0
    store.close()


def test_store_close_is_idempotent(tmp_path):
    store = SqliteSessionStore(tmp_path / "s.db")
    store.close()
    store.close()
    assert store._conn is None


# ── 工具审计库接线 ────────────────────────────────────────────────

def test_audit_db_is_versioned(tmp_path):
    """审计库此前 user_version 一直是 0：建表裸跑，将来改表没有迁移入口。"""
    from kittymind.tools import audit as audit_mod

    log = audit_mod.ToolAuditLog(tmp_path / "a.db", tmp_path / "a.jsonl")
    try:
        assert user_version(log._db) == audit_mod._SCHEMA_VERSION
        assert applied_versions(log._db), "审计库也要留下迁移记录"
        log.record(session_id="s", tool="t", args="{}", decision="allow",
                   reason="", failed=False, duration_ms=1)
        assert log._db.execute("SELECT COUNT(*) FROM tool_audit").fetchone()[0] == 1
    finally:
        log.close()


def test_audit_db_migrates_legacy_unversioned_db(tmp_path):
    """老审计库（无 user_version 无记录表）打开后要被接管并标记版本。"""
    from kittymind.tools import audit as audit_mod

    db = tmp_path / "a.db"
    raw = sqlite3.connect(str(db))
    raw.execute(
        "CREATE TABLE tool_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
        " session_id TEXT, tool TEXT NOT NULL, args TEXT, decision TEXT NOT NULL,"
        " reason TEXT, failed INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0)"
    )
    # ts 必须是「保留期内」的：打开审计库会顺带裁剪过期行，写死一个老日期会让
    # 这一行在迁移之前就被删掉，测试就验不到「迁移不动数据」这件事了。
    raw.execute(
        "INSERT INTO tool_audit(ts, tool, decision) VALUES (?, 't', 'allow')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    raw.commit()
    raw.close()

    log = audit_mod.ToolAuditLog(db, tmp_path / "a.jsonl")
    try:
        assert user_version(log._db) == 1
        # 已有行必须保留（迁移只加结构，不动数据）
        assert log._db.execute("SELECT COUNT(*) FROM tool_audit").fetchone()[0] == 1
        # 索引也补上了
        idx = {r[0] for r in log._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "idx_audit_ts" in idx
    finally:
        log.close()


def test_audit_db_corrupt_is_quarantined(tmp_path):
    """审计库坏了要隔离留档 + 继续可用（旁路数据，不能拖垮 agent）。"""
    from kittymind.tools import audit as audit_mod

    db = tmp_path / "a.db"
    log = audit_mod.ToolAuditLog(db, tmp_path / "a.jsonl")
    log.close()
    db.write_bytes(b"\x00" * 64)

    log = audit_mod.ToolAuditLog(db, tmp_path / "a.jsonl")
    try:
        log.record(session_id="s", tool="t", args="{}", decision="allow",
                   reason="", failed=False, duration_ms=1)
        assert log._db.execute("SELECT COUNT(*) FROM tool_audit").fetchone()[0] == 1
    finally:
        log.close()
    assert list(tmp_path.glob("a.db.corrupt-*"))


# ── 行数保留期（Phase 17 追加：两张表都只增不删）──────────────────

def _age_usage_rows(store, days: int, session_id: str) -> None:
    """把某个会话的用量行改成 days 天前（造过期数据用）。"""
    old = int(time.time()) - days * 86400
    with store._lock:
        store._conn.execute(
            "UPDATE model_usage SET ts=? WHERE session_id=?", (old, session_id)
        )


def test_usage_prune_removes_only_expired(tmp_path):
    store = SqliteSessionStore(tmp_path / "s.db", maintenance=False)
    store.record_usage("old", "m1", 100, 10)
    store.record_usage("new", "m1", 200, 20)
    _age_usage_rows(store, 200, "old")

    assert store.prune_usage(90) == 1
    keys = {g["key"] for g in store.aggregate_usage("session")}
    assert keys == {"new"}
    store.close()


def test_usage_prune_disabled_when_days_non_positive(tmp_path):
    """days<=0 = 永久保留，一行都不许删。"""
    store = SqliteSessionStore(tmp_path / "s.db", maintenance=False)
    store.record_usage("s1", "m1", 10, 5)
    _age_usage_rows(store, 3650, "s1")   # 十年前的数据

    assert store.prune_usage(0) == 0
    assert store.prune_usage(-7) == 0
    assert store.aggregate_usage("session")   # 还在
    store.close()


def test_usage_prune_runs_on_startup(tmp_path):
    """裁剪挂在启动维护里，不依赖"备份是否到期"。"""
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db, maintenance=False)
    store.record_usage("old", "m1", 100, 10)
    store.record_usage("new", "m1", 100, 10)
    _age_usage_rows(store, 200, "old")
    store.close()

    reopened = SqliteSessionStore(db)
    try:
        keys = {g["key"] for g in reopened.aggregate_usage("session")}
        assert keys == {"new"}, "启动时应裁掉超过 USAGE_RETENTION_DAYS 的行"
    finally:
        reopened.close()


def test_usage_prune_skipped_when_maintenance_off(tmp_path):
    """维护总开关关掉时不做任何自动裁剪（用户显式要求"别动我的数据"）。"""
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db, maintenance=False)
    store.record_usage("old", "m1", 100, 10)
    _age_usage_rows(store, 200, "old")
    store.close()

    reopened = SqliteSessionStore(db, maintenance=False)
    try:
        assert {g["key"] for g in reopened.aggregate_usage("session")} == {"old"}
    finally:
        reopened.close()


def test_audit_prune_removes_expired_from_db_and_jsonl(tmp_path):
    """两侧必须一起裁：只删库不删日志会变成「文件里翻得到、库里查不到」。"""
    from kittymind.tools import audit as audit_mod

    db = tmp_path / "a.db"
    jsonl = tmp_path / "a.jsonl"
    log = audit_mod.ToolAuditLog(db, jsonl)
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        with log._lock:
            log._db.execute(
                "INSERT INTO tool_audit(ts, tool, decision) VALUES (?, 'old', 'allow')", (old,)
            )
            log._db.execute(
                "INSERT INTO tool_audit(ts, tool, decision) VALUES (?, 'new', 'allow')", (new,)
            )
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old, "tool": "old"}) + "\n")
            f.write(json.dumps({"ts": new, "tool": "new"}) + "\n")

        assert log.prune(30) == 1
        assert [r[0] for r in log._db.execute("SELECT tool FROM tool_audit")] == ["new"]
        kept = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines()]
        assert [x["tool"] for x in kept] == ["new"]
    finally:
        log.close()


def test_audit_prune_keeps_unparsable_lines(tmp_path):
    """解析不出 ts 的行保留：宁多留一行，也不因格式意外删掉证据。"""
    from kittymind.tools import audit as audit_mod

    jsonl = tmp_path / "a.jsonl"
    log = audit_mod.ToolAuditLog(tmp_path / "a.db", jsonl)
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        jsonl.write_text(
            json.dumps({"ts": old, "tool": "old"}) + "\n"
            + "这不是 JSON\n"
            + json.dumps({"tool": "no-ts"}) + "\n"
            + json.dumps({"ts": new, "tool": "new"}) + "\n",
            encoding="utf-8",
        )
        log.prune(30)
        lines = jsonl.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3       # 只删掉 old，两条"读不懂"的留下
        assert "old" not in jsonl.read_text(encoding="utf-8")
    finally:
        log.close()


def test_audit_prune_handles_missing_microseconds(tmp_path):
    """isoformat 在微秒为 0 时会省略小数部分——这种行也必须能判为过期。

    库侧是字符串比较（写入格式由 record() 恒定保证，见 prune 的说明），JSONL
    侧是解析成 datetime 后比较。这里钉住"老格式的行两个方向都删得掉"，
    否则它会永远留在库里，保留期形同虚设。
    """
    from kittymind.tools import audit as audit_mod

    jsonl = tmp_path / "a.jsonl"
    log = audit_mod.ToolAuditLog(tmp_path / "a.db", jsonl)
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=90)).replace(
            microsecond=0
        ).isoformat()
        assert "." not in old, "构造前提：这条 ts 不带微秒"
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old, "tool": "old"}) + "\n")
        with log._lock:
            log._db.execute(
                "INSERT INTO tool_audit(ts, tool, decision) VALUES (?, 'old', 'allow')", (old,)
            )

        log.prune(30)
        assert log._db.execute("SELECT COUNT(*) FROM tool_audit").fetchone()[0] == 0
        assert jsonl.read_text(encoding="utf-8").strip() == ""
    finally:
        log.close()



