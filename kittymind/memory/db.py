"""记忆派生库 `memory.db` —— 向量的向量、召回留痕、事件索引共用同一个文件与版本链。

**为什么是"派生库"**：库里每一张表都能从真相源重建（向量从 `memory/*.md` 重嵌、
事件索引从 `memory/events.jsonl` 重放），所以它**不进备份、不进隔离**——损坏时
直接删库重建，没有需要抢救的东西。这与 `sessions.db` 的"隔离留档"是刻意相反的
策略（会话是用户数据，记忆的向量不是）。

**为什么只有一个链**：`run_migrations` 用 `user_version` 做版本游标，一个文件
上挂两条链会互相踩版本号。所以路径、链、打开策略都收在这里，`index.py` 与
`ledger.py` 都从这里取连接。

**文件名由记忆目录名派生**（`<记忆目录名>.db`）：真实目录名就是 `memory`，于是
实际文件名正是 `memory.db`；带目录名的意义在于——同父目录下的两个记忆目录若用
固定名会共用同一个库，而库是按主键对齐的，两个库的记忆会互相覆盖（Phase 17M 踩过）。
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path

from ..config import cfg
from ..storage import (
    Migration,
    MigrationFailedError,
    SchemaTooNewError,
    checkpoint,
    quick_check,
    run_migrations,
)

logger = logging.getLogger(__name__)

_DB_SUFFIX = ".db"

_VEC_DDL = """
CREATE TABLE IF NOT EXISTS memory_vec (
    name       TEXT PRIMARY KEY,
    file       TEXT NOT NULL,
    text_hash  TEXT NOT NULL,      -- 索引文本（含正文前 N 字）的 sha256，不是文件字节
    mtime_ns   INTEGER NOT NULL DEFAULT 0,   -- 0 = 无文件信息，必须走 hash 对账
    file_size  INTEGER NOT NULL DEFAULT 0,
    model_id   TEXT NOT NULL,      -- 向量来源模型（硬校验）
    dim        INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    vector     BLOB NOT NULL
);
"""

_RECALL_DDL = """
CREATE TABLE IF NOT EXISTS memory_recall_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,                -- 本地时间 'YYYY-MM-DD HH:MM:SS'（定宽，字典序可比）
    session_id   TEXT,
    turn_seq     INTEGER NOT NULL DEFAULT 0,
    mode         TEXT NOT NULL DEFAULT '',
    query_text   TEXT NOT NULL DEFAULT '',
    candidates   TEXT NOT NULL DEFAULT '[]',   -- JSON [{name, score}]，top_k 候选集
    injected     TEXT NOT NULL DEFAULT '[]',   -- JSON [name]，真正进入本轮 prompt 的
    gate_blocked TEXT NOT NULL DEFAULT '[]',   -- JSON [name]，影子模式下"若开阈值会被拦下"的
    margin       REAL NOT NULL DEFAULT 0,      -- top1 - median(top_k)，供标定 τ
    latency_ms   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recall_ts ON memory_recall_log(ts);
CREATE INDEX IF NOT EXISTS idx_recall_session ON memory_recall_log(session_id);
"""


def _m_create_memory_vec(conn: sqlite3.Connection) -> None:
    conn.executescript(_VEC_DDL)


def _m_create_recall_log(conn: sqlite3.Connection) -> None:
    conn.executescript(_RECALL_DDL)


# 只追加，不改已发布的步骤（改了就与老库的 user_version 对不上）
MIGRATIONS = [
    Migration(1, "create_memory_vec", _m_create_memory_vec),
    Migration(2, "create_memory_recall_log", _m_create_recall_log),
]


def memory_db_path(memory_dir: str | Path | None = None) -> Path:
    """`<记忆目录的父目录>/<记忆目录名>.db`（真实目录 → `~/.kittymind/memory.db`）。"""
    base = Path(memory_dir) if memory_dir else cfg.MEMORY_DIR
    return base.parent / f"{base.name}{_DB_SUFFIX}"


def discard_db(path: str | Path) -> None:
    """删库文件（连同 `-wal` / `-shm`）。仅用于**派生**库的损坏重建。"""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(str(path) + suffix).unlink()


def open_memory_db(path: str | Path, *, maintenance: bool | None = None) -> sqlite3.Connection:
    """打开（并在需要时重建）派生库，保证 schema 已升到最新。

    与 Phase 17 的迁移纪律一致：**失败即删库重建**而不是留一个半截 schema——
    派生库没有需要抢救的数据，重建比带病运行便宜得多。
    """
    target = Path(path)
    enabled = cfg.DB_MAINTENANCE_ENABLED if maintenance is None else maintenance
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(target)
    with contextlib.suppress(Exception):
        conn.execute("PRAGMA journal_mode=WAL")
    if enabled and cfg.DB_SELF_CHECK and quick_check(conn) != "ok":
        logger.warning("记忆库损坏，删库重建（记忆文件不受影响）")
        conn = _recreate(target, conn)
    try:
        outcome = run_migrations(conn, MIGRATIONS)
    except (MigrationFailedError, SchemaTooNewError) as e:
        logger.warning("记忆库不可用（迁移失败），删库重建：%s", e)
        conn = _recreate(target, conn)
        outcome = run_migrations(conn, MIGRATIONS)
    if outcome.changed:
        logger.info("记忆库 %s", outcome.describe())
    with contextlib.suppress(Exception):
        checkpoint(conn, "TRUNCATE")
    return conn


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)


def _recreate(path: Path, conn: sqlite3.Connection) -> sqlite3.Connection:
    with contextlib.suppress(Exception):
        conn.close()
    discard_db(path)
    return _connect(path)
