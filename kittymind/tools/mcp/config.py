"""读取 `~/.kittymind/mcp.json`，返回 server 名 -> 配置字典。

格式（与 Claude Desktop 生态兼容）：
    {
      "mcpServers": {
        "github": {
          "type": "stdio",                 # stdio（默认）/ sse / streamable_http
          "command": "npx", "args": [...], "env": {...}, "cwd": "...",
          "disabled": false,
          "autoApprove": ["search_repositories"]
        },
        "internal-search": {
          "type": "sse",
          "url": "https://...", "headers": {...}
        }
      }
    }

文件不存在 = 没配置任何 MCP server，不是错误（与 web_search 未配置时的处理方式一致）。
单个 server 配置有明显错误（缺 command/url）只记日志跳过，不影响其它 server。
"""

from __future__ import annotations

import json
import logging

from ...config import cfg

logger = logging.getLogger(__name__)

_VALID_TYPES = {"stdio", "sse", "streamable_http"}


def load_mcp_config() -> dict[str, dict]:
    """返回已过滤 disabled/非法配置的 {server_name: server_cfg}。"""
    path = cfg.MCP_CONFIG_FILE
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("MCP 配置文件 %s 解析失败，已忽略: %s", path, e)
        return {}

    servers = raw.get("mcpServers") or {}
    if not isinstance(servers, dict):
        logger.warning("MCP 配置文件 %s 的 mcpServers 字段不是对象，已忽略", path)
        return {}

    result: dict[str, dict] = {}
    for name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            logger.warning("MCP server %r 配置不是对象，已跳过", name)
            continue
        if server_cfg.get("disabled", False):
            continue

        transport = server_cfg.get("type", "stdio")
        if transport not in _VALID_TYPES:
            logger.warning("MCP server %r 的 type=%r 不支持，已跳过", name, transport)
            continue
        if transport == "stdio" and not server_cfg.get("command"):
            logger.warning("MCP server %r 缺少 command 字段，已跳过", name)
            continue
        if transport in ("sse", "streamable_http") and not server_cfg.get("url"):
            logger.warning("MCP server %r 缺少 url 字段，已跳过", name)
            continue

        result[name] = server_cfg
    return result
