"""SqliteSessionStore / SessionManager 单元测试（无需 LLM）"""

from pathlib import Path

import pytest

from kittymind.session.store import SqliteSessionStore
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
    assert state.get("context_ratio", 0.0) == 0.0

    store.save_state("s1",
                     compressed_once=True,
                     last_prompt_tokens=5000,
                     context_ratio=0.42)
    state2 = store.get_state("s1")
    assert bool(state2["compressed_once"]) is True
    assert state2["last_prompt_tokens"] == 5000
    assert abs(state2["context_ratio"] - 0.42) < 1e-6


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
    assert session["messages"][0]["seq"] == 0
    assert session["messages"][1]["seq"] == 1


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
    seqs = [m["seq"] for m in mgr.get_session(sid)["messages"]]
    assert seqs == [0, 1, 2, 3]


def test_manager_session_state_roundtrip(mgr):
    sid = mgr.create_session()
    mgr.save_session_state(sid,
                           compressed_once=True,
                           last_prompt_tokens=8192,
                           context_ratio=0.73)
    state = mgr.get_session_state(sid)
    assert bool(state["compressed_once"]) is True
    assert state["last_prompt_tokens"] == 8192
    assert abs(state["context_ratio"] - 0.73) < 1e-6


def test_manager_get_session_includes_context_ratio(mgr):
    sid = mgr.create_session()
    mgr.save_session_state(sid, context_ratio=0.55)
    session = mgr.get_session(sid)
    assert abs(session["header"]["context_ratio"] - 0.55) < 1e-6
