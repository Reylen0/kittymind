"""task 工具 —— 把子任务委派给独立的子 Agent，实现 context 隔离。

子 Agent 拥有全新的 messages[]，干完活只把最终文字返回给父 Agent。
父 Agent 的 context 里不会出现子任务的中间工具调用结果。

  - 深度/总数守护（DelegationBudget）
  - orchestrator / leaf 角色（depth < max_depth 则追加 task 工具）
  - allowed_tools 约束子 Agent 可用工具集
  - 用量回传（工具数/耗时/估算 tokens）
  - subagent.start / subagent.done 生命周期事件

本工具是异步工具（`is_async = True`）：子 Agent 走 `async_stream_run(root=False)`，
`root=False` 表示复用父 Agent 已经建立的作用域——不重新设置 cwd、不重置委派预算
（否则递归防爆栏归零）、不覆盖根 session 标签，详见 `KittyAgent.async_stream_run`。
"""

import time

from pydantic import BaseModel, Field

from ...callbacks.base import BaseCallBack
from ...config import cfg
from ...context import estimate_tokens
from ...events.types import SUBAGENT_DONE, SUBAGENT_START
from ...prompts import build_subtask_prompt
from ...agent.delegation import (
    DelegationLimit,
    _root_session_id,
    can_delegate,
    child_scope,
    current_budget,
)
from ..base import BaseTool, ToolResult


class TaskInput(BaseModel):
    prompt: str = Field(description="交给子 Agent 的任务描述，需完整说明目标和上下文")
    allowed_tools: list[str] | None = Field(
        default=None,
        description="（可选）限制子 Agent 可用的工具名称列表；不填则使用全部工具。",
    )


class _ToolCallCounter(BaseCallBack):
    """统计子 Agent 的工具调用次数，挂入 callbacks 无侵入地计数。"""

    def __init__(self) -> None:
        self.count = 0

    def on_tool_start(self, name: str, args) -> None:
        self.count += 1


class TaskTool(BaseTool):
    """
    在独立上下文中运行子任务，只返回最终结论文字。

    适用场景：
    - 需要大量读文件的调查性任务（避免污染父 context）
    - 相对独立的子任务，结果可以用一段文字表达
    - 父 Agent 想保持 context 整洁时

    注意：子 Agent 与父 Agent 共享同一工作目录，文件修改对双方可见。
    """

    name: str = "task"
    description: str = (
        "在独立上下文中运行子任务，只返回最终结论文字。"
        "适合调查性或独立性较强的子任务，防止大量中间步骤污染父 Agent 的上下文。"
        "子 Agent 拥有完整工具集（深度未达上限时含 task 工具），与父 Agent 共享工作目录。"
    )
    param_class = TaskInput
    is_async = True

    def __init__(
        self,
        llm,
        sub_tools: list[BaseTool],
        ask_fn=None,
        event_bus=None,
    ):
        self._llm = llm
        self._sub_tools = sub_tools
        self._ask_fn = ask_fn
        self._event_bus = event_bus

    def execute(self, parameters: TaskInput) -> ToolResult:
        raise RuntimeError("task 是异步工具，只能通过 aexecute() 调用")

    # ── 事件发送 ─────────────────────────────────────────────────

    async def _emit(self, event: str, data: dict) -> None:
        if self._event_bus is None:
            return
        data.setdefault("session_id", _root_session_id.get())
        await self._event_bus.emit(event, data)

    # ── 执行入口 ──────────────────────────────────────────────────

    async def aexecute(self, parameters: TaskInput) -> ToolResult:
        from ...agent.kitty_agent import KittyAgent

        budget = current_budget()
        if not can_delegate(budget):
            b = budget
            return ToolResult(True, (
                f"委派已达上限（深度 {b.max_depth} / 总数 {b.max_total}），"
                "请直接完成任务或将子任务拆得更小。"
            ))

        # 按 allowed_tools 过滤；过滤后为空则回退全量（防止无工具空转）
        if parameters.allowed_tools:
            allowed = set(parameters.allowed_tools)
            child_tools: list[BaseTool] = [
                t for t in self._sub_tools if t.name in allowed
            ] or list(self._sub_tools)
        else:
            child_tools = list(self._sub_tools)

        try:
            with child_scope(budget) as active:
                # orchestrator：深度还没到上限，给子 Agent 追加 task 工具
                if active.depth < active.max_depth:
                    child_tools = child_tools + [
                        TaskTool(
                            llm=self._llm,
                            sub_tools=child_tools,
                            ask_fn=self._ask_fn,
                            event_bus=self._event_bus,
                        )
                    ]
                # leaf：不加 task 工具，提示词也不提 task

                system_prompt = build_subtask_prompt(child_tools)
                counter = _ToolCallCounter()
                sub_agent = KittyAgent(
                    name="kitty-sub",
                    llm=self._llm,
                    system_prompt=system_prompt,
                    tools=child_tools,
                    callbacks=[counter],
                    max_iterations=cfg.SUBAGENT_MAX_ITERATIONS,
                    ask_fn=self._ask_fn,
                )

                depth = active.depth
                t0 = time.monotonic()
                await self._emit(SUBAGENT_START, {
                    "depth": depth,
                    "prompt_preview": parameters.prompt[:120],
                })

                ok = True
                result = ""
                try:
                    result = "".join([
                        chunk async for chunk in sub_agent.async_stream_run(
                            session_id=None, input_text=parameters.prompt, root=False,
                        )
                    ])
                    if not result.strip():
                        ok = False
                        result = "子任务无输出"
                except Exception as e:
                    ok = False
                    result = f"子任务执行失败: {e}"
                finally:
                    elapsed = time.monotonic() - t0
                    tokens = estimate_tokens(sub_agent.last_messages)
                    await self._emit(SUBAGENT_DONE, {
                        "depth": depth,
                        "ok": ok,
                        "tool_calls": counter.count,
                        "tokens": tokens,
                        "elapsed": round(elapsed, 2),
                    })

                footnote = (
                    f"\n\n[子任务完成 · {counter.count} 工具 · "
                    f"~{tokens} tokens · {elapsed:.1f}s]"
                )
                return ToolResult(ok, result + footnote)

        except DelegationLimit as e:
            return ToolResult(True, str(e))
