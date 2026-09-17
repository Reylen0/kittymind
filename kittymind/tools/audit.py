"""工具调用审计日志：SQLite + JSONL 双写。

每次工具调用（放行/警告/阻断/拒绝）记录一行：时间、会话、工具、参数（脱敏截断）、
决策、原因、是否失败、耗时。用于排查「模型为什么反复调工具、哪次被拦」。

写入失败绝不影响工具执行（record 内部吞异常）。多线程安全（工具经 to_thread 执行）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import cfg

_SCHEMA = """
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


class ToolAuditLog:
    """工具审计双写记录器（SQLite + JSONL）。"""

    def __init__(self, db_path: Path, jsonl_path: Path):
        self._lock = threading.Lock()
        self._jsonl_path = Path(jsonl_path)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

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
            try:
                self._db.execute(
                    "INSERT INTO tool_audit "
                    "(ts, session_id, tool, args, decision, reason, failed, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["ts"], row["session_id"], row["tool"], row["args"],
                     row["decision"], row["reason"], int(row["failed"]), row["duration_ms"]),
                )
                self._db.commit()
            except Exception:
                pass
            try:
                with self._jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass


# ── 进程级单例（所有 Agent / 子 Agent 共享一个连接 + 一份日志） ──

_instance: ToolAuditLog | None = None
_instance_lock = threading.Lock()


def get_tool_audit_log() -> ToolAuditLog:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ToolAuditLog(cfg.TOOL_AUDIT_DB, cfg.TOOL_AUDIT_JSONL)
    return _instance
