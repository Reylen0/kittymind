"""task 工具 —— 把子任务委派给独立的子 Agent，实现 context 隔离。

子 Agent 拥有全新的 messages[]，干完活只把最终文字返回给父 Agent。
父 Agent 的 context 里不会出现子任务的中间工具调用结果。
"""

from pydantic import BaseModel, Field

from ...prompts import build_system_prompt

from ..base import BaseTool


class TaskInput(BaseModel):
    prompt: str = Field(description="交给子 Agent 的任务描述，需完整说明目标和上下文")


class TaskTool(BaseTool):
    """
    在独立对话上下文中运行子任务，只返回最终结论文字。

    适用场景：
    - 需要大量读文件的调查性任务（避免污染父 context）
    - 相对独立的子任务，结果可以用一段文字表达
    - 父 Agent 想保持 context 整洁时

    注意：子 Agent 与父 Agent 共享同一工作目录，文件修改对双方可见。
    子 Agent 不含 task 工具本身，防止递归嵌套。
    """

    name: str = "task"
    description: str = (
        "在独立上下文中运行子任务，只返回最终结论文字。"
        "适合调查性或独立性较强的子任务，防止大量中间步骤污染父 Agent 的上下文。"
        "子 Agent 拥有完整工具集（不含 task 工具本身），与父 Agent 共享工作目录。"
    )
    param_class = TaskInput

    def __init__(self, llm, sub_tools: list[BaseTool], ask_fn=None):
        self._llm = llm
        self._sub_tools = sub_tools
        self._system_prompt = build_system_prompt(sub_tools)
        self._ask_fn = ask_fn

    def execute(self, parameters: TaskInput) -> str:
        from ...agent.tool_agent import ToolAgent
        from ..permission import PermissionToolExecutor

        sub_agent = ToolAgent(
            name="kitty-sub",
            llm=self._llm,
            system_prompt=self._system_prompt,
            tools=self._sub_tools,
        )
        if self._ask_fn is not None:
            sub_agent.tool_executor = PermissionToolExecutor(
                sub_agent.tool_registry,
                ask_fn=self._ask_fn,
            )

        print(
            f"\n[task] 子任务开始: "
            f"{parameters.prompt[:80]}{'…' if len(parameters.prompt) > 80 else ''}",
            flush=True,
        )
        try:
            result = sub_agent.run(session_id=None, input_text=parameters.prompt)
        except Exception as e:
            result = f"子任务执行失败: {e}"

        print("[task] 子任务完成", flush=True)
        return result
