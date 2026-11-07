"""单个 MCP Server 的异步连接生命周期。

kittymind 本身单事件循环跑满所有工具调用（`ToolExecutor` 异步执行），因此这里
不需要像同步 agent 那样另开线程+独立事件循环——用一个常驻的 `asyncio.Task`
把 transport 的 `async with` 上下文管理器挂住，通过 `asyncio.Event` 和外部握手
即可：

    start() 创建后台任务 → 任务内部建连/initialize/list_tools → 设置 ready
    → 挂起等 stop 事件 → stop() 置位 stop 事件 → 任务退出 async with 完成清理

这样「谁调用就得在谁的调用栈里 async with」的生命周期耦合问题不存在：
`call_tool()` 可以在任意时刻从任意协程调用，只要 session 还活着。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool as McpTool

logger = logging.getLogger(__name__)


def _unwrap(exc: BaseException) -> BaseException:
    """anyio TaskGroup 失败时抛的是 ExceptionGroup，取第一个真正的子异常。

    用 duck-typing 而不是 `isinstance(exc, BaseExceptionGroup)`——后者是 3.11+
    内置类型，本项目 `requires-python = ">=3.10"`，3.10 上直接 NameError。
    """
    while hasattr(exc, "exceptions") and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class MCPConnection:
    """一个 MCP server 的连接 + 已发现工具列表。"""

    def __init__(self, server_name: str, server_cfg: dict) -> None:
        self.server_name = server_name
        self._cfg = server_cfg
        self.tools: list[McpTool] = []
        self.error: str | None = None

        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ── 生命周期 ─────────────────────────────────────────────────

    async def start(self, timeout: float) -> bool:
        """建连 + initialize + list_tools。成功返回 True，失败/超时返回 False（见 self.error）。"""
        self._task = asyncio.create_task(self._run(), name=f"mcp-conn:{self.server_name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            self.error = f"连接超时（{timeout}s）"
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
            return False
        return self.error is None

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, timeout=5)

    async def _run(self) -> None:
        try:
            async with self._open_transport() as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                self.tools = list(listed.tools)
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except Exception as e:
            self.error = str(_unwrap(e)) or type(_unwrap(e)).__name__
            logger.warning("MCP server %r 连接异常: %s", self.server_name, self.error)
        finally:
            self._session = None
            self._ready.set()  # 未就绪就失败时，解除 start() 里的等待

    @asynccontextmanager
    async def _open_transport(self) -> AsyncIterator[tuple]:
        transport = self._cfg.get("type", "stdio")
        if transport == "stdio":
            env = {**os.environ, **(self._cfg.get("env") or {})}
            params = StdioServerParameters(
                command=self._cfg["command"],
                args=self._cfg.get("args") or [],
                env=env,
                cwd=self._cfg.get("cwd"),
            )
            async with stdio_client(params) as streams:
                yield streams
        elif transport == "sse":
            async with sse_client(self._cfg["url"], headers=self._cfg.get("headers")) as streams:
                yield streams
        elif transport == "streamable_http":
            headers = self._cfg.get("headers")
            if headers:
                import httpx2
                async with (
                    httpx2.AsyncClient(headers=headers) as http_client,
                    streamable_http_client(self._cfg["url"], http_client=http_client) as streams,
                ):
                    yield streams
            else:
                async with streamable_http_client(self._cfg["url"]) as streams:
                    yield streams
        else:
            raise ValueError(f"不支持的 MCP 传输类型: {transport!r}")

    # ── 调用 ─────────────────────────────────────────────────────

    async def call_tool(self, tool_name: str, args: dict, timeout: float) -> tuple[bool, str]:
        if self._session is None:
            detail = f"：{self.error}" if self.error else ""
            return False, f"MCP server '{self.server_name}' 未连接或已断开{detail}"
        try:
            result = await asyncio.wait_for(self._session.call_tool(tool_name, args), timeout)
        except TimeoutError:
            return False, f"MCP 工具调用超时（{timeout}s）"
        except Exception as e:
            return False, f"MCP 调用失败: {type(e).__name__}: {e}"

        texts = [c.text for c in (result.content or []) if hasattr(c, "text")]
        content = "\n".join(texts) if texts else "(无输出)"
        return (not result.is_error), content
