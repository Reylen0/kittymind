"""命令行持续对话入口"""

from dotenv import load_dotenv
load_dotenv()

from kittymind.agent import ToolAgent
from kittymind.core import BaseAgentLLM
from kittymind.tools import GetCurrentTimeTool


def main():
    llm = BaseAgentLLM()
    agent = ToolAgent(
        name="chat_agent",
        llm=llm,
        tools=[GetCurrentTimeTool()],
        system_prompt="你是一个聊天助手，可以进行日常对话。",
    )

    print("=== Agent 对话 ===")
    print("输入 'quit' 退出，输入 'clear' 清空历史\n")

    session_id = "session_1"
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
            agent.clear_history(session_id)
            print("历史已清空\n")
            continue

        print("Assistant: ", end="", flush=True)
        for chunk in agent.stream_run(session_id, user_input):
            print(chunk, end="", flush=True)
        print("\n")


if __name__ == "__main__":
    main()
