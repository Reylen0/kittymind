"""JSON-RPC 方法路由。

每个 WebSocket 连接对应一个 RpcHandler 实例。
turn/run 产生的 agent 事件通过 EventBus 订阅后实时 push 给客户端。
"""

import asyncio
import json
from typing import Any

from kittymind.events.types import (
    AGENT_CHUNK, AGENT_THINKING,
    AGENT_TOOL_CALL, AGENT_TOOL_RESULT,
    AGENT_DONE, AGENT_ERROR, AGENT_CONTEXT_USAGE,
)
from kittymind.agent import KittyAgent


class RpcHandler:
    def __init__(self, agent: KittyAgent, ws, bridge=None, loop=None) -> None:
        self.agent = agent
        self.ws = ws
        self._tasks: dict[str, asyncio.Task] = {}
        self._bridge = bridge
        if bridge is not None and loop is not None:
            bridge.set_connection(loop, self._push)

    # ──────────────────────────────────────────────────────────────
    # 消息收发
    # ──────────────────────────────────────────────────────────────

    async def dispatch(self, request: dict) -> None:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        handlers = {
            "turn/run":                self._turn_run,
            "turn/cancel":             self._turn_cancel,
            "session/create":          self._session_create,
            "session/list":            self._session_list,
            "session/get":             self._session_get,
            "session/delete":          self._session_delete,
            "agent/status":            self._agent_status,
            "workspace/list":          self._workspace_list,
            "workspace/create":        self._workspace_create,
            "tool/permission_response": self._permission_response,
        }

        fn = handlers.get(method)
        if fn is None:
            await self._error(req_id, f"unknown method: {method}")
            return

        try:
            await fn(req_id, params)
        except Exception as e:
            await self._error(req_id, str(e))

    async def cancel_all(self) -> None:
        """连接断开时取消所有正在运行的任务。"""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    async def _send(self, data: dict) -> None:
        try:
            await self.ws.send(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass  # 连接已关闭

    async def _result(self, req_id: Any, result: Any) -> None:
        await self._send({"id": req_id, "result": result})

    async def _error(self, req_id: Any, message: str) -> None:
        await self._send({"id": req_id, "error": {"message": message}})

    async def _push(self, method: str, params: dict) -> None:
        """Server push（无 id）"""
        await self._send({"method": method, "params": params})

    # ──────────────────────────────────────────────────────────────
    # turn/*
    # ──────────────────────────────────────────────────────────────

    async def _turn_run(self, req_id: Any, params: dict) -> None:
        session_id = params.get("session_id")
        text = params.get("text", "").strip()

        if not session_id:
            await self._error(req_id, "session_id is required")
            return
        if not text:
            await self._error(req_id, "text is required")
            return

        # 取消当前 session 正在运行的任务（如有）
        if session_id in self._tasks:
            self._tasks[session_id].cancel()

        bus = self.agent.event_bus

        # 注册 EventBus 转发处理器（只转发本 session 的事件）
        async def fwd_chunk(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.chunk", data)

        async def fwd_thinking(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.thinking", data)

        async def fwd_tool_call(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.tool_call", data)

        async def fwd_tool_result(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.tool_result", data)

        async def fwd_done(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.done", data)

        async def fwd_error(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.error", data)

        async def fwd_context_usage(et, data):
            if data.get("session_id") == session_id:
                await self._push("agent.context_usage", data)

        bus.subscribe(AGENT_CHUNK,         fwd_chunk)
        bus.subscribe(AGENT_THINKING,      fwd_thinking)
        bus.subscribe(AGENT_TOOL_CALL,     fwd_tool_call)
        bus.subscribe(AGENT_TOOL_RESULT,   fwd_tool_result)
        bus.subscribe(AGENT_DONE,          fwd_done)
        bus.subscribe(AGENT_ERROR,         fwd_error)
        bus.subscribe(AGENT_CONTEXT_USAGE, fwd_context_usage)

        # 立即 ack，事件通过 server push 异步推送
        await self._result(req_id, {"status": "started"})

        workspace_id = params.get("workspace_id")

        async def _run() -> None:
            try:
                async for _ in self.agent.async_stream_run(
                    session_id, text, workspace_id=workspace_id
                ):
                    pass  # 文字 delta 已由 fwd_chunk 推给客户端
            except asyncio.CancelledError:
                await self._push("agent.done", {
                    "session_id": session_id, "cancelled": True
                })
            except Exception:
                pass  # agent.error 已由 KittyAgent 通过 EventBus 推送
            finally:
                bus.unsubscribe(AGENT_CHUNK,         fwd_chunk)
                bus.unsubscribe(AGENT_THINKING,      fwd_thinking)
                bus.unsubscribe(AGENT_TOOL_CALL,     fwd_tool_call)
                bus.unsubscribe(AGENT_TOOL_RESULT,   fwd_tool_result)
                bus.unsubscribe(AGENT_DONE,          fwd_done)
                bus.unsubscribe(AGENT_ERROR,         fwd_error)
                bus.unsubscribe(AGENT_CONTEXT_USAGE, fwd_context_usage)
                self._tasks.pop(session_id, None)

        task = asyncio.create_task(_run())
        self._tasks[session_id] = task

    async def _turn_cancel(self, req_id: Any, params: dict) -> None:
        session_id = params.get("session_id")
        task = self._tasks.get(session_id)
        if task:
            task.cancel()
            await self._result(req_id, {"cancelled": True})
        else:
            await self._result(req_id, {"cancelled": False})

    # ──────────────────────────────────────────────────────────────
    # session/*
    # ──────────────────────────────────────────────────────────────

    def _mgr(self):
        return self.agent.session_manager

    async def _session_create(self, req_id: Any, params: dict) -> None:
        mgr = self._mgr()
        if not mgr:
            await self._error(req_id, "no session manager configured")
            return
        title = params.get("title")
        sid = mgr.create_session(title=title)
        await self._result(req_id, {
            "session_id": sid,
            "title": title or "新会话",
        })

    async def _session_list(self, req_id: Any, params: dict) -> None:
        mgr = self._mgr()
        await self._result(req_id, mgr.list_sessions() if mgr else [])

    async def _session_get(self, req_id: Any, params: dict) -> None:
        mgr = self._mgr()
        if not mgr:
            await self._error(req_id, "no session manager configured")
            return
        session_id = params.get("session_id")
        session = mgr.get_session(session_id)
        if session is None:
            await self._error(req_id, f"session not found: {session_id}")
            return
        await self._result(req_id, session)

    async def _session_delete(self, req_id: Any, params: dict) -> None:
        mgr = self._mgr()
        if not mgr:
            await self._error(req_id, "no session manager configured")
            return
        session_id = params.get("session_id")
        if session_id in self._tasks:
            self._tasks[session_id].cancel()
        mgr.delete_session(session_id)
        await self._result(req_id, {"deleted": True})

    # ──────────────────────────────────────────────────────────────
    # agent/status
    # ──────────────────────────────────────────────────────────────

    async def _agent_status(self, req_id: Any, params: dict) -> None:
        await self._result(req_id, {
            "name": self.agent.name,
            "model": self.agent.llm.model,
            "running_sessions": list(self._tasks.keys()),
        })

    # ──────────────────────────────────────────────────────────────
    # workspace/*
    # ──────────────────────────────────────────────────────────────

    def _wm(self):
        return getattr(self.agent, "workspace_manager", None)

    async def _workspace_list(self, req_id: Any, params: dict) -> None:
        wm = self._wm()
        await self._result(req_id, wm.list_workspaces() if wm else [])

    async def _workspace_create(self, req_id: Any, params: dict) -> None:
        wm = self._wm()
        if not wm:
            await self._error(req_id, "no workspace manager configured")
            return
        name = params.get("name", "").strip()
        path = params.get("path", "").strip()
        if not name or not path:
            await self._error(req_id, "name and path are required")
            return
        ws = wm.create_workspace(name, path)
        await self._result(req_id, ws)

    # ──────────────────────────────────────────────────────────────
    # tool/permission_response
    # ──────────────────────────────────────────────────────────────

    async def _permission_response(self, req_id: Any, params: dict) -> None:
        request_id = params.get("request_id", "")
        approved = bool(params.get("approved", False))
        if self._bridge is not None:
            self._bridge.respond(request_id, approved)
        await self._result(req_id, {"ok": True})
