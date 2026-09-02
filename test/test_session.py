"""SessionStore / SessionManager 单元测试（无需 LLM）"""

import json
from pathlib import Path

import pytest

from baseagent.session.store import SessionStore
from baseagent.session.manager import SessionManager


# ──────────────────────────────────────────────────────────────
# SessionStore
# ──────────────────────────────────────────────────────────────

def test_store_write_and_read(tmp_path):
    store = SessionStore(tmp_path)
    store.write_header("s1", {"version": 1, "id": "s1", "title": "test"})
    store.append("s1", {"seq": 0, "role": "user", "content": "hello"})
    store.append("s1", {"seq": 1, "role": "assistant", "content": "hi"})

    header, records = store.read("s1")
    assert header["id"] == "s1"
    assert len(records) == 2
    assert records[0]["content"] == "hello"


def test_store_torn_tail_recovery(tmp_path):
    """写入一条完整行 + 一条不完整行，应只读到完整的那条。"""
    store = SessionStore(tmp_path)
    store.write_header("s1", {"version": 1, "id": "s1"})
    store.append("s1", {"seq": 0, "role": "user", "content": "ok"})

    # 手动追加一条 torn tail（截断的 JSON）
    path = store._path("s1")
    with path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 1, "role": "assistan')  # 故意截断

    header, records = store.read("s1")
    assert len(records) == 1  # torn tail 被跳过
    assert records[0]["content"] == "ok"


def test_store_exists_and_delete(tmp_path):
    store = SessionStore(tmp_path)
    assert not store.exists("s1")
    store.write_header("s1", {"version": 1, "id": "s1"})
    assert store.exists("s1")
    store.delete("s1")
    assert not store.exists("s1")


def test_store_list_ids(tmp_path):
    store = SessionStore(tmp_path)
    store.write_header("s1", {"version": 1, "id": "s1"})
    store.write_header("s2", {"version": 1, "id": "s2"})
    ids = store.list_ids()
    assert set(ids) == {"s1", "s2"}


# ──────────────────────────────────────────────────────────────
# SessionManager
# ──────────────────────────────────────────────────────────────

def test_manager_create_and_get(tmp_path):
    mgr = SessionManager(data_dir=tmp_path)
    sid = mgr.create_session(title="聊天")
    session = mgr.get_session(sid)
    assert session is not None
    assert session["header"]["title"] == "聊天"
    assert session["messages"] == []


def test_manager_list_sessions(tmp_path):
    mgr = SessionManager(data_dir=tmp_path)
    mgr.create_session(title="A")
    mgr.create_session(title="B")
    sessions = mgr.list_sessions()
    assert len(sessions) == 2


def test_manager_delete_session(tmp_path):
    mgr = SessionManager(data_dir=tmp_path)
    sid = mgr.create_session()
    mgr.delete_session(sid)
    assert mgr.get_session(sid) is None


def test_manager_append_turn(tmp_path):
    mgr = SessionManager(data_dir=tmp_path)
    sid = mgr.create_session(title="test")

    turn = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]
    mgr.append_turn(sid, turn, "你好")

    session = mgr.get_session(sid)
    assert len(session["messages"]) == 2
    assert session["messages"][0]["seq"] == 0
    assert session["messages"][1]["seq"] == 1


def test_manager_append_turn_creates_session_if_missing(tmp_path):
    """session 不存在时 append_turn 应自动创建。"""
    mgr = SessionManager(data_dir=tmp_path)
    turn = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    mgr.append_turn("auto-sid", turn, "hello")
    session = mgr.get_session("auto-sid")
    assert session is not None
    assert session["header"]["title"] == "hello"


def test_manager_load_history(tmp_path):
    mgr = SessionManager(data_dir=tmp_path)
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
    # seq 字段不应出现在返回结果里
    assert "seq" not in history[0]


def test_manager_generate_title(tmp_path):
    mgr = SessionManager(data_dir=tmp_path)
    assert mgr.generate_title("短标题") == "短标题"
    long_text = "这是一段超过二十个字符的标题用于测试截断功能"
    title = mgr.generate_title(long_text)
    assert title.endswith("...")
    assert len(title) == 23  # 20 chars + "..."


def test_manager_multiple_turns_seq(tmp_path):
    """多轮对话的 seq 应连续递增。"""
    mgr = SessionManager(data_dir=tmp_path)
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

    session = mgr.get_session(sid)
    seqs = [m["seq"] for m in session["messages"]]
    assert seqs == [0, 1, 2, 3]
