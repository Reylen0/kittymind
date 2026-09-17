"""server RPC 路由测试（无需 LLM，mock agent）"""

import asyncio
import json
import pytest

from unittest.mock import AsyncMock, MagicMock, patch
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
