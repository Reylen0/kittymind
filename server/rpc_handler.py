"""JSON-RPC 方法路由。

每个 WebSocket 连接对应一个 RpcHandler 实例。
turn/run 产生的 agent 事件通过 EventBus 订阅后实时 push 给客户端。
"""

import asyncio
import contextlib
import json
from typing import Any

from kittymind.events.types import (
    AGENT_CHUNK, AGENT_THINKING,
    AGENT_TOOL_CALL, AGENT_TOOL_RESULT,
    AGENT_DONE, AGENT_ERROR, AGENT_CONTEXT_USAGE,
    SUBAGENT_START, SUBAGENT_DONE,
)
from kittymind.agent import KittyAgent
from kittymind.usage import cost_from_by_model


# 单次 session/get 最多返回的消息条数（分页上限）：请求方传再大的 limit 也不越界，
# 避免一次 RPC 把超长会话整个拖回前端
_MAX_PAGE_MESSAGES = 200

# 单次 session/search 最多返回的命中条数。比分页上限小一个量级：搜索结果是给人
# 扫一眼用的，几百条命中在侧栏里既滚不完也没意义，相关度靠前的那些才有价值。
_MAX_SEARCH_HITS = 100


class RpcHandler:
    def __init__(self, agent: KittyAgent, ws, bridge=None) -> None:
        self.agent = agent
        self.ws = ws
        self._tasks: dict[str, asyncio.Task] = {}
        self._bridge = bridge
        # 方法路由表：每个连接建一次即可，不必每次 dispatch 重建
        self._handlers: dict[str, Any] = {
            "turn/run":                 self._turn_run,
            "turn/cancel":              self._turn_cancel,
            "session/create":           self._session_create,
            "session/list":             self._session_list,
            "session/get":              self._session_get,
            "session/search":           self._session_search,
            "session/delete":           self._session_delete,
            "agent/status":             self._agent_status,
            "usage/report":             self._usage_report,
            "workspace/list":           self._workspace_list,
            "workspace/create":         self._workspace_create,
            "workspace/delete":         self._workspace_delete,
            "tool/permission_response": self._permission_response,
            "permission/pending":       self._permission_pending,
        }
        # 本连接的审批通道 id：bridge 是进程级单例，靠它区分多连接
        self.conn_id: str | None = None
        if bridge is not None:
            self.conn_id = bridge.set_connection(self._push)

    # ──────────────────────────────────────────────────────────────
    # 消息收发
    # ──────────────────────────────────────────────────────────────

    async def dispatch(self, request: dict) -> None:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        fn = self._handlers.get(method)
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
        # 连接已关闭时 send 会抛异常，静默忽略
        with contextlib.suppress(Exception):
            await self.ws.send(json.dumps(data, ensure_ascii=False))

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

        async def fwd_subagent_start(et, data):
            if data.get("session_id") == session_id:
                await self._push("subagent.start", data)

        async def fwd_subagent_done(et, data):
            if data.get("session_id") == session_id:
                await self._push("subagent.done", data)

        bus.subscribe(AGENT_CHUNK,         fwd_chunk)
        bus.subscribe(AGENT_THINKING,      fwd_thinking)
        bus.subscribe(AGENT_TOOL_CALL,     fwd_tool_call)
        bus.subscribe(AGENT_TOOL_RESULT,   fwd_tool_result)
        bus.subscribe(AGENT_DONE,          fwd_done)
        bus.subscribe(AGENT_ERROR,         fwd_error)
        bus.subscribe(AGENT_CONTEXT_USAGE, fwd_context_usage)
        bus.subscribe(SUBAGENT_START,      fwd_subagent_start)
        bus.subscribe(SUBAGENT_DONE,       fwd_subagent_done)

        # 立即 ack，事件通过 server push 异步推送
        await self._result(req_id, {"status": "started"})

        workspace_id = params.get("workspace_id")

        async def _run() -> None:
            # 标记本 turn 归属的连接：整条调用链（工具执行 → 权限审批）都在这个
            # task 内部 await，contextvar 天然沿调用栈传播，PermissionBridge.ask()
            # 据此找到正确的推送目标（多连接并存时不会互相劫持审批）。
            conn_token = (
                self._bridge.bind(self.conn_id)
                if self._bridge is not None and self.conn_id is not None
                else None
            )
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
                if conn_token is not None:
                    self._bridge.unbind(conn_token)
                bus.unsubscribe(AGENT_CHUNK,         fwd_chunk)
                bus.unsubscribe(AGENT_THINKING,      fwd_thinking)
                bus.unsubscribe(AGENT_TOOL_CALL,     fwd_tool_call)
                bus.unsubscribe(AGENT_TOOL_RESULT,   fwd_tool_result)
                bus.unsubscribe(AGENT_DONE,          fwd_done)
                bus.unsubscribe(AGENT_ERROR,         fwd_error)
                bus.unsubscribe(AGENT_CONTEXT_USAGE, fwd_context_usage)
                bus.unsubscribe(SUBAGENT_START,      fwd_subagent_start)
                bus.unsubscribe(SUBAGENT_DONE,       fwd_subagent_done)
                # 仅当本任务仍是该 session 的当前任务时才摘除登记。
                # 同一 session 再次 turn/run 会先 cancel 旧任务并登记新任务，
                # 旧任务的 finally 若无条件 pop，会把新任务条目误删，
                # 导致随后 turn/cancel 找不到任务、停止按钮失效。
                if self._tasks.get(session_id) is asyncio.current_task():
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
        if not session_id:
            await self._error(req_id, "session_id is required")
            return

        # 可选分页：limit 必须是正整数（缺省 = 全量，保持旧语义）。
        # before_seq 只在分页模式下有意义——它是不含端点的游标，取「seq 更早」的那一页。
        kwargs: dict = {}
        limit = params.get("limit")
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and int(limit) > 0:
            kwargs["limit"] = min(int(limit), _MAX_PAGE_MESSAGES)
            before_seq = params.get("before_seq")
            if isinstance(before_seq, (int, float)) and not isinstance(before_seq, bool):
                kwargs["before_seq"] = before_seq

        session = mgr.get_session(session_id, **kwargs)
        if session is None:
            await self._error(req_id, f"session not found: {session_id}")
            return
        await self._result(req_id, session)

    async def _session_search(self, req_id: Any, params: dict) -> None:
        """全文搜索历史消息。结果按会话分组，每组带若干命中片段。

        空查询回空数组而不是报错：输入框被清空是正常状态，不是调用错误。

        和其余 session/* 一样直接同步调 SQLite，不裹 asyncio.to_thread ——
        本项目的界线是「LLM / 工具体进线程，SQLite 裸调」，FTS 查询是毫秒级，
        为它单独破例只会让 RPC 层多出一个不一致的特例。
        """
        mgr = self._mgr()
        if not mgr:
            await self._error(req_id, "no session manager configured")
            return

        query = (params.get("query") or "").strip()
        if not query:
            await self._result(req_id, [])
            return

        kwargs: dict = {}
        limit = params.get("limit")
        # not isinstance(bool) 是刻意的：True 是 int 的子类，会被当成 limit=1
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and int(limit) > 0:
            kwargs["limit"] = min(int(limit), _MAX_SEARCH_HITS)
        else:
            kwargs["limit"] = _MAX_SEARCH_HITS
        session_id = params.get("session_id")
        if session_id:
            kwargs["session_id"] = session_id

        await self._result(req_id, mgr.search_messages(query, **kwargs))

    async def _session_delete(self, req_id: Any, params: dict) -> None:
        mgr = self._mgr()
        if not mgr:
            await self._error(req_id, "no session manager configured")
            return
        session_id = params.get("session_id")
        if session_id in self._tasks:
            self._tasks[session_id].cancel()
        # 如实返回：删不存在的会话给 {"deleted": false}，不再谎报成功
        deleted = mgr.delete_session(session_id)
        await self._result(req_id, {"deleted": deleted})

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
    # usage/report
    # ──────────────────────────────────────────────────────────────

    async def _usage_report(self, req_id: Any, params: dict) -> None:
        """用量成本报告。group_by ∈ {model, day, session}，since/until 为 epoch 秒。

        返回 {"groups": [...], "total": {...}}。cost 字段在价目表查不到时为 null。
        """
        mgr = self._mgr()
        if not mgr:
            await self._error(req_id, "no session manager configured")
            return

        group_by = params.get("group_by") or "model"
        if group_by not in ("model", "day", "session"):
            await self._error(req_id, f"invalid group_by: {group_by}")
            return

        since = params.get("since")
        until = params.get("until")
        def _to_ts(v):
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        groups = mgr.aggregate_usage(group_by, _to_ts(since), _to_ts(until))

        # 成本估算：每组按「组内各模型分别计价再求和」（by_model 明细由 store
        # 聚合时保留），全部模型查不到价目时为 null。
        #
        # session 分组补充标题 / 工作区名：主行显示标题，小字行显示 id +
        # 工作区。会话已删除时 read_header 返回 None → title 保持 None，前端
        # 回退显示 id（用量记录审计上本就允许悬空）。
        sessions_meta: dict[str, tuple[str | None, str | None]] = {}
        if group_by == "session":
            ws_names: dict[str | None, str | None] = {}
            wm = self._wm()
            for g in groups:
                sid = g["key"]
                if sid in sessions_meta:
                    continue
                header = mgr.store.read_header(sid)
                if header is None:
                    sessions_meta[sid] = (None, None)
                    continue
                ws_id = header.get("workspace_id")
                if ws_id not in ws_names:
                    ws = wm.get_workspace(ws_id) if (wm and ws_id) else None
                    ws_names[ws_id] = ws.get("name") if ws else None
                sessions_meta[sid] = (header.get("title"), ws_names[ws_id])

        result_groups = []
        total_p = total_c = total_n = 0
        total_cost: float | None = None
        for g in groups:
            total_p += g["prompt_tokens"]
            total_c += g["completion_tokens"]
            total_n += g["n_calls"]
            cost = cost_from_by_model(g.get("by_model", {}))
            if cost is not None:
                total_cost = (total_cost or 0.0) + cost
            row = {
                "key": g["key"],
                "prompt_tokens": g["prompt_tokens"],
                "completion_tokens": g["completion_tokens"],
                "n_calls": g["n_calls"],
                "cost": cost,
            }
            if group_by == "session":
                title, ws_name = sessions_meta.get(g["key"], (None, None))
                row["title"] = title
                row["workspace"] = ws_name
            result_groups.append(row)

        await self._result(req_id, {
            "group_by": group_by,
            "groups": result_groups,
            "total": {
                "prompt_tokens": total_p,
                "completion_tokens": total_c,
                "n_calls": total_n,
                "cost": total_cost,
            },
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

    async def _workspace_delete(self, req_id: Any, params: dict) -> None:
        """删除工作区。其下会话不删——解除归属移回「对话」分组。

        工作区只是会话的一个视图分组（JSON 清单），会话历史在 SQLite 里；
        删分组连带删对话是数据事故，不是用户预期。
        """
        wm = self._wm()
        if not wm:
            await self._error(req_id, "no workspace manager configured")
            return
        workspace_id = params.get("workspace_id", "")
        if not wm.delete_workspace(workspace_id):
            await self._error(req_id, f"workspace not found: {workspace_id}")
            return
        mgr = self._mgr()
        moved = mgr.clear_workspace(workspace_id) if mgr else 0
        await self._result(req_id, {"deleted": True, "moved_sessions": moved})

    # ──────────────────────────────────────────────────────────────
    # tool/permission_response
    # ──────────────────────────────────────────────────────────────

    async def _permission_response(self, req_id: Any, params: dict) -> None:
        request_id = params.get("request_id", "")
        approved = bool(params.get("approved", False))
        hit = False
        if self._bridge is not None:
            # 显式带 conn_id：审批只在本连接的挂起请求里查找，
            # 避免两个连接同时弹窗时 request_id 撞车或跨连接误批
            hit = self._bridge.respond(request_id, approved, conn_id=self.conn_id)
        # ok = 是否真的有桥可路由：bridge 为 None 时谎报 ok:true 会掩盖「审批没人接」
        await self._result(req_id, {"ok": self._bridge is not None, "matched": hit})

    # ──────────────────────────────────────────────────────────────
    # permission/pending
    # ──────────────────────────────────────────────────────────────

    async def _permission_pending(self, req_id: Any, params: dict) -> None:
        """列出本连接下某个会话仍在等待用户确认的审批请求。

        前端切回会话（或窗口重新加载）时调用，用返回值重放弹窗——审批事件是
        一次性推送，错过了就再也收不到，必须能按需补拉。

        必须按 conn_id 限定：bridge 是进程级单例，不加限定会把别的窗口的
        待审批一并带回来。
        """
        session_id = params.get("session_id")
        pending: list = []
        if self._bridge is not None:
            pending = self._bridge.pending_for(session_id, conn_id=self.conn_id)
        await self._result(req_id, {"pending": pending})
