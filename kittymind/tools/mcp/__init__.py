"""外部 MCP Server 接入。

发现与连接的入口是 `loader.discover_mcp_tools()`，但本文件**刻意不在此处重新导出
它**——`tools/permission.py` 要从 `.mcp.trust` 导入信任表（判断 MCP 工具是否需要
审批），而 `loader.py` 会级联导入第三方 `mcp` SDK（`connection.py` 里的
`from mcp import ClientSession, ...`）。包的 `__init__.py` 在导入任何子模块前
总会先执行，若在这里 `from .loader import discover_mcp_tools`，等于每次工具调用
都要经过的 `permission.py` 被迫级联加载整个 MCP SDK——哪怕用户从没配置过 MCP
server、甚至没装 `mcp` 这个包。

调用方按需直接导入：
    from kittymind.tools.mcp.loader import discover_mcp_tools
    from kittymind.tools.mcp.trust import is_mcp_tool, is_auto_approved
"""
