"""SQLite 维护工具：自检、备份、WAL checkpoint、空间回收。

这些都是"数据库生命周期的边角活"，单独放一层，供会话库（sessions.db）与
工具审计库（tool_audit.db）共用。

设计取舍：
  - **自检用 `quick_check` 而不是 `integrity_check`**：quick_check 跳过索引与
    唯一性校验（不做 O(n²) 的重复检查），对正常库是 O(页数) 的顺序扫描，
    启动时跑得起；integrity_check 更彻底但慢得多，交给显式的深度检查入口。
  - **备份用 Python 的 `Connection.backup()`（SQLite Online Backup API），
    而不是复制文件、也不是 `VACUUM INTO`**：WAL 模式下直接拷主库文件会漏掉
    还没 checkpoint 的已提交数据；`VACUUM INTO` 需要独占且要求目标不存在。
    backup() 能把 WAL 内容一并带上，也不需要独占。
  - **损坏时只隔离、不删除**：把库文件（连同 -wal/-shm）改名留档，用户随时
    可以拿去做取证或手工抢救。程序永不删用户数据。
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# 备份文件命名：`<库名>-YYYYmmdd-HHMMSS.db`。`list_backups` 只认这个形状，
# 绝不误删用户放在同目录的其它东西。
BACKUP_SUFFIX = ".db"

_QUARANTINE_MARK = ".corrupt-"


@dataclass(frozen=True)
class DbStats:
    """库的物理占用情况。"""

    page_count: int
    page_size: int
    freelist_count: int
    size_bytes: int
    wal_bytes: int

    @property
    def freelist_ratio(self) -> float:
        """空闲页占比。删过大量数据后这个值会很高，是 VACUUM 的信号。"""
        if self.page_count <= 0:
            return 0.0
        return self.freelist_count / self.page_count


def read_stats(conn: sqlite3.Connection, db_path: Path | None = None) -> DbStats:
    """读取页统计与文件大小（wal 大小取自同名 `-wal` 文件）。"""

    def _pragma_int(name: str) -> int:
        try:
            row = conn.execute(f"PRAGMA {name}").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    page_count = _pragma_int("page_count")
    page_size = _pragma_int("page_size")
    wal_bytes = 0
    size_bytes = 0
    if db_path is not None:
        path = Path(db_path)
        with contextlib.suppress(OSError):
            size_bytes = path.stat().st_size
        with contextlib.suppress(OSError):
            wal_bytes = Path(str(path) + "-wal").stat().st_size
    return DbStats(
        page_count=page_count,
        page_size=page_size,
        freelist_count=_pragma_int("freelist_count"),
        size_bytes=size_bytes,
        wal_bytes=wal_bytes,
    )


def quick_check(conn: sqlite3.Connection) -> str:
    """快速自检，返回 "ok" 或第一段错误描述。任何异常按不可用处理。"""
    try:
        rows = conn.execute("PRAGMA quick_check(1)").fetchall()
    except sqlite3.Error as e:
        return f"无法执行自检：{e}"
    if not rows:
        return "自检无输出（库可能不可读）"
    return str(rows[0][0])


def integrity_check(conn: sqlite3.Connection, limit: int = 20) -> list[str]:
    """深度自检。返回问题列表，空列表表示通过。"""
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as e:
        return [f"无法执行深度自检：{e}"]
    messages = [str(r[0]) for r in rows]
    if messages == ["ok"]:
        return []
    return messages[:limit]


def checkpoint(conn: sqlite3.Connection, mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """执行 `PRAGMA wal_checkpoint`，返回 (busy, log_frames, checkpointed)。

    `TRUNCATE` 会把 WAL 截回 0 字节——只在启动早期（还没有并发读写）用；
    运行期用 `PASSIVE`（不阻塞任何读者/写者，能搬多少搬多少）。
    """
    mode = mode.upper()
    if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        mode = "PASSIVE"
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    except sqlite3.Error:
        return (-1, -1, -1)
    if not row:
        return (-1, -1, -1)
    return (int(row[0]), int(row[1]), int(row[2]))


def backup_to(conn: sqlite3.Connection, dest: Path) -> Path:
    """把连接里的库备份到 dest（覆盖同名旧文件）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def should_vacuum(stats: DbStats, *, min_ratio: float = 0.4, min_bytes: int = 0) -> bool:
    """是否值得做 VACUUM。

    VACUUM 会重写整个库（磁盘峰值约 2×、期间占写锁），所以要求两个条件同时
    成立：空闲页占比够高，且库本身够大。小库做不做都无所谓，别白折腾。
    天然自带频率限制：VACUUM 之后 freelist 归零，条件自己就不成立了。
    """
    if stats.page_count <= 0:
        return False
    if stats.size_bytes < max(0, min_bytes):
        return False
    return stats.freelist_ratio >= max(0.0, min_ratio)


def vacuum(conn: sqlite3.Connection) -> bool:
    """重写库以回收空闲页。失败（例如当前有并发写）返回 False。"""
    try:
        conn.execute("VACUUM")
    except sqlite3.Error:
        return False
    return True


def quarantine_path(db_path: Path, stamp: str | None = None) -> Path:
    """给出损坏库的隔离目标路径（`sessions.db.corrupt-<ts>`）。"""
    db_path = Path(db_path)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    return db_path.with_name(db_path.name + _QUARANTINE_MARK + stamp)


def quarantine_db(db_path: Path, stamp: str | None = None) -> Path:
    """把库文件连同 `-wal` / `-shm` 一起改名隔离，返回新路径。

    只重命名，不删除——损坏的库是用户数据，可能还想抢救。
    """
    db_path = Path(db_path)
    target = quarantine_path(db_path, stamp)
    db_path.rename(target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            with contextlib.suppress(OSError):
                side.rename(Path(str(target) + suffix))
    return target


def backup_name(db_path: Path, stamp: str | None = None) -> str:
    """备份文件名：`<库名>-YYYYmmdd-HHMMSS.db`（库名来自被备份的库，不重复加前缀）。"""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    return f"{Path(db_path).stem}-{stamp}{BACKUP_SUFFIX}"


def list_backups(backup_dir: Path, stem: str = "sessions") -> list[Path]:
    """按修改时间从新到旧列出备份。

    `stem` 限定"备份的是哪个库"——只认 `<库名>-*.db`，既避免把两个库的备份
    混在一起滚动清理，也不会误删用户放在同目录的其它文件。
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    candidates = [
        p for p in backup_dir.glob(f"{stem}-*{BACKUP_SUFFIX}")
        if p.is_file()
    ]
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_backup_age_hours(backup_dir: Path, stem: str = "sessions") -> float | None:
    """最新备份距今多少小时；没有备份返回 None。"""
    backups = list_backups(backup_dir, stem)
    if not backups:
        return None
    with contextlib.suppress(OSError):
        return (time.time() - backups[0].stat().st_mtime) / 3600.0
    return None


def prune_backups(backup_dir: Path, keep: int, stem: str = "sessions") -> list[Path]:
    """只保留最新 keep 份备份，返回被删掉的路径。keep<=0 表示不清理。"""
    if keep <= 0:
        return []
    removed: list[Path] = []
    for path in list_backups(backup_dir, stem)[keep:]:
        with contextlib.suppress(OSError):
            path.unlink()
            removed.append(path)
    return removed


def copy_sidecars(src: Path, dest: Path) -> None:
    """把 `-wal` / `-shm` 一并复制（供手工恢复流程使用）。"""
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(side, Path(str(dest) + suffix))
