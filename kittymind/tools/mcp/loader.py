"""编排 config → connection → tool：discover_mcp_tools() 是唯一对外入口。"""

from __future__ import annotations

import asyncio
import logging
import re

from ...config import cfg
from ..base import BaseTool
from .config import load_mcp_config
from .connection import MCPConnection
from .tool import MCPTool
from .trust import mark_auto_approve

logger = logging.getLogger(__name__)

_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_TOOL_NAME_LEN = 64  # OpenAI function-calling 的 name 长度上限


def _normalize(s: str) -> str:
    """把非 `[a-zA-Z0-9_-]` 字符替换为下划线——原始 MCP 名字不保证满足这个字符集。"""
    return _NAME_UNSAFE.sub("_", s)


def _prefixed_name(server_name: str, tool_name: str) -> str:
    prefixed = f"mcp__{_normalize(server_name)}__{_normalize(tool_name)}"
    return prefixed[:_MAX_TOOL_NAME_LEN]


async def discover_mcp_tools() -> tuple[list[BaseTool], list[MCPConnection]]:
    """并发连接 mcp.json 里所有启用的 server，返回 (发现的工具, 连接列表)。

    单个 server 连接失败只记日志跳过，不影响其它 server 或调用方启动流程。
    返回的 connections 需由调用方在进程退出时逐个 `stop()`。
    """
    if not cfg.MCP_ENABLED:
        return [], []

    configs = load_mcp_config()
    if not configs:
        return [], []

    conns = {name: MCPConnection(name, server_cfg) for name, server_cfg in configs.items()}
    await asyncio.gather(
        *(conn.start(cfg.MCP_CONNECT_TIMEOUT) for conn in conns.values()),
        return_exceptions=True,
    )

    tools: list[BaseTool] = []
    seen: set[str] = set()
    for name, conn in conns.items():
        if conn.error:
            logger.warning("MCP server %r 连接失败，已跳过: %s", name, conn.error)
            continue
        logger.info("MCP server %r 已连接，发现 %d 个工具", name, len(conn.tools))

        auto_approve = set(configs[name].get("autoApprove") or [])
        for spec in conn.tools:
            prefixed = _prefixed_name(name, spec.name)
            if prefixed in seen:
                logger.warning("MCP 工具名冲突，已跳过: %s（来自 server %r）", prefixed, name)
                continue
            seen.add(prefixed)
            if spec.name in auto_approve:
                mark_auto_approve(prefixed)
            tools.append(MCPTool(conn, spec, prefixed))

    return tools, list(conns.values())
