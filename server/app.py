"""KittyMind Python server 启动入口。

用法:
    uv run python -m server.app

环境变量:
    WS_HOST  — 监听地址（默认 127.0.0.1）
    WS_PORT  — 监听端口（默认 8765）

Electron 通过监听 stdout 中的 "[ready] ws://..." 行确认 server 就绪。
"""

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from kittymind.tools.builtin.bash_tool import BashTool
from kittymind.tools.builtin.file_read_tool import FileReadTool
from kittymind.tools.builtin.file_write_tool import FileWriteTool
from kittymind.tools.builtin.file_edit_tool import FileEditTool
from kittymind.tools.builtin.glob_tool import GlobTool
from kittymind.tools.builtin.grep_tool import GrepTool
from kittymind.tools.builtin.ls_tool import LsTool
from kittymind.tools.builtin.git_tool import GitTool
from kittymind.tools.builtin.screenshot_tool import ScreenshotTool
from kittymind.tools.builtin.clipboard_tool import ClipboardTool
from kittymind.tools.builtin.get_current_time_tool import GetCurrentTimeTool
from kittymind.tools.builtin.write_memory_tool import WriteMemoryTool
from kittymind.tools.builtin.task_tool import TaskTool
from kittymind.tools.builtin.verify_tool import VerifyTool
from kittymind.tools.builtin.web_search_tool import WebSearchTool
from kittymind.tools.builtin.web_extract_tool import WebExtractTool
from kittymind.tools.builtin.load_skill_tool import LoadSkillTool
from kittymind.tools.builtin.write_skill_tool import WriteSkillTool
from kittymind.tools.mcp.loader import discover_mcp_tools
from kittymind.memory.store import MemoryStore
from kittymind.skills.store import SkillStore
from kittymind.logging_setup import setup_logging
from kittymind.config import cfg
from server.permission_bridge import PermissionBridge

# .env 加载优先级：
#   1. ~/.kittymind/.env  （用户级配置，打包和开发都适用）
#   2. exe/脚本所在目录/.env
#   3. 当前工作目录/.env（兜底）
_user_env = Path.home() / ".kittymind" / ".env"
if _user_env.exists():
    load_dotenv(_user_env)
else:
    _exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    load_dotenv(_exe_dir / ".env")
    load_dotenv()  # fallback: cwd

from kittymind.core.llm import BaseAgentLLM
from kittymind.events.bus import EventBus
from kittymind.session.manager import SessionManager
from kittymind.workspace.manager import WorkspaceManager
from kittymind.agent import KittyAgent
from kittymind.prompts import build_system_prompt
from server.ws_server import start_server

logger = logging.getLogger(__name__)


async def build_agent(bridge: PermissionBridge) -> tuple[KittyAgent, list]:
    llm = BaseAgentLLM()
    memory_store = MemoryStore()
    # 工具集与 CLI（chat_async.py）保持一致：15 个内置工具 + task 委派
    base_tools = [
        GetCurrentTimeTool(),
        LsTool(),
        GlobTool(),
        GrepTool(),
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        GitTool(),
        BashTool(),
        ScreenshotTool(),
        ClipboardTool(),
        WriteMemoryTool(memory_store),
        VerifyTool(),
        WebSearchTool(),
        WebExtractTool(),
    ]
    # 技能系统（~/.kittymind/skills/ 下的 SKILL.md，只有 name/description 两个
    # 元信息字段，没有使用统计/归档——纯粹的文件扫描）。
    skill_store = None
    if cfg.SKILL_ENABLED:
        skill_store = SkillStore()
        base_tools += [LoadSkillTool(skill_store), WriteSkillTool(skill_store)]
    # 外部 MCP server（~/.kittymind/mcp.json 配置，未配置则为空列表）；
    # 单个 server 连不上只记日志跳过，不拖垮启动流程。
    mcp_tools, mcp_conns = await discover_mcp_tools()
    sub_tools = base_tools + mcp_tools

    # bus 必须先建，再同时传给 TaskTool 和 KittyAgent——子事件要发到同一个总线
    bus  = EventBus()
    task_tool = TaskTool(
        llm=llm,
        sub_tools=sub_tools,
        ask_fn=bridge.ask,
        event_bus=bus,
    )
    all_tools = sub_tools + [task_tool]
    agent = KittyAgent(
        name="kitty",
        llm=llm,
        tools=all_tools,
        system_prompt=build_system_prompt(all_tools),
        event_bus=bus,
        session_manager=SessionManager(),
        workspace_manager=WorkspaceManager(),
        memory=memory_store,
        skill_store=skill_store,
        ask_fn=bridge.ask,
    )
    print(
        "[aux] 压缩摘要 / 记忆任务使用: "
        + (agent.aux_llm.model if agent.aux_llm else "未配置（回退主模型）"),
        flush=True,
    )
    if mcp_tools:
        print(f"[mcp] 已接入 {len(mcp_tools)} 个外部 MCP 工具", flush=True)
    if skill_store is not None:
        skill_count = len(skill_store.list_skills())
        if skill_count:
            print(f"[skill] 已加载 {skill_count} 个技能", flush=True)
    return agent, mcp_conns


async def main() -> None:
    setup_logging()  # 诊断日志走 stderr；stdout 只留 [ready] / [aux] 两行启动横幅
    host = os.getenv("WS_HOST", cfg.WS_HOST)
    base_port = int(os.getenv("WS_PORT", cfg.WS_PORT))
    bridge = PermissionBridge()
    agent, mcp_conns = await build_agent(bridge)

    try:
        port = base_port
        for _ in range(cfg.PORT_RETRY_COUNT):
            try:
                await start_server(agent, host, port, bridge=bridge)
                return
            except OSError as e:
                if e.errno in (10048, 98):
                    logger.warning("端口 %d 被占用，尝试 %d", port, port + 1)
                    port += 1
                else:
                    raise

        logger.error("端口 %d-%d 全部不可用，退出", base_port, port - 1)
        sys.exit(1)
    finally:
        # MCP server 子进程/连接逐个收尾，不能让孤儿进程挂着
        await asyncio.gather(*(c.stop() for c in mcp_conns), return_exceptions=True)
        # 正常退出 / Ctrl-C / 端口耗尽，都把 SQLite 连接关干净（WAL checkpoint 随之完成）
        if agent.session_manager is not None:
            agent.session_manager.close()
        # 记忆派生库也收尾：不关的话 WAL 会跨会话累积（它是独立库，不跟着 session_manager 走）
        from kittymind.memory.index import reset_memory_index
        from kittymind.memory.ledger import reset_memory_ledger
        reset_memory_index()
        reset_memory_ledger()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
