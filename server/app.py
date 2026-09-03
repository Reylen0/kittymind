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

from kittymind.core.llm import BaseAgentLLM
from kittymind.events.bus import EventBus
from kittymind.session.manager import SessionManager
from kittymind.tools import GetCurrentTimeTool
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
    base_port = int(os.getenv("WS_PORT", "8765"))
    agent = build_agent()

    # 如果首选端口被占用，自动往后找一个可用端口
    port = base_port
    for _ in range(10):
        try:
            print(f"[ready] ws://{host}:{port}", flush=True)
            await start_server(agent, host, port)
            return
        except OSError as e:
            if e.errno == 10048 or e.errno == 98:  # Windows / Linux address in use
                print(f"[warn] port {port} in use, trying {port + 1}", flush=True)
                port += 1
            else:
                raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
