"""SQLite 会话存储实现。

单连接 + threading.Lock 保证线程安全。
WAL 模式（失败则降级 DELETE）。

schema 版本 5（PRAGMA user_version=5）:
  sessions(id, title, workspace_id, created_at, updated_at,
           compressed_once, last_prompt_tokens)
  messages(id, session_id, seq REAL, role, content, tool_calls, tool_call_id, ts,
           active, compacted, is_summary)

seq 用 REAL 支持压缩摘要行的小数序（被压中段末尾与首条保留行 seq 的中点）。
active=1 → 模型视图（摘要+尾部，用于构建 LLM 上下文）。
compacted=1 → 已被压缩掉的原始行（审计用，模型不可见）。

上下文占用只存 last_prompt_tokens（实际已用 token，内容相关）。
总窗口大小是配置派生值，由读取方按当前 cfg 现算，故不落库。

升级方式：逐级迁移（_migrate），任何入口版本都补到最新，与"从哪一版升上来"无关。

两条踩过的坑（都曾真实致损）：
  1. 外键是 per-connection 设置且 SQLite 默认 OFF，必须每次打开连接就开；
     藏在 _DDL 里只在首次建库生效（executescript 还会隐式 COMMIT）。
  2. 写 sessions 表不能用 INSERT OR REPLACE —— 它先 DELETE 冲突行再 INSERT，
     会顺着 messages 的 ON DELETE CASCADE 把该会话的全部消息删掉。
"""

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path


_SCHEMA_VERSION = 5

_DDL = """
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
            # 外键是 per-connection 设置且 SQLite 默认 OFF：必须在任何语句之前、
            # 每次打开连接都打开。写进 _DDL 只在首次建库生效，旧库会以"外键关闭"
            # 运行（级联删除失效、留下孤儿行）。
            self._conn.execute("PRAGMA foreign_keys = ON")
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA journal_mode=WAL")
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver == 0:
                self._conn.executescript(_DDL)
            self._migrate()
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _migrate(self) -> None:
        """把库补齐到当前版本。

        逐级应用而非 if/elif 跳级：任何入口版本（v1/v2/v3/v4/未知）都要走完全部
        步骤，否则会出现"升到 v5 但缺列"的半截 schema —— 曾经的 ver < 3 分支就是
        只删旧列、直接标 v5，把 is_summary 漏了，read_display 直接报错。
        全部操作幂等，可重复执行。
        """
        # v2 → v3：清掉历史遗留的 token 统计列
        for col in _LEGACY_COLS:
            self._drop_column_if_exists("sessions", col)
        # v4 → v5：压缩摘要标记列
        self._ensure_column("messages", "is_summary", "INTEGER NOT NULL DEFAULT 0")
        # 外键开启前遗落的孤儿行（旧版本级联失效时留下的），清掉以免越积越多
        with contextlib.suppress(Exception):
            self._conn.execute(
                "DELETE FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)"
            )

    def _columns(self, table: str) -> set:
        try:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        except Exception:
            return set()
        return {r[1] for r in rows}

    def _ensure_column(self, table: str, col: str, decl: str) -> None:
        if col in self._columns(table):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _drop_column_if_exists(self, table: str, col: str) -> None:
        if col not in self._columns(table):
            return
        # SQLite < 3.35 不支持 DROP COLUMN，留着无害
        with contextlib.suppress(Exception):
            self._conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")

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
        """写入 / 更新会话 header。

        必须是真正的 UPSERT（ON CONFLICT DO UPDATE）而不是 INSERT OR REPLACE：
        REPLACE 会先 DELETE 冲突行再 INSERT，而 messages.session_id 带
        ON DELETE CASCADE —— 「改个标题」等于「删光这个会话的全部聊天记录」。

        只更新 title / workspace_id / updated_at：
          - created_at 保留原值：本方法可能因改标题等操作被重复调用，
            不能让每次调用都把创建时间刷成当下
          - compressed_once / last_prompt_tokens 保留（REPLACE 会重置为默认值）
          - workspace_id 缺省时保留原值（COALESCE），不把已有归属抹成 NULL
        """
        now = self._now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions"
                "(id, title, workspace_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   title=excluded.title,"
                "   workspace_id=COALESCE(excluded.workspace_id, sessions.workspace_id),"
                "   updated_at=excluded.updated_at",
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

    def list_headers(self) -> list[dict]:
        """只读会话 header（不触碰 messages 表），供 session/list 使用。

        只取 id/title/workspace_id/created_at 这 4 个字段：会话列表场景不需要
        消息内容，若靠调用方逐个 read() 取全量消息再挑字段，会话一多，列表
        RPC 就会退化成全量读消息。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, workspace_id, created_at"
                " FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "version":            1,
                "id":                 r[0],
                "title":              r[1],
                "workspace_id":       r[2],
                "created_at":         r[3],
            }
            for r in rows
        ]

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
