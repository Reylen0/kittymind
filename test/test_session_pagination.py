"""会话消息分页（store / manager 层）。

分页的三条不变式：
  1. 页与页由 seq 区间严格切分：拼接起来 == 全量视图，不重叠、不遗漏；
  2. 页首落在「一批工具调用」中间时向前补齐到发起行，同批工具不被拆成两个折叠组；
  3. tool 行自带名字/入参（随行下发），因此页尾切开也不影响渲染。
"""

import pytest

from kittymind.session.manager import SessionManager


@pytest.fixture
def mgr(tmp_path):
    return SessionManager(db_path=tmp_path / "page.db")


def _seed(mgr, sid) -> list[dict]:
    """一段典型会话（seq 0..10）：两批工具调用 + 纯文本轮次。"""
    turn = [
        {"role": "user", "content": "q1"},                                    # 0
        {"role": "assistant", "content": None,                                # 1
         "tool_calls": [
             {"id": "c1", "function": {"name": "glob", "arguments": '{"p":"*.txt"}'}},
             {"id": "c2", "function": {"name": "file_read", "arguments": '{"p":"a.txt"}'}},
         ]},
        {"role": "tool", "content": "r1", "tool_call_id": "c1"},              # 2
        {"role": "tool", "content": "r2", "tool_call_id": "c2"},              # 3
        {"role": "assistant", "content": "a1"},                               # 4
        {"role": "user", "content": "q2"},                                    # 5
        {"role": "assistant", "content": None,                                # 6
         "tool_calls": [
             {"id": "c3", "function": {"name": "ls", "arguments": '{"p":"."}'}},
         ]},
        {"role": "tool", "content": "r3", "tool_call_id": "c3"},              # 7
        {"role": "assistant", "content": "a2"},                               # 8
        {"role": "user", "content": "q3"},                                    # 9
        {"role": "assistant", "content": "a3"},                               # 10
    ]
    mgr.create_session(session_id=sid)
    mgr.append_turn(sid, turn)
    return turn


# ── 切页正确性 ────────────────────────────────────────────────────

def test_page_returns_latest_slice(mgr):
    sid = "s1"
    _seed(mgr, sid)
    page = mgr.get_session(sid, limit=4)
    # 4 条里最早一条恰是 tool 行（seq 7）→ 顺带把发起行 6 补齐进来，故实得 5 条
    assert [m["seq"] for m in page["messages"]] == [6, 7, 8, 9, 10]
    assert page["has_more"] is True
    assert page["cursor"] == 6


def test_paging_forward_matches_full_view(mgr):
    """逐页向前翻：拼接结果与全量视图一致，且没有任何一条重复出现。"""
    sid = "s2"
    _seed(mgr, sid)
    full = [m["seq"] for m in mgr.get_session(sid)["messages"]]

    collected: list[int] = []
    cursor = None
    for _ in range(20):
        page = mgr.get_session(sid, limit=3, before_seq=cursor)
        collected = [m["seq"] for m in page["messages"]] + collected
        if not page["has_more"]:
            break
        cursor = page["cursor"]
    else:
        pytest.fail("翻页未收敛（has_more 一直为真）")

    assert collected == full
    assert len(collected) == len(set(collected))   # 页与页之间不得重叠


def test_page_head_expands_to_tool_call_start(mgr):
    """页首是批中段的 tool 行时向前补齐到发起行，同批工具调用不被拆到两页。"""
    sid = "s3"
    _seed(mgr, sid)
    # limit=4 + 游标 7：截到 [3,4,5,6]，最早一条 3 是 tool → 补齐出 2、1
    page = mgr.get_session(sid, limit=4, before_seq=7)
    assert [m["seq"] for m in page["messages"]] == [1, 2, 3, 4, 5, 6]
    assert page["messages"][0]["role"] == "assistant"
    assert [c["id"] for c in page["messages"][0]["tool_calls"]] == ["c1", "c2"]
    assert page["cursor"] == 1


def test_page_tail_not_trimmed(mgr):
    """页尾切在发起行与工具结果之间时不裁剪：发起行的 tool_calls 仍完整。

    这类切口只可能出现在显式指定游标的调用上（正常翻页始终从最新往早取，
    工具结果在更晚的一侧）。它必须是无害的——工具行自带名字，不需要发起行同页。
    """
    sid = "s5"
    _seed(mgr, sid)
    page = mgr.get_session(sid, limit=1, before_seq=7)
    assert [m["seq"] for m in page["messages"]] == [6]
    assert page["messages"][0]["tool_calls"][0]["id"] == "c3"
    assert page["has_more"] is True


def test_tool_rows_carry_owner_name_and_args(mgr):
    """工具行的名字/入参随行下发（前端不再跨消息配对 tool_call_id）。"""
    sid = "s4"
    _seed(mgr, sid)
    by_seq = {m["seq"]: m for m in mgr.get_session(sid, limit=100)["messages"]}
    assert by_seq[2]["tool_name"] == "glob"
    assert by_seq[2]["tool_args"] == '{"p":"*.txt"}'
    assert by_seq[3]["tool_name"] == "file_read"
    assert by_seq[7]["tool_name"] == "ls"
    assert "tool_name" not in by_seq[0]        # 非 tool 行不带


def test_compacted_rows_still_paged(mgr):
    """被压缩的原始消息仍在展示视图里，摘要行不出现，且分页能逐页翻到。"""
    sid = "s6"
    _seed(mgr, sid)
    mgr.store.archive_and_compact(
        sid,
        compacted_seqs={0, 1, 2, 3},
        summary_rows=[{"seq": 1.5, "role": "assistant", "content": "[摘要]"}],
        new_active_msgs=[{"role": "user", "content": "q4"},
                         {"role": "assistant", "content": "a4"}],
    )
    full = [m["seq"] for m in mgr.get_session(sid)["messages"]]
    collected: list = []
    cursor = None
    for _ in range(20):
        page = mgr.get_session(sid, limit=2, before_seq=cursor)
        assert all(m["content"] != "[摘要]" for m in page["messages"])
        collected = [m["seq"] for m in page["messages"]] + collected
        if not page["has_more"]:
            break
        cursor = page["cursor"]
    assert collected == full
    assert 0 in collected                      # compacted 的原始行没被丢掉


# ── 兼容与边界 ────────────────────────────────────────────────────

def test_full_mode_unchanged(mgr):
    """不传 limit 时保持旧语义：全量消息，且不带分页字段。"""
    sid = "s7"
    _seed(mgr, sid)
    full = mgr.get_session(sid)
    assert len(full["messages"]) == 11
    assert "has_more" not in full
    assert "cursor" not in full


def test_cursor_before_oldest_returns_empty(mgr):
    sid = "s8"
    _seed(mgr, sid)
    page = mgr.get_session(sid, limit=5, before_seq=0)
    assert page["messages"] == []
    assert page["has_more"] is False
    assert page["cursor"] is None


def test_get_session_header_never_reads_messages(mgr):
    """get_session_header 只查 sessions 表：agent 每轮解析工作目录都会调用它。"""
    sid = "s9"
    _seed(mgr, sid)
    for name in ("read", "read_display", "read_display_page"):
        setattr(mgr.store, name, lambda _n=name, *a, **k: pytest.fail(f"{_n} 不应被调用"))
    header = mgr.get_session_header(sid)
    assert header["id"] == sid
    assert header["workspace_id"] is None
    assert header["used_tokens"] == 0
