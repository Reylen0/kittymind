"""server RPC 路由测试（无需 LLM，mock agent）"""

import asyncio
import json

from unittest.mock import MagicMock
from server.rpc_handler import RpcHandler


def make_mock_agent(tmp_path):
    """创建带 SessionManager 的 mock KittyAgent。"""
    from kittymind.events.bus import EventBus
    from kittymind.session.manager import SessionManager
    from kittymind.agent import KittyAgent

    agent = MagicMock(spec=KittyAgent)
    agent.name = "kitty"
    agent.llm = MagicMock()
    agent.llm.model = "test-model"
    agent.event_bus = EventBus()
    agent.session_manager = SessionManager(db_path=tmp_path / "test.db")

    # async_stream_run yields nothing (instant done)
    async def _noop_stream(*args, **kwargs):
        return
        yield  # make it an async generator

    agent.async_stream_run = _noop_stream
    return agent


class FakeWs:
    """记录所有发出消息的假 WebSocket。"""
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data: str):
        self.sent.append(json.loads(data))


# ──────────────────────────────────────────────────────────────
# session/*
# ──────────────────────────────────────────────────────────────

async def test_session_create_and_list(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "session/create", "params": {"title": "测试会话"}})
    assert ws.sent[0]["id"] == 1
    sid = ws.sent[0]["result"]["session_id"]
    assert ws.sent[0]["result"]["title"] == "测试会话"

    ws.sent.clear()
    await handler.dispatch({"id": 2, "method": "session/list", "params": {}})
    sessions = ws.sent[0]["result"]
    assert any(s["id"] == sid for s in sessions)


async def test_session_get(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "session/create", "params": {"title": "foo"}})
    sid = ws.sent[0]["result"]["session_id"]

    ws.sent.clear()
    await handler.dispatch({"id": 2, "method": "session/get", "params": {"session_id": sid}})
    assert ws.sent[0]["result"]["header"]["id"] == sid


async def test_session_get_paged(tmp_path):
    """session/get 带 limit → 只回一页，并给出 has_more/游标供继续前翻。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager
    sid = mgr.create_session(title="分页")
    mgr.append_turn(sid, [{"role": "user", "content": f"m{i}"} for i in range(10)])

    ws.sent.clear()
    await handler.dispatch({"id": 1, "method": "session/get",
                            "params": {"session_id": sid, "limit": 4}})
    page = ws.sent[0]["result"]
    assert [m["seq"] for m in page["messages"]] == [6, 7, 8, 9]
    assert page["has_more"] is True
    assert page["cursor"] == 6

    ws.sent.clear()
    await handler.dispatch({"id": 2, "method": "session/get",
                            "params": {"session_id": sid, "limit": 4,
                                       "before_seq": page["cursor"]}})
    older = ws.sent[0]["result"]
    assert [m["seq"] for m in older["messages"]] == [2, 3, 4, 5]
    assert older["has_more"] is True
    assert older["cursor"] == 2


async def test_session_get_limit_invalid_falls_back_to_full(tmp_path):
    """limit 非法（0 / 负数 / 非数字）时回退全量，而不是报错或返回空页。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager
    sid = mgr.create_session(title="分页")
    mgr.append_turn(sid, [{"role": "user", "content": f"m{i}"} for i in range(6)])

    for bad in (0, -3, "abc", None):
        ws.sent.clear()
        await handler.dispatch({"id": 1, "method": "session/get",
                                "params": {"session_id": sid, "limit": bad}})
        result = ws.sent[0]["result"]
        assert len(result["messages"]) == 6
        assert "has_more" not in result

    # 超大 limit 被夹到上限（此处未触顶，仅验证不会因越界而失败）
    ws.sent.clear()
    await handler.dispatch({"id": 2, "method": "session/get",
                            "params": {"session_id": sid, "limit": 10_000}})
    assert len(ws.sent[0]["result"]["messages"]) == 6


