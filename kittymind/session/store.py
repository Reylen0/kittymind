"""SQLite 会话存储实现。

单连接 + threading.Lock 保证线程安全。
WAL 模式（失败则降级 DELETE）。

schema 版本 4（PRAGMA user_version=4）:
  sessions(id, title, workspace_id, created_at, updated_at,
           compressed_once, last_prompt_tokens)
  messages(id, session_id, seq REAL, role, content, tool_calls, tool_call_id, ts,
           active, compacted)

seq 用 REAL 支持压缩摘要行的小数序（被压中段末尾与首条保留行 seq 的中点）。
active=1 → 模型视图（摘要+尾部，用于构建 LLM 上下文）。
compacted=1 → 已被压缩掉的原始行（审计用，模型不可见）。

上下文占用只存 last_prompt_tokens（实际已用 token，内容相关）。
总窗口大小是配置派生值，由读取方按当前 cfg 现算，故不落库。

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
    seq          REAL    NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    ts           INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    compacted    INTEGER NOT NULL DEFAULT 0,
    is_summary   INTEGER NOT NULL DEFAULT 0   -- 压缩摘要行，模型可见但前端不展示
);

CREATE INDEX IF NOT EXISTS idx_msg_session_seq ON messages(session_id, seq);
"""

_LEGACY_COLS = ("used_tokens", "total_tokens", "context_ratio")


class SqliteSessionStore:
    """SQLite 会话存储。线程安全（Lock + check_same_thread=False）。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,
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
                self._conn.execute("PRAGMA user_version=5")
            elif ver < 3:
                for col in _LEGACY_COLS:
                    try:
                        self._conn.execute(f"ALTER TABLE sessions DROP COLUMN {col}")
                    except Exception:
                        pass
                self._conn.execute("PRAGMA user_version=5")
            elif ver in (3, 4):
                # v3/v4 → v5: 新增 is_summary 列
                try:
                    self._conn.execute(
                        "ALTER TABLE messages ADD COLUMN is_summary INTEGER NOT NULL DEFAULT 0"
                    )
                except Exception:
                    pass  # 已存在时忽略
                self._conn.execute("PRAGMA user_version=5")

    # ── 内部辅助 ──────────────────────────────────────────────────

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _ts(self) -> int:
        return int(time.time())

    def next_seq(self, session_id: str) -> int:
        """下一个整数 seq，严格大于该会话所有行（含 compacted）的最大 seq。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT CAST(COALESCE(MAX(seq), -1) AS INTEGER) + 1"
                " FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

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
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        """读 header + active 消息列表（带 seq，供 agent 打 _seq 标记）。"""
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

    def read_full(self, session_id: str) -> list[dict]:
        """完整视图：active + compacted，按 seq 排序（审计 / 回溯用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, role, content, tool_calls, tool_call_id, active, compacted"
                " FROM messages WHERE session_id=? AND (active=1 OR compacted=1)"
                " ORDER BY seq",
                (session_id,),
            ).fetchall()
        result = []
        for r in rows:
            msg: dict = {
                "seq":       r[0],
                "role":      r[1],
                "content":   r[2],
                "active":    bool(r[5]),
                "compacted": bool(r[6]),
            }
            if r[3]:
                msg["tool_calls"] = json.loads(r[3])
            if r[4]:
                msg["tool_call_id"] = r[4]
            result.append(msg)
        return result

    def read_display(self, session_id: str) -> list[dict]:
        """前端展示视图：原始消息（compacted=1）+ 活跃非摘要消息（active=1, is_summary=0），按 seq 排序。

        摘要行（is_summary=1）被过滤掉，用户看到的是完整的原始对话记录而非摘要。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, role, content, tool_calls, tool_call_id"
                " FROM messages"
                " WHERE session_id=?"
                "   AND ((active=1 AND is_summary=0) OR compacted=1)"
                " ORDER BY seq",
                (session_id,),
            ).fetchall()
        records = []
        for r in rows:
            msg: dict = {"role": r[1], "content": r[2]}
            if r[3]:
                msg["tool_calls"] = json.loads(r[3])
            if r[4]:
                msg["tool_call_id"] = r[4]
            records.append(msg)
        return records

    def archive_and_compact(
        self,
        session_id: str,
        compacted_seqs: set,
        summary_rows: list[dict],
        new_active_msgs: list[dict],
    ) -> None:
        """原子压缩落库。

        compacted_seqs: 被摘要掉的中段消息 seq 集合 → 标记 active=0, compacted=1。
        summary_rows:   摘要消息列表，每条带 'seq'(小数)、'role'、'content'。
        new_active_msgs: 本轮新增消息（无 _seq，尾部新消息），seq 从 next_seq 起递增。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN")

                # 1. 中段旧消息标 compacted
                if compacted_seqs:
                    placeholders = ",".join("?" for _ in compacted_seqs)
                    self._conn.execute(
                        f"UPDATE messages SET active=0, compacted=1"
                        f" WHERE session_id=? AND seq IN ({placeholders})",
                        (session_id, *compacted_seqs),
                    )

                # 2. 插入摘要行（小数 seq，is_summary=1 标记供前端过滤）
                ts = int(time.time())
                for s in summary_rows:
                    self._conn.execute(
                        "INSERT INTO messages"
                        "(session_id, seq, role, content, ts, active, compacted, is_summary)"
                        " VALUES (?, ?, ?, ?, ?, 1, 0, 1)",
                        (session_id, s["seq"], s["role"], s.get("content"), ts),
                    )

                # 3. 追加本轮新消息
                base_seq_row = self._conn.execute(
                    "SELECT CAST(COALESCE(MAX(seq), -1) AS INTEGER) + 1"
                    " FROM messages WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                seq = int(base_seq_row[0]) if base_seq_row else 0
                for msg in new_active_msgs:
                    tc = msg.get("tool_calls")
                    self._conn.execute(
                        "INSERT INTO messages"
                        "(session_id, seq, role, content, tool_calls, tool_call_id, ts, active, compacted)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)",
                        (
                            session_id, seq,
                            msg["role"],
                            msg.get("content"),
                            json.dumps(tc, ensure_ascii=False) if tc else None,
                            msg.get("tool_call_id"),
                            ts,
                        ),
                    )
                    seq += 1

                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

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
