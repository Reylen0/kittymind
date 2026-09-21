"""SQLite 会话存储实现。

单连接 + threading.Lock 保证线程安全。
WAL 模式（失败则降级 DELETE）。

schema 版本 7（PRAGMA user_version=7）:
  sessions(id, title, workspace_id, created_at, updated_at,
           compressed_once, last_prompt_tokens)
  messages(id, session_id, seq REAL, role, content, tool_calls, tool_call_id, ts,
           active, compacted, is_summary)
  messages_fts(body)  —— 全文检索索引，rowid 对齐 messages.id（见 _ensure_fts）
  model_usage(id, session_id, model_id, prompt_tokens, completion_tokens,
              n_calls, ts, kind)

model_usage 的 session_id **不带外键 CASCADE**：用量是审计数据，删会话时
成本记录要保留（否则删会话就把成本历史抹掉，违背追踪初衷）。删会话后
该行的 session_id 悬空，聚合按 model/day 时仍计入，按 session 时天然消失。

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
from datetime import datetime, timezone
from pathlib import Path

from ._search_text import to_match_expr, to_search_text


_SCHEMA_VERSION = 7

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

# model_usage：用量成本追踪。session_id 允许 NULL（非会话类消耗），
# 故意不建外键 CASCADE——删会话保留成本审计记录。
_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS model_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT,
    model_id          TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    n_calls           INTEGER NOT NULL DEFAULT 1,
    ts                INTEGER NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'turn'
);

CREATE INDEX IF NOT EXISTS idx_usage_model_ts ON model_usage(model_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_session ON model_usage(session_id);
"""

_LEGACY_COLS = ("used_tokens", "total_tokens", "context_ratio")

# ── 全文检索（v5 → v6）────────────────────────────────────────────
# body 存的是二元组预处理文本（见 _search_text.py），不是原文；rowid 对齐
# messages.id（该列是 INTEGER PRIMARY KEY AUTOINCREMENT，天然就是 rowid）。
#
# 删除侧用纯 SQL 触发器，于是 sessions 的 ON DELETE CASCADE 能顺带清掉 FTS 行
# （已验证级联删除会触发 AFTER DELETE）。写入侧**不能**也做成触发器：二元组要
# 靠 Python 算，触发器里只能经 create_function 调宿主函数，而那样一来任何外部
# 工具（DB Browser、sqlite3 CLI）打开这个库时都找不到该函数，连 INSERT 都会失败。
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(body, tokenize='unicode61');

CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
  DELETE FROM messages_fts WHERE rowid = old.id;
