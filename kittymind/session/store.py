"""SQLite 会话存储实现。

单连接 + threading.Lock 保证线程安全。
WAL 模式（失败则降级 DELETE）。

schema 版本 1（PRAGMA user_version=1）:
  sessions(id, title, workspace_id, created_at, updated_at,
           compressed_once, last_prompt_tokens, context_ratio)
  messages(id, session_id, seq, role, content, tool_calls, tool_call_id, ts,
           active, compacted)  ← active/compacted 为阶段二预留

升级方式：PRAGMA user_version 驱动迁移链（Phase 17.2）。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path


_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT    PRIMARY KEY,
    title           TEXT    NOT NULL DEFAULT '新对话',
    workspace_id    TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    -- Phase 14.7: 压缩状态持久化
    compressed_once INTEGER NOT NULL DEFAULT 0,
    last_prompt_tokens INTEGER,
    context_ratio   REAL    NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT,
    tool_calls   TEXT,   -- JSON
    tool_call_id TEXT,
    ts           INTEGER NOT NULL DEFAULT 0,
    -- Stage 2 预留（Phase 14 阶段二）
    active       INTEGER NOT NULL DEFAULT 1,
    compacted    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_msg_session_seq ON messages(session_id, seq);
"""


class SqliteSessionStore:
    """SQLite 会话存储。线程安全（Lock + check_same_thread=False）。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,   # autocommit，手动 BEGIN/COMMIT
        )
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            # WAL 模式（网络盘/只读挂载时静默降级）
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            self._conn.executescript(_DDL)
            self._conn.execute("PRAGMA user_version=1")

    # ── 内部辅助 ──────────────────────────────────────────────────

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _ts(self) -> int:
        return int(time.time())

    # ── SessionStore 接口 ─────────────────────────────────────────

    def write_header(self, session_id: str, header: dict) -> None:
        now = self._now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions"
                "(id, title, workspace_id, created_at, updated_at)"
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    header.get("title", "新对话"),
                    header.get("workspace_id"),
                    header.get("created_at", now),
                    now,
                ),
            )

    def append(self, session_id: str, record: dict) -> None:
        tc = record.get("tool_calls")
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(session_id, seq, role, content, tool_calls, tool_call_id, ts)"
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    record["seq"],
                    record["role"],
                    record.get("content"),
                    json.dumps(tc, ensure_ascii=False) if tc else None,
                    record.get("tool_call_id"),
                    self._ts(),
                ),
            )

    def read(self, session_id: str) -> tuple[dict | None, list[dict]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, workspace_id, created_at, compressed_once,"
                "last_prompt_tokens, context_ratio"
                " FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None, []

        header = {
            "version":            1,
            "id":                 row[0],
            "title":              row[1],
            "workspace_id":       row[2],
            "created_at":         row[3],
            "compressed_once":    bool(row[4]),
            "last_prompt_tokens": row[5],
            "context_ratio":      float(row[6] or 0.0),
        }

        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, role, content, tool_calls, tool_call_id"
                " FROM messages WHERE session_id=? AND active=1 ORDER BY seq",
                (session_id,),
            ).fetchall()

        records = []
        for r in rows:
            msg: dict = {"seq": r[0], "role": r[1], "content": r[2]}
            if r[3]:
                msg["tool_calls"] = json.loads(r[3])
            if r[4]:
                msg["tool_call_id"] = r[4]
            records.append(msg)
        return header, records

    def update_header(self, session_id: str, updates: dict) -> None:
        """更新 sessions 表的可更新字段（title / workspace_id 等）。"""
        allowed = {"title", "workspace_id"}
        cols = {k: v for k, v in updates.items() if k in allowed}
        if not cols:
            return
        set_clause = ", ".join(f"{k}=?" for k in cols)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {set_clause}, updated_at=? WHERE id=?",
                (*cols.values(), self._now_iso(), session_id),
            )

    def exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row is not None

    def delete(self, session_id: str) -> None:
        with self._lock:
            # ON DELETE CASCADE 会同步删 messages
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def list_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        return [r[0] for r in rows]

    def get_state(self, session_id: str) -> dict:
        """读取会话的压缩状态字段。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT compressed_once, last_prompt_tokens, context_ratio"
                " FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        return {
            "compressed_once":    bool(row[0]),
            "last_prompt_tokens": row[1],
            "context_ratio":      float(row[2] or 0.0),
        }

    def save_state(self, session_id: str, **fields) -> None:
        """将压缩状态持久化到 sessions 表。"""
        allowed = {"compressed_once", "last_prompt_tokens", "context_ratio"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        # 布尔 → int
        if "compressed_once" in cols:
            cols["compressed_once"] = int(bool(cols["compressed_once"]))
        set_clause = ", ".join(f"{k}=?" for k in cols)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {set_clause}, updated_at=? WHERE id=?",
                (*cols.values(), self._now_iso(), session_id),
            )
