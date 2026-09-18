"""工具执行权限桥接（按连接隔离）。

连接 PermissionToolExecutor（在 worker 线程中同步阻塞）与 WebSocket 推送（在
asyncio 事件循环中异步执行）。每次工具调用触发权限检查时：

1. ask() 被 worker 线程调用，向**该工具调用所属连接**推送 tool.permission_request，
   然后阻塞 threading.Event；
2. 渲染器用户点击允许/拒绝后，该连接的 RpcHandler 收到 tool/permission_response，
   调用 respond()；
3. respond() 设置 Event，ask() 解除阻塞并返回 bool。

为什么按连接分桶
----------------
`ask_fn=bridge.ask` 在 Agent 构造时就被绑定，Agent 是进程级单例，因此本对象只能是
单例——**不能**靠「每连接建一个新 bridge」来隔离。于是改为内部按连接分桶：每个
WebSocket 连接注册自己的一份 (loop, push_fn, pending)，互不覆盖、互不清理。

归属连接如何确定
----------------
ask() 用 contextvar 定位归属连接：RpcHandler 在处理 turn 的任务里 bind() 一次。
工具在线程池里执行时，kitty_agent 用 `copy_context().run` 显式把上下文带进 worker
线程（`asyncio.to_thread` 自带该行为，但裸 `run_in_executor` 不会——这也是工具
执行不能用裸 run_in_executor 的原因），因此阻塞中的 ask() 也能读到正确的连接 id
（已用并发双连接实测验证）。
定位不到归属连接时（多个连接活跃但调用发生在任何连接上下文之外）一律 fail-closed
返回 False——猜错连接等于把 A 的审批弹窗推给 B，比直接拒绝更糟。
"""

import asyncio
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from kittymind.config import cfg


@dataclass
class _Pending:
    """一次等待中的审批请求。"""

    event: threading.Event
    approved: bool = False


@dataclass
class _Connection:
    """一个 WebSocket 连接的审批通道。"""

    conn_id: str
    loop: asyncio.AbstractEventLoop
    push_fn: object
    pending: dict[str, _Pending] = field(default_factory=dict)


class PermissionBridge:
    """多连接权限审批路由器（进程级单例，内部按连接分桶）。"""

    def __init__(self) -> None:
        self._conns: dict[str, _Connection] = {}
        self._lock = threading.Lock()
        # 当前工具调用归属的连接 id；由 RpcHandler 在每个 turn 任务里 bind
        self._current: ContextVar[str | None] = ContextVar(
            "km_permission_conn", default=None
        )

    # ── 连接注册 / 注销 ──────────────────────────────────────────

    def set_connection(
        self,
        loop: asyncio.AbstractEventLoop,
        push_fn,
        conn_id: str | None = None,
    ) -> str:
        """注册一个连接，返回其 conn_id（调用方需保存，注销时用）。"""
        cid = conn_id or uuid.uuid4().hex[:8]
        with self._lock:
            self._conns[cid] = _Connection(conn_id=cid, loop=loop, push_fn=push_fn)
        return cid

    def clear_connection(self, conn_id: str | None = None) -> None:
        """注销连接并 deny 其全部挂起请求，防止 worker 线程永久阻塞。

        传 conn_id 只清理该连接（正常路径）；传 None 清理全部（进程收尾兜底）。
        """
        if conn_id is None:
            with self._lock:
                conns = list(self._conns.values())
                self._conns.clear()
        else:
            with self._lock:
                conn = self._conns.pop(conn_id, None)
            conns = [conn] if conn is not None else []
        for conn in conns:
            with self._lock:
                pending = list(conn.pending.values())
                conn.pending.clear()
            for item in pending:
                item.event.set()  # deny by default

    # ── 归属连接绑定 ─────────────────────────────────────────────

    def bind(self, conn_id: str) -> Token:
        """在当前任务上下文中标记归属连接；返回 token 供 unbind 还原。"""
        return self._current.set(conn_id)

    def unbind(self, token: Token) -> None:
        self._current.reset(token)

    def _resolve(self, conn_id: str | None) -> _Connection | None:
        """定位目标连接。显式 conn_id > contextvar > 唯一连接；否则 None。"""
        with self._lock:
            if conn_id is not None:
                return self._conns.get(conn_id)
            cid = self._current.get()
            if cid is not None:
                return self._conns.get(cid)
            # 单连接场景（CLI 测试 / 单窗口）无需 contextvar 也能正确路由
            if len(self._conns) == 1:
                return next(iter(self._conns.values()))
            return None

    # ── 审批往返 ─────────────────────────────────────────────────

    def ask(
        self,
        tool_name: str,
        args: dict,
        reason: str,
        conn_id: str | None = None,
    ) -> bool:
        """Called from worker thread. Blocks until user responds or timeout (→ deny)."""
        conn = self._resolve(conn_id)
        if conn is None:
            return False  # fail-closed：定位不到归属连接就拒绝

        request_id = uuid.uuid4().hex[:8]
        item = _Pending(event=threading.Event())
        with self._lock:
            conn.pending[request_id] = item

        try:
            asyncio.run_coroutine_threadsafe(
                conn.push_fn("tool.permission_request", {
                    "request_id": request_id,
                    "tool": tool_name,
                    "args": args,
                    "reason": reason,
                }),
                conn.loop,
            )
        except Exception:
            # 事件循环已关闭（连接正在断开）——收回登记并拒绝
            with self._lock:
                conn.pending.pop(request_id, None)
            return False

        try:
            item.event.wait(timeout=cfg.PERMISSION_ASK_TIMEOUT)
        finally:
            with self._lock:
                conn.pending.pop(request_id, None)
        return item.approved

    def respond(
        self,
        request_id: str,
        approved: bool,
        conn_id: str | None = None,
    ) -> bool:
        """Called from the asyncio event loop. Unblocks the waiting worker thread.

        返回是否真的命中了一个挂起请求（未命中说明超时/连接已断，调用方可用于诊断）。
        """
        conn = self._resolve(conn_id)
        if conn is None:
            return False
        with self._lock:
            item = conn.pending.get(request_id)
        if item is None:
            return False
        item.approved = approved
        item.event.set()
        return True
