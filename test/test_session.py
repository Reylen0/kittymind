"""SqliteSessionStore / SessionManager 单元测试（无需 LLM）"""

import sqlite3
from pathlib import Path

import pytest

from kittymind.session.store import SqliteSessionStore, _SCHEMA_VERSION
from kittymind.session.manager import SessionManager


# ── fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return SqliteSessionStore(tmp_path / "test.db")


@pytest.fixture
def mgr(tmp_path):
    return SessionManager(db_path=tmp_path / "test.db")


# ── SqliteSessionStore ────────────────────────────────────────────

def test_store_write_and_read(store):
    store.write_header("s1", {"version": 1, "id": "s1", "title": "test",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    store.append("s1", {"seq": 0, "role": "user", "content": "hello"})
    store.append("s1", {"seq": 1, "role": "assistant", "content": "hi"})

    header, records = store.read("s1")
    assert header["id"] == "s1"
    assert len(records) == 2
    assert records[0]["content"] == "hello"


def test_store_exists_and_delete(store):
    assert not store.exists("s1")
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    assert store.exists("s1")
    store.delete("s1")
    assert not store.exists("s1")


def test_store_list_ids(store):
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    store.write_header("s2", {"version": 1, "id": "s2",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    ids = store.list_ids()
    assert set(ids) == {"s1", "s2"}


def test_store_get_and_save_state(store):
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    state = store.get_state("s1")
    assert bool(state.get("compressed_once")) is False
    assert state.get("last_prompt_tokens") is None

    store.save_state("s1",
                     compressed_once=True,
                     last_prompt_tokens=5000)
    state2 = store.get_state("s1")
    assert bool(state2["compressed_once"]) is True
    assert state2["last_prompt_tokens"] == 5000


def test_store_tool_calls_roundtrip(store):
    tc = [{"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}]
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    store.append("s1", {"seq": 0, "role": "assistant",
                         "content": None, "tool_calls": tc})
    _, records = store.read("s1")
    assert records[0]["tool_calls"] == tc


# ── SessionManager ────────────────────────────────────────────────

def test_manager_create_and_get(mgr):
    sid = mgr.create_session(title="聊天")
    session = mgr.get_session(sid)
    assert session is not None
    assert session["header"]["title"] == "聊天"
    assert session["messages"] == []


def test_manager_list_sessions(mgr):
    mgr.create_session(title="A")
    mgr.create_session(title="B")
    assert len(mgr.list_sessions()) == 2


# ── P0 回归：session/list 不得读取消息表 ──────────────────────────

def test_store_list_headers_returns_summary_only(store):
    store.write_header("s1", {"version": 1, "id": "s1", "title": "t1",
                              "created_at": "2026-01-01T00:00:00+00:00"})
    for i in range(50):
        store.append("s1", {"seq": i, "role": "user", "content": f"msg-{i}"})

    headers = store.list_headers()
    assert [h["id"] for h in headers] == ["s1"]
    assert headers[0]["title"] == "t1"
    assert headers[0]["created_at"] == "2026-01-01T00:00:00+00:00"


def test_manager_list_sessions_never_reads_messages(mgr):
    """回归：list_sessions 曾对每个会话调用 read()（连带加载该会话全部消息）。"""
    for i in range(3):
        sid = mgr.create_session(title=f"S{i}")
        mgr.append_turn(sid, [{"role": "user", "content": "hi"}] * 20)

    def _boom(*args, **kwargs):
        raise AssertionError("list_sessions 不应读取消息")

    mgr.store.read = _boom
    mgr.store.read_display = _boom

    sessions = mgr.list_sessions()
    assert len(sessions) == 3
    assert {s["title"] for s in sessions} == {"S0", "S1", "S2"}
    assert all(s["id"] for s in sessions)


def test_manager_delete_session(mgr):
    sid = mgr.create_session()
    mgr.delete_session(sid)
    assert mgr.get_session(sid) is None


def test_manager_append_turn(mgr):
    sid = mgr.create_session(title="test")
    turn = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]
    mgr.append_turn(sid, turn, "你好")
    session = mgr.get_session(sid)
    assert len(session["messages"]) == 2
    # get_session 走展示视图（无 seq），用 load_messages 验证 seq
    msgs = mgr.load_messages(sid)
    assert msgs[0]["seq"] == 0
    assert msgs[1]["seq"] == 1


def test_manager_append_turn_creates_session_if_missing(mgr):
    turn = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    mgr.append_turn("auto-sid", turn, "hello")
    session = mgr.get_session("auto-sid")
    assert session is not None
    assert session["header"]["title"] == "hello"


def test_manager_load_history(mgr):
    sid = mgr.create_session()
    turn = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "结果", "tool_call_id": "c1"},
        {"role": "assistant", "content": "答案"},
    ]
    mgr.append_turn(sid, turn)
    history = mgr.load_history(sid)
    assert len(history) == 4
    assert history[1]["tool_calls"] == [{"id": "c1"}]
    assert history[2]["tool_call_id"] == "c1"
    assert "seq" not in history[0]


def test_manager_generate_title(mgr):
    assert mgr.generate_title("短标题") == "短标题"
    long_text = "这是一段超过二十个字符的标题用于测试截断功能"
    title = mgr.generate_title(long_text)
    assert title.endswith("...")
    assert len(title) == 23


def test_manager_multiple_turns_seq(mgr):
    sid = mgr.create_session()
    mgr.append_turn(sid, [
        {"role": "user", "content": "turn1"},
        {"role": "assistant", "content": "resp1"},
    ])
    mgr.append_turn(sid, [
        {"role": "user", "content": "turn2"},
        {"role": "assistant", "content": "resp2"},
    ])
    history = mgr.load_history(sid)
    assert len(history) == 4
    seqs = [m["seq"] for m in mgr.load_messages(sid)]
    assert seqs == [0, 1, 2, 3]


def test_manager_session_state_roundtrip(mgr):
    sid = mgr.create_session()
    mgr.save_session_state(sid,
                           compressed_once=True,
                           last_prompt_tokens=8192)
    state = mgr.get_session_state(sid)
    assert bool(state["compressed_once"]) is True
    assert state["last_prompt_tokens"] == 8192


def test_manager_get_session_derives_token_counts(mgr):
    from kittymind.config import cfg
    sid = mgr.create_session()
    mgr.save_session_state(sid, last_prompt_tokens=50000)
    session = mgr.get_session(sid)
    # used 来自持久化，total 按当前 cfg 现算（换窗口后自动跟随）
    assert session["header"]["used_tokens"] == 50000
    assert session["header"]["total_tokens"] == \
        max(1, cfg.LLM_CONTEXT_WINDOW - cfg.LLM_RESERVED_OUTPUT_TOKENS)


# ── Phase 14 阶段二：archive_and_compact / next_seq / read_full ──

def test_store_next_seq_empty(store):
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    assert store.next_seq("s1") == 0


def test_store_next_seq_after_append(store):
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    store.append("s1", {"seq": 0, "role": "user", "content": "hi"})
    store.append("s1", {"seq": 1, "role": "assistant", "content": "hello"})
    assert store.next_seq("s1") == 2


def test_store_next_seq_monotonic_with_compacted(store):
    """next_seq 应覆盖 compacted 行，防止与已压缩消息序号冲突。"""
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    for i in range(5):
        store.append("s1", {"seq": i, "role": "user", "content": f"m{i}"})

    # 压缩 seq 1,2 为 compacted；seq 0 保留；插入摘要 seq=0.5
    store.archive_and_compact(
        "s1",
        compacted_seqs={1, 2},
        summary_rows=[{"seq": 1.5, "role": "assistant", "content": "summary"}],
        new_active_msgs=[],
    )
    # 压缩后最大 seq = max(0, 1.5, 3, 4, 1, 2) = 4 → next = 5
    assert store.next_seq("s1") == 5


def test_store_archive_and_compact_marks_mid(store):
    """中段消息变 compacted，尾部保持 active，摘要 seq 是小数。"""
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    for i in range(6):
        store.append("s1", {"seq": i, "role": "user", "content": f"m{i}"})

    store.archive_and_compact(
        "s1",
        compacted_seqs={1, 2, 3},                                     # 中段标 compacted
        summary_rows=[{"seq": 1.5, "role": "assistant", "content": "summary of 1-3"}],
        new_active_msgs=[{"role": "user", "content": "new msg"}],     # 本轮新消息
    )

    _header, active = store.read("s1")

    # active 视图：seq 0(user), 1.5(summary), 4(user), 5(user), 6(new msg)
    assert any(r["content"] == "summary of 1-3" for r in active)
    assert any(r["content"] == "new msg" for r in active)
    assert all(r["content"] not in ("m1", "m2", "m3") for r in active)

    # seq 0 / 4 / 5 仍在 active
    seqs = [r["seq"] for r in active]
    assert 0 in seqs
    assert 4 in seqs
    assert 5 in seqs


def test_store_read_full_contains_compacted(store):
    """read_full 返回 active + compacted，供审计。"""
    store.write_header("s1", {"version": 1, "id": "s1",
                               "created_at": "2026-01-01T00:00:00+00:00"})
    for i in range(4):
        store.append("s1", {"seq": i, "role": "user", "content": f"m{i}"})

    store.archive_and_compact(
        "s1",
        compacted_seqs={1, 2},
        summary_rows=[{"seq": 1.5, "role": "assistant", "content": "summary"}],
        new_active_msgs=[],
    )

    # active 视图只有 3 条（seq 0, 1.5, 3）
    _, active = store.read("s1")
    assert len(active) == 3

    # 完整视图包含全部 6 行（0,1,1.5,2,3）
    full = store.read_full("s1")
    assert len(full) == 5
    compacted_rows = [r for r in full if r["compacted"]]
    assert len(compacted_rows) == 2


def test_manager_append_turn_seq_no_conflict_after_compact(mgr):
    """append_turn 在有 compacted 行后 seq 单调递增，不冲突。"""
    sid = mgr.create_session()
    mgr.append_turn(sid, [
        {"role": "user", "content": "t0"},
        {"role": "assistant", "content": "r0"},
    ])
    # 手动压缩 seq 0（标 compacted）
    mgr.store.archive_and_compact(
        sid,
        compacted_seqs={0},
        summary_rows=[{"seq": 0.5, "role": "assistant", "content": "summary"}],
        new_active_msgs=[],
    )
    # 再追加一轮：seq 应从 max(0,0.5,1) + 1 = 2 开始，不是 len(active)=2（碰巧相同）
    # 但若 active 只剩 {0.5, 1}，len=2，next_seq=2，两者一致；
    # 关键是即使 len(active) 不同，next_seq 仍正确：
    mgr.append_turn(sid, [{"role": "user", "content": "t1"}])
    _, active = mgr.store.read(sid)
    seqs = sorted(r["seq"] for r in active)
    # 不应有重复 seq
    assert len(seqs) == len(set(seqs))
    # 最新消息 seq 必须 > 所有已有 seq
    assert seqs[-1] > 1  # max existing was 1 (seq of "r0")


def test_manager_load_messages_contains_seq(mgr):
    """load_messages 返回带 seq 的活跃消息，供 agent 打 _seq 标记。"""
    sid = mgr.create_session()
    mgr.append_turn(sid, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])
    msgs = mgr.load_messages(sid)
    assert len(msgs) == 2
    assert all("seq" in m for m in msgs)
    assert msgs[0]["seq"] == 0
    assert msgs[1]["seq"] == 1


def test_manager_load_full_history(mgr):
    """load_full_history 在压缩后仍能返回被压缩行。"""
    sid = mgr.create_session()
    mgr.append_turn(sid, [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ])
    mgr.store.archive_and_compact(
        sid,
        compacted_seqs={0},
        summary_rows=[{"seq": 0.5, "role": "assistant", "content": "sum"}],
        new_active_msgs=[],
    )
    full = mgr.load_full_history(sid)
    assert any(r.get("compacted") for r in full)
    assert any(r.get("content") == "sum" for r in full)


def test_get_session_display_excludes_summary(mgr):
    """get_session 返回的 messages 不含摘要行，只含原始消息。"""
    sid = mgr.create_session()
    mgr.append_turn(sid, [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ])
    # 压缩中间两条，插入摘要
    mgr.store.archive_and_compact(
        sid,
        compacted_seqs={1, 2},
        summary_rows=[{"seq": 1.5, "role": "assistant", "content": "[摘要]"}],
        new_active_msgs=[],
    )
    session = mgr.get_session(sid)
    contents = [m["content"] for m in session["messages"]]
    # 原始消息都在
    assert "q1" in contents
    assert "a1" in contents
    assert "q2" in contents
    assert "a2" in contents
    # 摘要不在
    assert "[摘要]" not in contents


# ── P0 回归：header 写入必须是 UPSERT，不得连带删消息 ───────────────

_CREATED = "2026-01-01T00:00:00+00:00"


def test_write_header_twice_keeps_messages(store):
    """INSERT OR REPLACE 会先 DELETE 冲突行 → 触发 messages 的 ON DELETE CASCADE。

    「改个标题」曾等于「删光这个会话的全部聊天记录」，且静默无异常。
    """
    store.write_header("s1", {"title": "旧标题", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "user", "content": "hello"})
    store.append("s1", {"seq": 1, "role": "assistant", "content": "hi"})

    store.write_header("s1", {"title": "新标题"})

    header, records = store.read("s1")
    assert header["title"] == "新标题"
    assert len(records) == 2, "改写 header 不得删除该会话消息"
    assert store.next_seq("s1") == 2


def test_write_header_preserves_created_at_and_state(store):
    """重复写 header 不应刷新 created_at，也不应重置压缩状态。"""
    store.write_header("s1", {"title": "t", "created_at": _CREATED})
    store.save_state("s1", compressed_once=True, last_prompt_tokens=1234)

    store.write_header("s1", {"title": "t2"})   # 这次不带 created_at

    header, _ = store.read("s1")
    assert header["created_at"] == _CREATED
    assert header["compressed_once"] is True
    assert header["last_prompt_tokens"] == 1234


def test_write_header_keeps_workspace_when_omitted(store):
    store.write_header("s1", {"title": "t", "created_at": _CREATED, "workspace_id": "ws-1"})
    store.write_header("s1", {"title": "t2"})
    assert store.read("s1")[0]["workspace_id"] == "ws-1"


def test_create_session_with_existing_id_keeps_history(mgr):
    """create_session 是公开 API（session/create RPC 会透传 session_id）。"""
    sid = mgr.create_session(title="原会话")
    mgr.append_turn(sid, [{"role": "user", "content": "重要内容"}])

    again = mgr.create_session(title="重复建", session_id=sid)
    assert again == sid
    assert len(mgr.load_messages(sid)) == 1, "用已有 id 建会话不得清空历史"


# ── P0 回归：外键开关必须 per-connection 生效 ─────────────────────

def _msg_count(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def test_foreign_keys_active_on_reopened_db(tmp_path):
    """外键是 per-connection 设置，且 SQLite 默认 OFF。

    旧实现把 PRAGMA foreign_keys=ON 写在 _DDL 里，只在首次建库执行——第二次打开
    同一库时外键是关的，delete_session 只删 header、消息全变孤儿行。
    """
    db = tmp_path / "fk.db"
    first = SqliteSessionStore(db)
    first.write_header("s1", {"title": "t", "created_at": _CREATED})
    first.append("s1", {"seq": 0, "role": "user", "content": "x"})
    assert _msg_count(db) == 1

    second = SqliteSessionStore(db)          # 同一个库，第二次打开
    assert second._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    second.delete("s1")
    assert _msg_count(db) == 0, "删除会话必须级联清掉消息"


def test_orphan_messages_cleaned_on_open(tmp_path):
    """旧版本外键失效期间留下的孤儿行，在下次打开时清理掉。"""
    db = tmp_path / "orphan.db"
    store = SqliteSessionStore(db)
    store.write_header("s1", {"title": "t", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "user", "content": "x"})

    raw = sqlite3.connect(str(db))           # 裸连接默认外键 OFF，可写入孤儿行
    raw.execute(
        "INSERT INTO messages(session_id, seq, role, content, ts)"
        " VALUES ('ghost', 0, 'user', 'orphan', 0)"
    )
    raw.commit()
    raw.close()
    assert _msg_count(db) == 2

    SqliteSessionStore(db)                   # 重新打开 → 触发清理
    assert _msg_count(db) == 1


# ── P1 回归：老版本库迁移后 schema 必须完整 ───────────────────────

def _make_legacy_db(path: Path, version: int) -> None:
    """造一个"那个版本的真实结构"的老库（按声明版本补上当时已有的列）。

    不能所有版本都套同一份最老的表结构：迁移链只跑 `version > user_version`
    的步骤，若把 v7 的库造成"缺 is_summary"的形态，就不存在任何一步会去补它，
    这不是迁移的 bug，而是造出来的库本身不自洽。
    """
    msg_extra = (
        ",\n        is_summary INTEGER NOT NULL DEFAULT 0" if version >= 5 else ""
    )
    ses_extra = (
        ",\n        archived INTEGER NOT NULL DEFAULT 0" if version >= 8 else ""
    )
    conn = sqlite3.connect(str(path))
    conn.executescript(f"""
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',
        workspace_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        compressed_once INTEGER NOT NULL DEFAULT 0, last_prompt_tokens INTEGER{ses_extra});
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq REAL NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT,
        tool_call_id TEXT, ts INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0{msg_extra});
    """)
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("version", [1, 2, 3, 4, 7])
def test_legacy_db_migration_adds_new_columns(tmp_path, version):
    """任何入口版本升上来都要补齐新列（is_summary / archived）。

    旧实现的 ver < 3 分支只删旧列就标 v5，跳过了加列这一步，read_display 直接报错。
    v7 是"还没有 archived 列"的最近一版，专门列进来钉住 v7 → v8 这条路。
    """
    db = tmp_path / f"old{version}.db"
    _make_legacy_db(db, version)

    store = SqliteSessionStore(db)

    msg_cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(messages)")}
    assert "is_summary" in msg_cols, f"v{version} 迁移后缺 is_summary 列"
    ses_cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(sessions)")}
    assert "archived" in ses_cols, f"v{version} 迁移后缺 archived 列"
    # 断言常量而非字面量：schema 每次升版都要改这行的话，迟早改漏
    ver = sqlite3.connect(str(db)).execute("PRAGMA user_version").fetchone()[0]
    assert ver == _SCHEMA_VERSION

    store.write_header("s1", {"title": "t", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "user", "content": "hi"})
    assert store.read_display("s1")[0]["content"] == "hi"
    # 老库升上来的会话默认未归档（DEFAULT 0），不会"凭空消失"在侧栏之外
    assert store.read_header("s1")["archived"] is False


def test_migration_is_idempotent(tmp_path):
    """迁移可重复执行：反复打开同一个库不应报错，也不该改动数据。"""
    db = tmp_path / "reopen.db"
    first = SqliteSessionStore(db)
    first.write_header("s1", {"title": "t", "created_at": _CREATED})
    first.append("s1", {"seq": 0, "role": "user", "content": "x"})

    for _ in range(3):
        SqliteSessionStore(db)

    assert _msg_count(db) == 1
    header, records = SqliteSessionStore(db).read("s1")
    assert header["title"] == "t" and len(records) == 1


# ── 会话归档（v8）：从侧栏收起，但不删数据 ────────────────────────

def test_archive_hides_from_list_but_keeps_data(store):
    store.write_header("s1", {"title": "a", "created_at": _CREATED})
    store.write_header("s2", {"title": "b", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "user", "content": "keep me"})

    assert store.set_archived("s1") is True

    assert [h["id"] for h in store.list_headers()] == ["s2"]
    # 归档不是删除：会话还在、消息一条不少
    assert store.exists("s1")
    assert store.read_display("s1")[0]["content"] == "keep me"


def test_include_archived_lists_them_with_flag(store):
    store.write_header("s1", {"title": "a", "created_at": _CREATED})
    store.set_archived("s1")

    rows = store.list_headers(include_archived=True)
    assert [h["id"] for h in rows] == ["s1"]
    # 带上标记，前端才知道要灰显（而不是把它当成普通会话）
    assert rows[0]["archived"] is True


def test_unarchive_restores_to_list(store):
    store.write_header("s1", {"title": "a", "created_at": _CREATED})
    store.set_archived("s1")
    assert store.set_archived("s1", False) is True

    assert [h["id"] for h in store.list_headers()] == ["s1"]
    assert store.read_header("s1")["archived"] is False


def test_set_archived_unknown_session_returns_false(store):
    assert store.set_archived("nope") is False


def test_archived_session_still_searchable(store):
    """搜索是"找东西"的手段——归档会话搜不到，就等于真找不回了。"""
    store.write_header("s1", {"title": "a", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "user", "content": "归档也要能搜到"})
    store.set_archived("s1")

    assert [h["session_id"] for h in store.search("归档")] == ["s1"]


def test_new_message_auto_unarchives(store):
    """归档是收纳动作，不是状态锁：用户回来接着说话，它就该自动浮出来。"""
    store.write_header("s1", {"title": "a", "created_at": _CREATED})
    store.set_archived("s1")

    store.append("s1", {"seq": 0, "role": "user", "content": "我还有话要说"})

    assert store.read_header("s1")["archived"] is False
    assert [h["id"] for h in store.list_headers()] == ["s1"]


def test_manager_set_archived_and_list(mgr):
    """归档在 manager 层也要能透传，且 list_sessions 支持 include_archived。"""
    sid = mgr.create_session(title="归档我")

    assert mgr.set_archived(sid) is True
    assert mgr.list_sessions() == []

    listed = mgr.list_sessions(include_archived=True)
    assert [s["id"] for s in listed] == [sid]
    assert listed[0]["archived"] is True

    assert mgr.set_archived(sid, False) is True
    assert [s["id"] for s in mgr.list_sessions()] == [sid]