# ── session/search ────────────────────────────────────────────────

async def _search(handler, ws, params):
    ws.sent.clear()
    await handler.dispatch({"id": 1, "method": "session/search", "params": params})
    return ws.sent[0]


async def test_session_search_groups_hits_by_session(tmp_path):
    """跨会话搜索：结果按会话分组，每组带标题与命中片段。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager

    s1 = mgr.create_session(title="聊压缩的会话")
    mgr.append_turn(s1, [{"role": "user", "content": "上下文压缩是怎么做的"}])
    s2 = mgr.create_session(title="聊别的会话")
    mgr.append_turn(s2, [{"role": "user", "content": "这里也提到了压缩"}])

    groups = (await _search(handler, ws, {"query": "压缩"}))["result"]

    assert {g["session_id"] for g in groups} == {s1, s2}
    group = next(g for g in groups if g["session_id"] == s1)
    assert group["title"] == "聊压缩的会话"
    hit = group["hits"][0]
    start, end = hit["marks"][0]
    assert hit["text"][start:end] == "压缩"
    assert hit["seq"] == 0


async def test_session_search_can_narrow_to_one_session(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager

    s1 = mgr.create_session(title="A")
    mgr.append_turn(s1, [{"role": "user", "content": "都提到了缓存"}])
    s2 = mgr.create_session(title="B")
    mgr.append_turn(s2, [{"role": "user", "content": "这里也有缓存"}])

    groups = (await _search(handler, ws, {"query": "缓存", "session_id": s1}))["result"]
    assert [g["session_id"] for g in groups] == [s1]


async def test_session_search_empty_query_returns_empty_array(tmp_path):
    """输入框清空是正常状态，应当回空数组而不是 error。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager
    sid = mgr.create_session(title="t")
    mgr.append_turn(sid, [{"role": "user", "content": "内容"}])

    for blank in ("", "   ", None):
        msg = await _search(handler, ws, {"query": blank})
        assert msg["result"] == []
        assert "error" not in msg


async def test_session_search_invalid_limit_falls_back(tmp_path):
    """limit 非法时回退默认上限，与 session/get 的既有口径一致（不报错）。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager
    sid = mgr.create_session(title="t")
    mgr.append_turn(sid, [{"role": "user", "content": "谈到了索引"}])

    for bad in (0, -3, "abc", None, True):
        msg = await _search(handler, ws, {"query": "索引", "limit": bad})
        assert "error" not in msg
        assert len(msg["result"][0]["hits"]) == 1


async def test_session_search_hostile_query_does_not_error(tmp_path):
    """用户打的 FTS5 语法字符必须被当字面量，不能让 MATCH 报错回前端。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    mgr = agent.session_manager
    sid = mgr.create_session(title="t")
    mgr.append_turn(sid, [{"role": "user", "content": "正常内容"}])

    for hostile in ('foo*', 'a AND b', '"', '((', 'NEAR(x y)'):
        msg = await _search(handler, ws, {"query": hostile})
        assert "error" not in msg, f"{hostile!r} 让 RPC 报错了"


async def test_session_get_not_found(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "session/get", "params": {"session_id": "nonexistent"}})
    assert "error" in ws.sent[0]


async def test_session_delete(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "session/create", "params": {}})
    sid = ws.sent[0]["result"]["session_id"]

    ws.sent.clear()
    await handler.dispatch({"id": 2, "method": "session/delete", "params": {"session_id": sid}})
    assert ws.sent[0]["result"]["deleted"] is True

    ws.sent.clear()
    await handler.dispatch({"id": 3, "method": "session/get", "params": {"session_id": sid}})
    assert "error" in ws.sent[0]


# ──────────────────────────────────────────────────────────────
# agent/status
# ──────────────────────────────────────────────────────────────

