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

    port = base_port
    for _ in range(20):
        try:
            await start_server(agent, host, port)
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
