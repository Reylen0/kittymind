"""KittyAgent 异步演示（展示会话持久化 + 事件总线用法）"""

import asyncio
from dotenv import load_dotenv

from kittymind.tools.builtin.bash_tool import BashTool
from kittymind.tools.builtin.clipboard_tool import ClipboardTool
from kittymind.tools.builtin.file_read_tool import FileReadTool
from kittymind.tools.builtin.file_write_tool import FileWriteTool
from kittymind.tools.builtin.screenshot_tool import ScreenshotTool
load_dotenv()

from kittymind.session import SessionManager
from kittymind.core import BaseAgentLLM
from kittymind.events import EventBus, AGENT_THINKING, AGENT_TOOL_CALL, AGENT_TOOL_RESULT, AGENT_DONE
from kittymind.tools import GetCurrentTimeTool
from kittymind.agent import KittyAgent


def pick_session(mgr: SessionManager) -> str:
    sessions = mgr.list_sessions()
    if sessions:
        print("=== 历史会话 ===")
        for i, s in enumerate(sessions[:5], 1):
            print(f"  {i}. {s['title']:<20}  {s['created_at'][:10]}  [{s['id'][:8]}]")
        print("  0. 新建会话")
        choice = input("选择 (直接回车=新建): ").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(sessions[:5]):
                sid = sessions[idx - 1]["id"]
                history = mgr.load_history(sid)
                print(f"已加载会话 [{sid[:8]}]，共 {len(history)} 条历史消息\n")
                return sid
    sid = mgr.create_session()
    print(f"已创建新会话 [{sid[:8]}]\n")
    return sid


async def main():
    mgr = SessionManager()
    session_id = pick_session(mgr)

    llm = BaseAgentLLM()
    bus = EventBus()

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
        print("\n[✨ done]", flush=True)

    agent = KittyAgent(
        name="kitty",
        llm=llm,
        tools=[
            GetCurrentTimeTool(), 
            ClipboardTool(), 
            ScreenshotTool(),
            FileReadTool(),
            FileWriteTool(),
            BashTool(),
        ],
        system_prompt="你是一个聪明可爱的桌面助手 KittyMind，可以进行日常对话并使用工具。",
        event_bus=bus,
        session_manager=mgr,
    )

    print(f"=== KittyAgent  session={session_id[:8]}...  输入 quit 退出 ===\n")

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
        async for chunk in agent.async_stream_run(session_id, user_input):
            print(chunk, end="", flush=True)
        print()


if __name__ == "__main__":
    asyncio.run(main())
