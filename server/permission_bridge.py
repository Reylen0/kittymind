"""工具执行权限桥接。

连接 PermissionToolExecutor（在 worker 线程中同步阻塞）与 WebSocket 推送（在
asyncio 事件循环中异步执行）。每次工具调用触发权限检查时：

1. ask() 被 worker 线程调用，发送 tool.permission_request 推送后阻塞 threading.Event；
2. 渲染器用户点击允许/拒绝后，RpcHandler 收到 tool/permission_response 并调用 respond()；
3. respond() 设置 Event，ask() 解除阻塞并返回 bool。

断开连接时 clear_connection() 自动 deny 所有挂起请求，防止 worker 线程永久阻塞。
"""

import asyncio
import threading
import uuid
from typing import Optional


class PermissionBridge:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._push_fn = None
        self._pending: dict[str, dict] = {}  # request_id → {event, approved}

    def set_connection(self, loop: asyncio.AbstractEventLoop, push_fn) -> None:
        self._loop = loop
        self._push_fn = push_fn

    def clear_connection(self) -> None:
        for item in list(self._pending.values()):
            item["event"].set()  # deny by default
        self._pending.clear()
        self._push_fn = None
        self._loop = None

    def ask(self, tool_name: str, args: dict, reason: str) -> bool:
        """Called from worker thread. Blocks until user responds or 120 s timeout (→ deny)."""
        if self._push_fn is None or self._loop is None:
            return False

        request_id = str(uuid.uuid4())[:8]
        evt = threading.Event()
        self._pending[request_id] = {"event": evt, "approved": False}

        asyncio.run_coroutine_threadsafe(
            self._push_fn("tool.permission_request", {
                "request_id": request_id,
                "tool": tool_name,
                "args": args,
                "reason": reason,
            }),
            self._loop,
        )

        evt.wait(timeout=120)
        return self._pending.pop(request_id, {}).get("approved", False)

    def respond(self, request_id: str, approved: bool) -> None:
        """Called from the asyncio event loop. Unblocks the waiting worker thread."""
        item = self._pending.get(request_id)
        if item:
            item["approved"] = approved
            item["event"].set()