async def test_agent_status(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "agent/status", "params": {}})
    result = ws.sent[0]["result"]
    assert result["name"] == "kitty"
    assert result["model"] == "test-model"
    assert result["running_sessions"] == []


# ──────────────────────────────────────────────────────────────
# turn/cancel (空任务)
# ──────────────────────────────────────────────────────────────

async def test_turn_cancel_no_task(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "turn/cancel", "params": {"session_id": "s1"}})
    assert ws.sent[0]["result"]["cancelled"] is False


# ──────────────────────────────────────────────────────────────
# unknown method
# ──────────────────────────────────────────────────────────────

async def test_unknown_method(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "foo/bar", "params": {}})
    assert "error" in ws.sent[0]


# ──────────────────────────────────────────────────────────────
# turn/run — 参数校验
# ──────────────────────────────────────────────────────────────

async def test_turn_run_missing_session_id(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "turn/run", "params": {"text": "hi"}})
    assert "error" in ws.sent[0]


async def test_turn_run_missing_text(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "turn/run", "params": {"session_id": "s1"}})
    assert "error" in ws.sent[0]


# ──────────────────────────────────────────────────────────────
# turn/run 重复调用 —— 任务登记竞态回归
# ──────────────────────────────────────────────────────────────

async def test_turn_run_twice_keeps_new_task_registered(tmp_path):
    """回归：同一 session 第二次 turn/run 取消旧任务后，旧任务的 finally
    曾无条件 pop(_tasks[session_id])，把新登记的条目误删，导致 turn/cancel 失效。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    entered = asyncio.Event()

    async def _slow_stream(session_id, text, **kwargs):
        entered.set()
        await asyncio.sleep(30)
        yield "never"

    agent.async_stream_run = _slow_stream
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "turn/run",
                            "params": {"session_id": "s1", "text": "first"}})
    await entered.wait()
    await asyncio.sleep(0)
    first_task = handler._tasks["s1"]

    # 第二次 turn/run：取消旧任务 → 登记新任务
    await handler.dispatch({"id": 2, "method": "turn/run",
                            "params": {"session_id": "s1", "text": "second"}})
    await asyncio.sleep(0.05)  # 给被取消任务的 finally 执行机会

    assert first_task.done(), "旧任务应已被取消"
    assert "s1" in handler._tasks, "新任务的登记条目被旧任务的 finally 误删"
    assert handler._tasks["s1"] is not first_task

    # 停止按钮仍然有效
    ws.sent.clear()
    await handler.dispatch({"id": 3, "method": "turn/cancel", "params": {"session_id": "s1"}})
    assert ws.sent[-1]["result"]["cancelled"] is True


async def test_turn_run_finished_task_removes_registration(tmp_path):
    """正常跑完的任务应把自己的登记摘掉，不留悬挂条目。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "turn/run",
                            "params": {"session_id": "s1", "text": "hi"}})
    for _ in range(5):
        await asyncio.sleep(0)

    assert handler._tasks == {}


# ──────────────────────────────────────────────────────────────
# workspace/delete
# ──────────────────────────────────────────────────────────────

async def test_workspace_delete_moves_sessions_not_deletes(tmp_path):
    """删工作区只解除会话归属，会话本身必须保留（移回「对话」分组）。"""
    from kittymind.workspace.manager import WorkspaceManager

    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    agent.workspace_manager = WorkspaceManager(tmp_path / "ws.json")
    handler = RpcHandler(agent, ws)

    created = agent.workspace_manager.create_workspace("demo", str(tmp_path))
    ws_id = created["id"]

    sid = agent.session_manager.create_session(title="工作区内会话", workspace_id=ws_id)
    agent.session_manager.append_turn(sid, [{"role": "user", "content": "hi"}], [])

    await handler.dispatch({"id": 1, "method": "workspace/delete",
                            "params": {"workspace_id": ws_id}})
    result = ws.sent[0]["result"]
    assert result["deleted"] is True
    assert result["moved_sessions"] == 1

    # 会话还在，workspace_id 已清空
    assert agent.session_manager.session_exists(sid)
    header = agent.session_manager.get_session_header(sid)
    assert header["workspace_id"] is None
    # 工作区清单里也没了
    assert agent.workspace_manager.get_workspace(ws_id) is None


