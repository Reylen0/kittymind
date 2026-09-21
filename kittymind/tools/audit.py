"""工具调用审计日志：SQLite + JSONL 双写。

每次工具调用（放行/警告/阻断/拒绝）记录一行：时间、会话、工具、参数（脱敏截断）、
决策、原因、是否失败、耗时。用于排查「模型为什么反复调工具、哪次被拦」。

写入失败绝不影响工具执行（record 内部吞异常）。多线程安全（工具经 to_thread 执行）。

schema 走与会话库同一套版本链（kittymind/storage/migrations.py）：审计库此前
`user_version` 一直是 0——建表靠 `CREATE TABLE IF NOT EXISTS` 裸跑，将来想改表
根本找不到"该从哪里升级"的入口。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import cfg
from ..storage import (
    Migration,
    MigrationFailedError,
    atomic_write_text,
    checkpoint,
    quarantine_db,
    quick_check,
    run_migrations,
)

logger = logging.getLogger(__name__)

# 审计库当前 schema 版本（= 迁移链最大 version，见 _migrations）
_SCHEMA_VERSION = 1

_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS tool_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    session_id  TEXT,
    tool        TEXT NOT NULL,
    args        TEXT,
    decision    TEXT NOT NULL,   -- allow | warn | block | denied
    reason      TEXT,
    failed      INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON tool_audit(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts      ON tool_audit(ts);
"""


def _m_init_tool_audit(conn: sqlite3.Connection) -> None:
    """v1：建审计表与索引（幂等）。"""
    conn.executescript(_CREATE_AUDIT)


_MIGRATIONS = [Migration(1, "create_tool_audit", _m_init_tool_audit)]


