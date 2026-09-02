"""KittyAgent 异步演示（展示事件总线用法）"""

import asyncio
from dotenv import load_dotenv

load_dotenv()

from baseagent.core import BaseAgentLLM
from baseagent.events import EventBus, AGENT_THINKING, AGENT_TOOL_CALL, AGENT_TOOL_RESULT, AGENT_DONE
from baseagent.tools import GetCurrentTimeTool
from kittymind.agent import KittyAgent


async def main():
    llm = BaseAgentLLM()
    bus = EventBus()

    # 注册事件处理器（演示桌宠情绪驱动点）
    @bus.on(AGENT_THINKING)
    async def on_thinking(et, data):
        print("\n[🤔 thinking...]", flush=True)

    @bus.on(AGENT_TOOL_CALL)
    async def on_tool_call(et, data):
        print(f"\n[🔧 tool: {data['name']}]", flush=True)

    @bus.on(AGENT_TOOL_RESULT)
    async def on_tool_result(et, data):
        print(f"[✅ result: {data['result']}]", flush=True)

    @bus.on(AGENT_DONE)
    async def on_done(et, data):
        print("\n[done]", flush=True)

    agent = KittyAgent(
        name="kitty",
        llm=llm,
        tools=[GetCurrentTimeTool()],
        system_prompt="你是一个聪明可爱的桌面助手 KittyMind，可以进行日常对话并使用工具。",
        event_bus=bus,
    )

    print("=== KittyAgent 异步对话 ===")
    print("输入 'quit' 退出\n")

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input or user_input.lower() in ("quit", "exit"):
            print("再见！")
            break

        print("KittyMind: ", end="", flush=True)
        async for chunk in agent.async_stream_run("session_1", user_input):
            print(chunk, end="", flush=True)
        print()


if __name__ == "__main__":
    asyncio.run(main())