async def test_workspace_delete_unknown_id_errors(tmp_path):
    """删不存在的工作区应报错而不是谎报成功。"""
    from kittymind.workspace.manager import WorkspaceManager

    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    agent.workspace_manager = WorkspaceManager(tmp_path / "ws.json")
    handler = RpcHandler(agent, ws)

    await handler.dispatch({"id": 1, "method": "workspace/delete",
                            "params": {"workspace_id": "nope"}})
    assert ws.sent[0].get("error") is not None


# ──────────────────────────────────────────────────────────────
# session/archive（v8：归档 = 从列表收起，不删数据）
# ──────────────────────────────────────────────────────────────

async def test_session_archive_then_list_needs_explicit_flag(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)

    sid = agent.session_manager.create_session(title="归档我")
    agent.session_manager.append_turn(sid, [{"role": "user", "content": "hi"}], [])

    await handler.dispatch({"id": 1, "method": "session/archive",
                            "params": {"session_id": sid}})
    assert ws.sent[0]["result"] == {"ok": True, "archived": True}

    # 默认列表不含已归档会话
    await handler.dispatch({"id": 2, "method": "session/list", "params": {}})
    assert ws.sent[1]["result"] == []

    # 显式打开开关才列出来，并带 archived 标记（前端据此灰显）
    await handler.dispatch({"id": 3, "method": "session/list",
                            "params": {"include_archived": True}})
    listed = ws.sent[2]["result"]
    assert [s["id"] for s in listed] == [sid]
    assert listed[0]["archived"] is True

    # 归档不等于删除：会话和消息都还在
    assert agent.session_manager.session_exists(sid)
    assert agent.session_manager.get_session(sid)["messages"]


async def test_session_unarchive_restores_to_default_list(tmp_path):
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    sid = agent.session_manager.create_session(title="先归档再恢复")

    await handler.dispatch({"id": 1, "method": "session/archive",
                            "params": {"session_id": sid}})
    await handler.dispatch({"id": 2, "method": "session/archive",
                            "params": {"session_id": sid, "archived": False}})
    assert ws.sent[1]["result"] == {"ok": True, "archived": False}

    await handler.dispatch({"id": 3, "method": "session/list", "params": {}})
    assert [s["id"] for s in ws.sent[2]["result"]] == [sid]


async def test_session_archive_unknown_id_reports_not_ok(tmp_path):
    """如实返回：归档不存在的会话给 ok=false，不谎报成功。"""
    ws = FakeWs()
    handler = RpcHandler(make_mock_agent(tmp_path), ws)

    await handler.dispatch({"id": 1, "method": "session/archive",
                            "params": {"session_id": "nope"}})
    assert ws.sent[0]["result"] == {"ok": False, "archived": True}


async def test_session_archive_requires_session_id(tmp_path):
    ws = FakeWs()
    handler = RpcHandler(make_mock_agent(tmp_path), ws)

    await handler.dispatch({"id": 1, "method": "session/archive", "params": {}})
    assert ws.sent[0].get("error") is not None


async def test_session_list_ignores_truthy_string_for_include_archived(tmp_path):
    """`"false"` 这种字符串不该被当开关打开——真值判断会让归档会话漏出来。"""
    ws = FakeWs()
    agent = make_mock_agent(tmp_path)
    handler = RpcHandler(agent, ws)
    sid = agent.session_manager.create_session(title="归档我")
    agent.session_manager.set_archived(sid)

    await handler.dispatch({"id": 1, "method": "session/list",
                            "params": {"include_archived": "false"}})
    assert ws.sent[0]["result"] == []
