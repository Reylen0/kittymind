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

from dotenv import load_dotenv

load_dotenv()

from baseagent.core.llm import BaseAgentLLM
from baseagent.events.bus import EventBus
from baseagent.session.manager import SessionManager
from baseagent.tools.builtin import GetCurrentTimeTool
from kittymind.agent import KittyAgent
from server.ws_server import start_server


def build_agent() -> KittyAgent:
    return KittyAgent(
        name="kitty",
        llm=BaseAgentLLM(),
        tools=[GetCurrentTimeTool()],
        system_prompt="你是一个聪明可爱的桌面助手 KittyMind，可以进行日常对话并使用工具。",
        event_bus=EventBus(),
        session_manager=SessionManager(),
    )


async def main() -> None:
    host = os.getenv("WS_HOST", "127.0.0.1")
    port = int(os.getenv("WS_PORT", "8765"))

    agent = build_agent()

    # Electron 监听这行确认 server 就绪后再创建窗口
    print(f"[ready] ws://{host}:{port}", flush=True)

    await start_server(agent, host, port)


if __name__ == "__main__":
    asyncio.run(main())
