"""KittyMind Python server 启动入口。

用法:
    uv run python -m server.app

环境变量:
    WS_HOST  — 监听地址（默认 127.0.0.1）
    WS_PORT  — 监听端口（默认 8765）

Electron 通过监听 stdout 中的 "[ready] ws://..." 行确认 server 就绪。
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from kittymind.tools.builtin.bash_tool import BashTool
from kittymind.tools.builtin.file_read_tool import FileReadTool
from kittymind.tools.builtin.file_write_tool import FileWriteTool
from kittymind.tools.builtin.write_memory_tool import WriteMemoryTool
from kittymind.tools.builtin.task_tool import TaskTool
from kittymind.memory.store import MemoryStore
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
from kittymind.tools import GetCurrentTimeTool
from kittymind.agent import KittyAgent
from kittymind.prompts import build_system_prompt
from server.ws_server import start_server


def build_agent(bridge: PermissionBridge) -> KittyAgent:
    llm = BaseAgentLLM()
    memory_store = MemoryStore()
    base_tools = [
        GetCurrentTimeTool(),
        FileReadTool(),
        FileWriteTool(),
        BashTool(),
        WriteMemoryTool(memory_store),
    ]
    task_tool = TaskTool(
        llm=llm,
        sub_tools=base_tools,
        ask_fn=bridge.ask,
    )
    all_tools = base_tools + [task_tool]
    agent = KittyAgent(
        name="kitty",
        llm=llm,
        tools=all_tools,
        system_prompt=build_system_prompt(all_tools),
        event_bus=EventBus(),
        session_manager=SessionManager(),
        workspace_manager=WorkspaceManager(),
        memory=memory_store,
        ask_fn=bridge.ask,
    )
    return agent


async def main() -> None:
    host = os.getenv("WS_HOST", cfg.WS_HOST)
    base_port = int(os.getenv("WS_PORT", cfg.WS_PORT))
    bridge = PermissionBridge()
    agent = build_agent(bridge)

    port = base_port
    for _ in range(cfg.PORT_RETRY_COUNT):
        try:
            await start_server(agent, host, port, bridge=bridge)
            return
        except OSError as e:
            if e.errno in (10048, 98):
                print(f"[warn] port {port} in use, trying {port + 1}", flush=True)
                port += 1
            else:
                raise

    print(f"[error] no available port in range {base_port}-{port - 1}", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
