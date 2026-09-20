"""工具执行权限桥接（按连接隔离）。

连接 KittyAgent 的工具执行链路（异步）与 WebSocket 推送（同一事件循环上的异步）。
每次工具调用触发权限检查时：

1. `ask()` 被 `await`，向**该工具调用所属连接**推送 `tool.permission_request`，
   然后 `await` 一个 Future；
2. 渲染器用户点击允许/拒绝后，该连接的 RpcHandler 收到 `tool/permission_response`，
   调用 `respond()`；
3. `respond()` 给 Future `set_result`，`ask()` 的 await 解除并返回 bool。

全程只在一个事件循环上进行，不跨线程，因此不需要锁——`_conns`/`pending` 的
读写天然串行化。

为什么按连接分桶
----------------
`ask_fn=bridge.ask` 在 Agent 构造时就被绑定，Agent 是进程级单例，因此本对象只能是
单例——**不能**靠「每连接建一个新 bridge」来隔离。于是改为内部按连接分桶：每个
WebSocket 连接注册自己的一份 (push_fn, pending)，互不覆盖、互不清理。

归属连接如何确定
----------------
`ask()` 用 contextvar 定位归属连接：RpcHandler 在处理 turn 的任务里 `bind()` 一次。
`asyncio.create_task` 会自动复制 contextvars，所以整条 await 链（工具执行 →
`aexecute` → `check_permission` → `ask`）都能读到正确的连接 id（已用并发双连接
实测验证）。
定位不到归属连接时（多个连接活跃但调用发生在任何连接上下文之外）一律 fail-closed
返回 False——猜错连接等于把 A 的审批弹窗推给 B，比直接拒绝更糟。
"""

import asyncio
import contextlib
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from kittymind.agent.delegation import current_root_session
from kittymind.config import cfg


@dataclass
class _Pending:
    """一次等待中的审批请求。

    除 future 外还留一份推送快照：会话切换 / 重连后前端能凭快照重放弹窗，
    否则「推一次就没了」——切走再切回，弹窗永远不会回来。
    """

    request_id: str
    future: "asyncio.Future[bool]"
    session_id: str | None = None
    tool: str = ""
    args: dict = field(default_factory=dict)
    reason: str = ""
    approved: bool = False

    def snapshot(self) -> dict:
        """重放弹窗所需字段，与 tool.permission_request 的 payload 同形。"""
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "args": self.args,
            "reason": self.reason,
            "session_id": self.session_id,
        }


@dataclass
class _Connection:
    """一个 WebSocket 连接的审批通道。"""

    conn_id: str
    push_fn: object
    pending: dict[str, _Pending] = field(default_factory=dict)


