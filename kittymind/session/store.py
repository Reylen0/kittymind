"""SQLite 会话存储实现。

单连接 + threading.Lock 保证线程安全。
WAL 模式（失败则降级 DELETE）。

schema 版本 3（PRAGMA user_version=3）:
  sessions(id, title, workspace_id, created_at, updated_at,
           compressed_once, last_prompt_tokens)
  messages(id, session_id, seq, role, content, tool_calls, tool_call_id, ts,
           active, compacted)  ← active/compacted 为阶段二预留

上下文占用只存 last_prompt_tokens（实际已用 token，内容相关）。
总窗口大小是配置派生值，由读取方按当前 cfg 现算，故不落库——
这样改动 LLM_CONTEXT_WINDOW / 换模型后，旧会话占用比自动跟随。

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
    compressed_once    INTEGER NOT NULL DEFAULT 0,
    last_prompt_tokens INTEGER
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

# v1/v2 遗留的上下文占用列，现由 cfg 现算，best-effort 清理（SQLite 3.35+ 支持 DROP COLUMN）
_LEGACY_COLS = ("used_tokens", "total_tokens", "context_ratio")


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
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver == 0:
                self._conn.executescript(_DDL)
                self._conn.execute("PRAGMA user_version=3")
            elif ver < 3:
                # 旧库：清理不再使用的占用列（last_prompt_tokens/compressed_once 自 v1 起已存在）
                for col in _LEGACY_COLS:
                    try:
                        self._conn.execute(f"ALTER TABLE sessions DROP COLUMN {col}")
                    except Exception:
                        pass  # 列不存在或 SQLite 版本过低时忽略
                self._conn.execute("PRAGMA user_version=3")

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
                "last_prompt_tokens"
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
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def list_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        return [r[0] for r in rows]

    def get_state(self, session_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT compressed_once, last_prompt_tokens"
                " FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        return {
            "compressed_once":    bool(row[0]),
            "last_prompt_tokens": row[1],
        }

    def save_state(self, session_id: str, **fields) -> None:
        allowed = {"compressed_once", "last_prompt_tokens"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        if "compressed_once" in cols:
            cols["compressed_once"] = int(bool(cols["compressed_once"]))
        set_clause = ", ".join(f"{k}=?" for k in cols)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {set_clause}, updated_at=? WHERE id=?",
                (*cols.values(), self._now_iso(), session_id),
            )