def _parse_ts(line: str) -> datetime | None:
    """从 JSONL 行里取出 ts 并解析成 aware datetime；解析不出来返回 None。

    返回 None 的行调用方会**保留**——审计日志宁可多留一行无法解析的，
    也不能因为格式意外就把证据删掉。
    """
    try:
        raw = json.loads(line).get("ts")
        if not raw:
            return None
        dt = datetime.fromisoformat(raw)
    except (ValueError, AttributeError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ToolAuditLog:
    """工具审计双写记录器（SQLite + JSONL）。"""

    def __init__(self, db_path: Path, jsonl_path: Path):
        self._lock = threading.Lock()
        self._path = Path(db_path)
        self._jsonl_path = Path(jsonl_path)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._setup()

    def _setup(self) -> None:
        """开连接 → 自检（坏了就隔离重建）→ 迁移 → 收掉上次的 WAL。

        审计是旁路数据，坏掉不值得阻塞启动，但也**不能静默**：这里一律告警，
        并把坏库改名留档，避免"审计静默失效但没人知道"。
        """
        with contextlib.suppress(Exception):
            self._db.execute("PRAGMA journal_mode=WAL")
        if cfg.DB_MAINTENANCE_ENABLED and cfg.DB_SELF_CHECK:
            status = quick_check(self._db)
            if status != "ok":
                logger.warning("工具审计库自检未通过：%s", status)
                self._reset_corrupt()
        try:
            outcome = run_migrations(self._db, _MIGRATIONS)
        except MigrationFailedError as e:
            # 迁移失败不能拖垮 agent（审计是旁路），但要留下明确告警
            logger.warning("工具审计库迁移失败（本次审计可能不完整）：%s", e)
            return
        if outcome.changed:
            logger.info("工具审计库 %s", outcome.describe())
        with contextlib.suppress(Exception):
            checkpoint(self._db, "TRUNCATE")
        if cfg.DB_MAINTENANCE_ENABLED:
            # 裁剪挂在这里而不是启动路径：审计记录器是懒加载单例，第一次真正要用
            # 工具时才建，天然避开了「拖慢 Electron 就绪探测」这个顾虑。
            with contextlib.suppress(Exception):
                self.prune(cfg.AUDIT_RETENTION_DAYS)

    def _reset_corrupt(self) -> None:
        """隔离损坏的审计库并新建空库（审计可丢，但要留档 + 告警）。"""
        with contextlib.suppress(Exception):
            self._db.close()
        with contextlib.suppress(Exception):
            target = quarantine_db(self._path)
            logger.warning("损坏的工具审计库已隔离到：%s", target)
        with contextlib.suppress(OSError):
            self._path.unlink()
        self._db = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )

    def close(self) -> None:
        """关闭连接（幂等）。收尾 checkpoint 把 WAL 截回 0，避免它随会话累积。"""
        with self._lock:
            if self._db is not None:
                with contextlib.suppress(Exception):
                    checkpoint(self._db, "TRUNCATE")
                with contextlib.suppress(Exception):
                    self._db.close()
                self._db = None

    def record(
        self,
        *,
        session_id: str | None,
        tool: str,
        args: str,
        decision: str,
        reason: str,
        failed: bool,
        duration_ms: int,
    ) -> None:
        """记录一条审计。任何异常都被吞掉，绝不影响工具流程。"""
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "tool": tool,
            "args": args,
            "decision": decision,
            "reason": reason or "",
            "failed": bool(failed),
            "duration_ms": int(duration_ms),
        }
        with self._lock:
            if self._db is None:
                return
            with contextlib.suppress(Exception):
                self._db.execute(
                    "INSERT INTO tool_audit "
                    "(ts, session_id, tool, args, decision, reason, failed, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["ts"], row["session_id"], row["tool"], row["args"],
                     row["decision"], row["reason"], int(row["failed"]), row["duration_ms"]),
                )
            try:
                with self._jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def prune(self, days: int) -> int:
        """删除超出保留期的审计记录，返回删除条数。days <= 0 = 不裁剪。

        两侧（SQLite + JSONL）**必须一起裁**：只删库不删日志，会出现「文件里翻得到、
        库里查不到」，排查时比不裁更误导人。

        裁剪时机是「记录器首次建立」时一次性执行（见 _setup），不是每次写入都查——
        保留期以天为单位，一天一次足够，不该给每条审计都加一次时间比较。
        """
        if days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = 0
        with self._lock:
            if self._db is not None:
                with contextlib.suppress(Exception):
                    cur = self._db.execute(
                        "DELETE FROM tool_audit WHERE ts < ?", (cutoff.isoformat(),)
                    )
                    removed = cur.rowcount
            self._prune_jsonl(cutoff)
        if removed:
            logger.info("工具审计已裁剪 %d 条（保留 %d 天）", removed, days)
        return removed

    def _prune_jsonl(self, cutoff: datetime) -> None:
        """重写 JSONL 只保留 cutoff 之后的行。

        ts 用 datetime 解析后比较（而不是字符串比大小）：这里握着的是用户可读的
        文本文件，格式可能被外部编辑过，字典序比较对格式不一致的行会给出错误结论。
        解析不出来 / 没有 ts 的行一律保留。
        """
        try:
            if not self._jsonl_path.is_file():
                return
            kept: list[str] = []
            with self._jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    ts = _parse_ts(line)
                    if ts is None or ts >= cutoff:
                        kept.append(line if line.endswith("\n") else line + "\n")
            atomic_write_text(self._jsonl_path, "".join(kept))
        except OSError as e:
            logger.debug("审计 JSONL 裁剪跳过：%s", e)


# ── 进程级单例（所有 Agent / 子 Agent 共享一个连接 + 一份日志） ──

# 单元素 list 当持有者：避免 `global` 语句（ruff PLW0603），与 core/llm_trace.py 一致
_instance: list[ToolAuditLog | None] = [None]
_instance_lock = threading.Lock()


def get_tool_audit_log() -> ToolAuditLog:
    if _instance[0] is None:
        with _instance_lock:
            if _instance[0] is None:
                _instance[0] = ToolAuditLog(cfg.TOOL_AUDIT_DB, cfg.TOOL_AUDIT_JSONL)
    return _instance[0]