class PermissionBridge:
    """多连接权限审批路由器（进程级单例，内部按连接分桶）。"""

    def __init__(self) -> None:
        self._conns: dict[str, _Connection] = {}
        # 当前工具调用归属的连接 id；由 RpcHandler 在每个 turn 任务里 bind
        self._current: ContextVar[str | None] = ContextVar(
            "km_permission_conn", default=None
        )

    # ── 连接注册 / 注销 ──────────────────────────────────────────

    def set_connection(self, push_fn, conn_id: str | None = None) -> str:
        """注册一个连接，返回其 conn_id（调用方需保存，注销时用）。"""
        cid = conn_id or uuid.uuid4().hex[:8]
        self._conns[cid] = _Connection(conn_id=cid, push_fn=push_fn)
        return cid

    def clear_connection(self, conn_id: str | None = None) -> None:
        """注销连接并 deny 其全部挂起请求，防止 await 永久悬挂。

        传 conn_id 只清理该连接（正常路径）；传 None 清理全部（进程收尾兜底）。
        必须在事件循环线程上调用（会对 Future 直接 set_result）。
        不推 permission_expired：走到这里说明连接正在断开，弹窗所在的渲染层
        已经收不到了（clear_connection 只 deny 本连接的挂起请求）。
        """
        if conn_id is None:
            conns = list(self._conns.values())
            self._conns.clear()
        else:
            conn = self._conns.pop(conn_id, None)
            conns = [conn] if conn is not None else []
        for conn in conns:
            pending = list(conn.pending.values())
            conn.pending.clear()
            for item in pending:
                if not item.future.done():
                    item.future.set_result(False)  # deny by default

    # ── 归属连接绑定 ─────────────────────────────────────────────

    def bind(self, conn_id: str) -> Token:
        """在当前任务上下文中标记归属连接；返回 token 供 unbind 还原。"""
        return self._current.set(conn_id)

    def unbind(self, token: Token) -> None:
        self._current.reset(token)

    def _resolve(self) -> _Connection | None:
        """定位目标连接。contextvar > 唯一连接；否则 None。"""
        cid = self._current.get()
        if cid is not None:
            return self._conns.get(cid)
        # 单连接场景（CLI 测试 / 单窗口）无需 contextvar 也能正确路由
        if len(self._conns) == 1:
            return next(iter(self._conns.values()))
        return None

    # ── 审批往返 ─────────────────────────────────────────────────

    async def ask(self, tool_name: str, args: dict, reason: str) -> bool:
        """await 一个 Future，直到用户响应或超时（→ deny）。不占用任何线程。

        payload 带上 session_id：前端要据此把弹窗归属到发起它的会话。子 Agent
        复用父级作用域，所以这里拿到的是父级真实会话，归属天然正确。
        """
        conn = self._resolve()
        if conn is None:
            return False  # fail-closed：定位不到归属连接就拒绝

        request_id = uuid.uuid4().hex[:8]
        session_id = current_root_session()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.pending[request_id] = _Pending(
            request_id=request_id, future=fut, session_id=session_id,
            tool=tool_name, args=args, reason=reason,
        )

        try:
            await conn.push_fn("tool.permission_request", {
                "request_id": request_id,
                "tool": tool_name,
                "args": args,
                "reason": reason,
                "session_id": session_id,
            })
        except Exception:
            # 推送失败（连接正在断开）——收回登记并拒绝
            conn.pending.pop(request_id, None)
            return False

        try:
            return await asyncio.wait_for(fut, timeout=cfg.PERMISSION_ASK_TIMEOUT)
        except asyncio.TimeoutError:
            # 超时按拒绝处理，同时主动撤回前端弹窗——否则弹窗仍挂在屏幕上，
            # 用户点「允许」时 respond() 返回 matched=false，观感是「批准了却没执行」。
            await self._notify_expired(conn, request_id, tool_name, session_id)
            return False
        finally:
            conn.pending.pop(request_id, None)

    async def _notify_expired(
        self, conn: _Connection, request_id: str, tool_name: str,
        session_id: str | None,
    ) -> None:
        """向前端推送 tool.permission_expired（连接已断时静默放弃）。"""
        with contextlib.suppress(Exception):
            await conn.push_fn("tool.permission_expired", {
                "request_id": request_id,
                "tool": tool_name,
                "session_id": session_id,
            })

    def pending_for(
        self, session_id: str | None = None, conn_id: str | None = None
    ) -> list[dict]:
        """返回某连接下指定会话的待审批快照，供切换会话 / 重连时重放弹窗。

        session_id 传 None 表示该连接的全部待审批（调试用）；正常调用都应带上
        session_id——否则会把别的会话的弹窗也一并带回来。
        """
        conn = self._conns.get(conn_id) if conn_id is not None else self._resolve()
        if conn is None:
            return []
        return [
            item.snapshot()
            for item in conn.pending.values()
            if session_id is None or item.session_id == session_id
        ]

    def respond(
        self,
        request_id: str,
        approved: bool,
        conn_id: str | None = None,
    ) -> bool:
        """Called from the asyncio event loop. 给挂起的 Future set_result。

        返回是否真的命中了一个挂起请求（未命中说明超时/连接已断，调用方可用于诊断）。
        """
        conn = self._conns.get(conn_id) if conn_id is not None else self._resolve()
        if conn is None:
            return False
        item = conn.pending.get(request_id)
        if item is None:
            return False
        item.approved = approved
        if not item.future.done():
            item.future.set_result(approved)
        return True
