"""把一个 MCP server 工具包装成 `BaseTool`，注册进 `ToolRegistry` 后与内置工具
同等对待——调用经过 `ToolExecutor` 的权限 / 守护栏 / 审计管线，不是旁路。
"""

from __future__ import annotations

from mcp.types import Tool as McpTool

from ...config import cfg
from ..base import BaseTool, ToolResult
from .connection import MCPConnection


class MCPTool(BaseTool):
    """单个 MCP 工具的 `BaseTool` 适配器。

    `to_schema()` 直接透传 server 给的 `input_schema`（任意 JSON Schema，
    不经过 pydantic——复杂 schema 的 anyOf/$ref 转 pydantic 有损且没必要）。
    `arun()` 同理跳过 `param_class` 校验这条默认路径，原始 dict 直接转发。
    """

    is_async = True

    def __init__(self, connection: MCPConnection, spec: McpTool, prefixed_name: str) -> None:
        self.name = prefixed_name
        self.description = f"[MCP:{connection.server_name}] {spec.description or spec.name}"
        self._connection = connection
        self._original_name = spec.name
        self._input_schema = spec.input_schema or {"type": "object", "properties": {}}
        annotations = spec.annotations
        self.is_concurrency_safe = bool(annotations and annotations.read_only_hint)

    def to_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._input_schema,
            },
        }

    def execute(self, parameters) -> ToolResult:
        raise RuntimeError("MCP 工具是异步工具，只能通过 aexecute()/arun() 调用")

    async def arun(self, parameters: dict) -> ToolResult:
        ok, content = await self._connection.call_tool(
            self._original_name, parameters, timeout=cfg.MCP_TOOL_TIMEOUT
        )
        return ToolResult(ok, content)
