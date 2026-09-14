"""命令行单轮对话入口（无跨轮记忆）"""

from dotenv import load_dotenv
load_dotenv()

from kittymind.agent import KittyAgent
from kittymind.core import BaseAgentLLM
from kittymind.tools import GetCurrentTimeTool


def main():
    llm = BaseAgentLLM()
    agent = KittyAgent(
        name="chat_agent",
        llm=llm,
        tools=[GetCurrentTimeTool()],
        system_prompt="你是一个聊天助手，可以进行日常对话。",
    )

    print("=== Agent 对话 ===")
    print("输入 'quit' 退出\n")

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

        print("Assistant: ", end="", flush=True)
        for chunk in agent.stream_run(None, user_input):
            print(chunk, end="", flush=True)
        print("\n")


if __name__ == "__main__":
    main()