END;
"""

# 只索引真正的对话正文。role='tool' 的 content 是工具原始输出（文件全文、
# bash stdout、grep 结果），索引进去会让索引比 messages 表本身还大，而且搜索
# 结果会被工具输出淹没——搜「压缩」命中一堆 grep 回显，真正的对话反而沉底。
_INDEXED_ROLES = ("user", "assistant")

# 回填/索引时跳过空正文（assistant 只发工具调用的那一轮 content 为 NULL）
_INDEXABLE_WHERE = (
    "role IN ('user','assistant') AND is_summary=0"
    " AND content IS NOT NULL AND content <> ''"
)

# 展示视图的行/条件：原始消息（compacted=1）+ 活跃非摘要消息（active=1, is_summary=0）
_DISPLAY_COLS = "seq, role, content, tool_calls, tool_call_id"
_DISPLAY_WHERE = "session_id=? AND ((active=1 AND is_summary=0) OR compacted=1)"

# 页首补齐的防呆上限：向前最多多取多少行（一批工具调用的行数不会接近这个量级）
_BLOCK_HEAD_SLACK = 200


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

    def close(self) -> None:
        """关闭 SQLite 连接（幂等）。进程收尾时调用；WAL 的 checkpoint 随之完成。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

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
        # v5 → v6：全文检索索引
        self._ensure_fts()
        # v6 → v7：用量成本追踪表
        self._ensure_usage()

    def _ensure_usage(self) -> None:
        """建 model_usage 表（幂等）。调用方（_migrate）已持有 self._lock。"""
        with contextlib.suppress(Exception):
            self._conn.executescript(_USAGE_DDL)

    def _ensure_fts(self) -> None:
        """建全文检索表与删除触发器；仅在本次真的新建了表时回填存量消息。

        调用方（_migrate ← _init_db）已持有 self._lock，本方法及其下游不再取锁。

        判断「是否新建」用 sqlite_master 而不是 user_version：用户可能在某个
        中间版本上跑过、也可能手工删过这张表，以表的实际存在与否为准最可靠。

        一致性前提：消息正文目前没有 UPDATE 路径——只有 INSERT、标记 compacted
        （不改 content）、级联删除三种。**若将来新增「编辑历史消息」功能，
        必须在那条路径上同步更新 FTS 行**，否则索引会与原文悄悄漂移。
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone() is not None
        try:
            self._conn.executescript(_FTS_DDL)
        except Exception:
            # FTS5 未编译进本机 SQLite：搜索能力降级为不可用，但不能拖垮整个会话库
            return
        if not exists:
            self._backfill_fts()

    def _backfill_fts(self) -> None:
        """把存量消息一次性灌进索引（调用方已持锁）。

        显式包事务：连接是 isolation_level=None（autocommit），逐条 INSERT
        会变成几万次 fsync，几秒钟的事能拖成几分钟。
        """
        rows = self._conn.execute(
            f"SELECT id, content FROM messages WHERE {_INDEXABLE_WHERE}"
        ).fetchall()
        if not rows:
            return
        try:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT INTO messages_fts(rowid, body) VALUES (?, ?)",
                ((r[0], to_search_text(r[1])) for r in rows),
            )
            self._conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                self._conn.execute("ROLLBACK")
            raise

    def _index_message(self, row_id: int, role: str, content: str | None) -> None:
        """把一条刚写入的消息加进索引（调用方必须已持有 self._lock）。

        索引失败不应该让「消息落库」这件事失败——搜索是增强能力，
        丢一条索引只是这条消息搜不到，丢一条消息是数据损坏。
        """
        if role not in _INDEXED_ROLES or not content:
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT INTO messages_fts(rowid, body) VALUES (?, ?)",
                (row_id, to_search_text(content)),
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
            cur = self._conn.execute(
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
            self._index_message(cur.lastrowid, record["role"], record.get("content"))

    def read_header(self, session_id: str) -> dict | None:
        """只读 sessions 表的 header（不触碰 messages 表）。

        分页入口需要 header（标题/工作目录/上下文占用），但不该顺带把整个会话的
        消息全查出来——旧实现复用 read()，为了一个 workspace_id 也会加载全量消息。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, workspace_id, created_at, compressed_once,"
                "last_prompt_tokens"
                " FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "version":            1,
            "id":                 row[0],
            "title":              row[1],
            "workspace_id":       row[2],
            "created_at":         row[3],
            "compressed_once":    bool(row[4]),
            "last_prompt_tokens": row[5],
        }

    def read(self, session_id: str) -> tuple[dict | None, list[dict]]:
        """读 header + active 消息列表（带 seq，供 agent 打 _seq 标记）。"""
        header = self.read_header(session_id)
        if header is None:
            return None, []

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
                f"SELECT {_DISPLAY_COLS} FROM messages"
                f" WHERE {_DISPLAY_WHERE} ORDER BY seq",
                (session_id,),
            ).fetchall()
        return self._records_from_rows(rows)

    def read_display_page(
        self, session_id: str, limit: int = 50, before_seq: float | None = None
    ) -> dict:
        """分页读展示视图：取 seq 严格小于 before_seq 的最新 limit 条（首屏不传 before_seq）。

        返回 {"messages": 升序列表, "has_more": 更早是否还有, "cursor": 本页最早一条的 seq}。
        `cursor` 原样回传给下一次调用即可继续向前翻页。

        两条不变式：
          1. 页与页严格由 seq 区间切分，不重叠、不遗漏（seq 在会话内唯一，索引
             idx_msg_session_seq 直接支持这种 DESC + LIMIT 查询）。
          2. 页首若落在「一批工具调用」的中段（首行是 tool 行），向前补齐到该批的
             发起行——否则同一批工具调用被切到两页，前端会把它们渲染成两个折叠组。
             页尾不裁剪：工具行的名字/入参已随行内联（见 _records_from_rows），
             即使发起行留在下一页，本页首的 tool 行也能独立显示。
        """
        limit = max(1, int(limit))
        where = [_DISPLAY_WHERE]
        args: list = [session_id]
        if before_seq is not None:
            where.append("seq < ?")
            args.append(before_seq)

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_DISPLAY_COLS} FROM messages"
                f" WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
            rows = rows[:limit]                      # DESC 头部 = 最新的 limit 条
            rows = self._extend_to_block_head(session_id, rows)
            if rows:
                # 补齐后最早一条之前可能还有行；has_more 以补齐后的边界为准重新判断
                has_more = self._conn.execute(
                    f"SELECT 1 FROM messages WHERE {_DISPLAY_WHERE} AND seq < ? LIMIT 1",
                    (session_id, rows[-1][0]),
                ).fetchone() is not None
            else:
                has_more = False

        messages = self._records_from_rows(reversed(rows))
        return {
            "messages": messages,
            "has_more": has_more,
            "cursor":    messages[0]["seq"] if messages else None,
        }

    # ── 展示记录构造（以下两个方法只做内存处理，不再取锁） ─────────

    def _extend_to_block_head(self, session_id: str, rows: list) -> list:
        """rows 为 seq 降序；若最早一行是 tool 行，向前取到该批的发起行。

        调用方必须已持有 self._lock（threading.Lock 不可重入）。发起行一定是紧邻
        前面的第一条非 tool 行，故不必解析 tool_calls 去找 id。
        """
        if not rows or rows[-1][1] != "tool":
            return rows
        extra = []
        seq = rows[-1][0]
        for _ in range(_BLOCK_HEAD_SLACK):
            row = self._conn.execute(
                f"SELECT {_DISPLAY_COLS} FROM messages"
                f" WHERE {_DISPLAY_WHERE} AND seq < ? ORDER BY seq DESC LIMIT 1",
                (session_id, seq),
            ).fetchone()
            if row is None:
                break
            extra.append(row)
            seq = row[0]
            if row[1] != "tool":
                break
        return rows + extra

    def _records_from_rows(self, rows) -> list[dict]:
        """数据库行 → 展示记录，并为 tool 行内联发起行里的名字/入参。

        名字随行下发，而不是让前端按 tool_call_id 跨消息配对：分页把消息切成若干段，
        单段内看不到发起行时前端只能退化成 'tool'（曾真实出现的历史 bug）。发起行
        必然与 tool 行同页——页首补齐保证了这一点。
        """
        rows = list(rows)
        calls: dict[str, dict] = {}
        for r in rows:
            if r[1] != "assistant" or not r[3]:
                continue
            with contextlib.suppress(Exception):
                for call in json.loads(r[3]):
                    if isinstance(call, dict) and call.get("id"):
                        calls.setdefault(call["id"], call)

        records = []
        for r in rows:
            msg: dict = {"seq": r[0], "role": r[1], "content": r[2]}
            if r[3]:
                msg["tool_calls"] = json.loads(r[3])
            if r[4]:
                msg["tool_call_id"] = r[4]
                fn = (calls.get(r[4]) or {}).get("function") or {}
                if fn.get("name"):
                    msg["tool_name"] = fn["name"]
                if fn.get("arguments") is not None:
                    msg["tool_args"] = fn["arguments"]
            records.append(msg)
        return records

    # ── 全文检索 ──────────────────────────────────────────────────

    def search(
        self, query: str, session_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """全文搜索消息正文，按相关度（bm25）排序。

        走**完整视图**（active=1 OR compacted=1）：被压缩掉的中段消息仍在库里，
        而那恰恰是用户最想找、界面上又最难翻到的部分——只搜活跃消息，
        长会话的搜索基本等于失效。

        返回每条命中的 session_id / seq / role / content（原文）/ 会话标题。
        给 seq 而不是 id：前端整套定位与分页游标（before_seq）都基于 seq。
        """
        expr = to_match_expr(query)
        if not expr:
            return []                       # 切不出 token：空查询，不是错误

        # 不给 messages_fts 起别名：FTS5 的 MATCH 与 bm25() 都只认表名，
        # 写成别名会直接报 "no such column"。
        sql = [
            "SELECT m.session_id, m.seq, m.role, m.content, s.title",
            "  FROM messages_fts",
            "  JOIN messages m ON m.id = messages_fts.rowid",
            "  JOIN sessions s ON s.id = m.session_id",
            " WHERE messages_fts MATCH ?",
            "   AND (m.active=1 OR m.compacted=1)",
        ]
        params: list = [expr]
        if session_id:
            sql.append("   AND m.session_id = ?")
            params.append(session_id)
        sql.append(" ORDER BY bm25(messages_fts) LIMIT ?")
        params.append(limit)

        with self._lock:
            try:
                rows = self._conn.execute("\n".join(sql), params).fetchall()
            except Exception:
                return []                   # 索引表缺失（FTS5 不可用）→ 搜索静默降级
        return [
            {"session_id": r[0], "seq": r[1], "role": r[2],
             "content": r[3], "title": r[4]}
            for r in rows
        ]

    def fts_row_count(self) -> int:
        """索引行数（测试与诊断用；FTS5 不可用时返回 0）。"""
        with self._lock:
            try:
                return self._conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
            except Exception:
                return 0

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
                # 摘要行刻意不进全文索引：它是原文的浓缩，而原文仍以 compacted=1
                # 留在库里且照常可搜——两边都索引只会让同一段内容命中两次。
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
                    cur = self._conn.execute(
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
                    self._index_message(cur.lastrowid, msg["role"], msg.get("content"))
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

    def delete(self, session_id: str) -> bool:
        """删除会话（连带 messages 的 ON DELETE CASCADE）。返回是否真的存在并删除。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return cur.rowcount > 0

    def clear_workspace(self, workspace_id: str) -> int:
        """解除某工作区下所有会话的归属（workspace_id 置 NULL），返回迁移条数。

        删工作区时调用：会话本身保留，只是移回「对话」分组——删分组不应
        连带删掉用户的对话历史。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET workspace_id=NULL, updated_at=? WHERE workspace_id=?",
                (self._now_iso(), workspace_id),
            )
            return cur.rowcount

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

    # ── 用量成本追踪 ─────────────────────────────────

    def record_usage(
        self,
        session_id: str | None,
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        n_calls: int = 1,
        kind: str = "turn",
    ) -> None:
        """写入一条用量记录。session_id 允许 None（非会话类消耗）。

        n_calls 只增不改为 0；空调用（prompt 与 completion 均为 0 且无调用）
        仍会落一条 0 行，调用方负责在没必要时不调用本方法。
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_usage"
                "(session_id, model_id, prompt_tokens, completion_tokens, n_calls, ts, kind)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    model_id,
                    int(prompt_tokens),
                    int(completion_tokens),
                    max(1, int(n_calls)),
                    self._ts(),
                    kind,
                ),
            )

    def aggregate_usage(
        self,
        group_by: str = "model",
        since: int | None = None,
        until: int | None = None,
    ) -> list[dict]:
        """按 model / day / session 聚合用量。

        返回列表，每条含 key / prompt_tokens / completion_tokens / n_calls /
        by_model（{model_id: {prompt_tokens, completion_tokens}}），按
        prompt_tokens + completion_tokens 降序（最大消耗排前）。

        by_model 的用途：day/session 分组不是单一模型，rpc 层要按「每组内各
        模型分别计价再求和」才能算准成本，所以聚合时顺带保留模型维度。
        """
        where: list[str] = []
        args: list = []
        if since is not None:
            where.append("ts >= ?")
            args.append(int(since))
        if until is not None:
            where.append("ts <= ?")
            args.append(int(until))
        cond = (" WHERE " + " AND ".join(where)) if where else ""

        if group_by == "day":
            key_expr = "date(ts, 'unixepoch', 'localtime')"
        elif group_by == "session":
            key_expr = "COALESCE(session_id, '(none)')"
        else:  # model
            key_expr = "model_id"

        sql = (
            f"SELECT {key_expr} AS k, model_id,"
            " SUM(prompt_tokens), SUM(completion_tokens), SUM(n_calls)"
            f" FROM model_usage{cond} GROUP BY k, model_id"
        )
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()

        # 合并 (组, 模型) 行 → 组行；保留每组内各模型 token 明细供上层计价
        groups: dict[str, dict] = {}
        for k, model_id, p, c, n in rows:
            g = groups.setdefault(
                k,
                {
                    "key": k,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "n_calls": 0,
                    "by_model": {},
                },
            )
            p_i, c_i, n_i = int(p or 0), int(c or 0), int(n or 0)
            g["prompt_tokens"] += p_i
            g["completion_tokens"] += c_i
            g["n_calls"] += n_i
            bucket = g["by_model"].setdefault(
                str(model_id), {"prompt_tokens": 0, "completion_tokens": 0}
            )
            bucket["prompt_tokens"] += p_i
            bucket["completion_tokens"] += c_i

        return sorted(
            groups.values(),
            key=lambda g: g["prompt_tokens"] + g["completion_tokens"],
            reverse=True,
        )

    def session_usage(self, session_id: str) -> dict:
        """单个会话的用量合计（供实时徽标）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0),"
                " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(n_calls),0)"
                " FROM model_usage WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return {
            "prompt_tokens": int(row[0] or 0),
            "completion_tokens": int(row[1] or 0),
            "n_calls": int(row[2] or 0),
        }
