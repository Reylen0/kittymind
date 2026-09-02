"""命令行持续对话入口"""

from dotenv import load_dotenv

from baseagent.agent.tool_agent import ToolAgent
from baseagent.tools import GetCurrentTimeTool

load_dotenv()

from baseagent.core import BaseAgentLLM
from baseagent.agent import SimpleAgent


def main():
    llm = BaseAgentLLM()

    chat_agent = ToolAgent(
        name="chat_agent",
        llm=llm,
        tools=[GetCurrentTimeTool()],
        system_prompt="你是一个聊天助手，可以进行日常对话。",
        description="处理日常闲聊，问候等普通对话",
    )

    print("=== Agent 对话 ===")
    print("输入 'quit' 或 'exit' 退出，输入 'clear' 清空历史\n")

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print("再见！")
            break

        if user_input.lower() == "clear":
            chat_agent.clear_history()
            print("历史已清空\n")
            continue

        print("Assistant: ", end="", flush=True)
        for chunk in chat_agent.stream_run("session_1", user_input):
            print(chunk, end="", flush=True)
        print("\n")


if __name__ == "__main__":
    main()
