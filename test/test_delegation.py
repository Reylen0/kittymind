"""delegation.py 单元测试（无需 LLM）"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from kittymind.agent.delegation import (
    DelegationBudget,
    DelegationLimit,
    can_delegate,
    child_scope,
    current_budget,
    reset_root_budget,
    reset_root_session,
    set_root_budget,
    set_root_session,
)


# ── 基础守护 ──────────────────────────────────────────────────────

def test_can_delegate_none():
    """budget=None（CLI 无预算）时允许委派。"""
    assert can_delegate(None) is True


def test_can_delegate_within_limits():
    b = DelegationBudget(depth=0, spawned=0, max_depth=3, max_total=8)
    assert can_delegate(b) is True


def test_can_delegate_depth_exceeded():
    b = DelegationBudget(depth=3, spawned=0, max_depth=3, max_total=8)
    assert can_delegate(b) is False


def test_can_delegate_total_exceeded():
    b = DelegationBudget(depth=0, spawned=8, max_depth=3, max_total=8)
    assert can_delegate(b) is False


# ── child_scope：sibling 路径 ─────────────────────────────────────

def test_sibling_depth_and_spawned():
    """两次顺序委派：depth 进出归零，spawned 累计到 2。"""
    b = DelegationBudget(max_depth=3, max_total=8)

    with child_scope(b):
        assert b.depth == 1
        assert b.spawned == 1
    assert b.depth == 0
    assert b.spawned == 1  # 不回退

    with child_scope(b):
        assert b.depth == 1
        assert b.spawned == 2
    assert b.depth == 0
    assert b.spawned == 2


def test_nested_depth():
    """三层嵌套：depth 逐层递增再逐层回退，spawned 累计到 3。"""
    b = DelegationBudget(max_depth=3, max_total=8)

    with child_scope(b):         # depth=1
        assert b.depth == 1
        with child_scope(b):     # depth=2
            assert b.depth == 2
            with child_scope(b): # depth=3
                assert b.depth == 3
            assert b.depth == 2
        assert b.depth == 1
    assert b.depth == 0
    assert b.spawned == 3


def test_depth_limit_raises():
    """depth 已到上限时 child_scope 抛 DelegationLimit。"""
    b = DelegationBudget(depth=3, spawned=0, max_depth=3, max_total=8)
    with pytest.raises(DelegationLimit), child_scope(b):
        pass


def test_total_limit_raises():
    """spawned 已到上限时 child_scope 抛 DelegationLimit。"""
    b = DelegationBudget(depth=0, spawned=8, max_depth=3, max_total=8)
    with pytest.raises(DelegationLimit), child_scope(b):
        pass


def test_depth_restored_on_limit():
    """超限抛异常后，depth 不会残留（但此处 depth 未被修改）。"""
    b = DelegationBudget(depth=3, spawned=0, max_depth=3, max_total=8)
    with pytest.raises(DelegationLimit), child_scope(b):
        pass
    assert b.depth == 3  # 超限时未曾 +1，原值保持


# ── child_scope：None budget（CLI 兜底）──────────────────────────

def test_none_budget_creates_temporary():
    """budget=None 时 child_scope 临时创建 budget 并在退出后还原。"""
    # 确保当前 ContextVar 为 None
    token = set_root_budget(3, 8)
    reset_root_budget(token)  # 还原为 None

    assert current_budget() is None
    with child_scope(None) as active:
        assert active is not None
        assert active.depth == 1
        # 进入 scope 后 ContextVar 被设置
        assert current_budget() is active
    # 退出后还原为 None
    assert current_budget() is None


# ── set_root_budget / reset ───────────────────────────────────────

def test_set_and_reset_root_budget():
    token = set_root_budget(3, 8)
    b = current_budget()
    assert b is not None
    assert b.max_depth == 3
    assert b.max_total == 8
    reset_root_budget(token)
    assert current_budget() is None


def test_set_and_reset_root_session():
    token = set_root_session("sess-123")
    from kittymind.agent.delegation import _root_session_id
    assert _root_session_id.get() == "sess-123"
    reset_root_session(token)
    assert _root_session_id.get() is None


# ── ContextVar 跨 to_thread（sibling 共享同一 budget 对象）─────────

def test_cross_thread_spawned_accumulates():
    """
    asyncio.to_thread 用 copy_context() 把 context 浅拷贝进线程：
    ContextVar 映射到同一 budget 对象，spawned 自增跨线程可见。
    """
    b = DelegationBudget(max_depth=3, max_total=8)
    budget_token = set_root_budget(3, 8)
    # 覆盖为我们自己的 budget 对象
    from kittymind.agent.delegation import _budget
    _budget.set(b)

    results = []

    def worker():
        ctx_budget = current_budget()
        with child_scope(ctx_budget):
            results.append(ctx_budget.spawned)

    ctx = copy_context()
    with ThreadPoolExecutor(max_workers=2) as pool:
        # 串行提交（模拟 kitty_agent 主循环对多 tool_call 的串行 to_thread）
        pool.submit(ctx.run, worker).result()
        pool.submit(ctx.run, worker).result()

    # 两次委派后 spawned 应为 2
    assert b.spawned == 2

    reset_root_budget(budget_token)


# ── spawned 在异常路径下不泄露 ────────────────────────────────────

def test_spawned_not_incremented_when_limit_exceeded():
    """超限时 spawned 不被自增（检查 limit 在修改前发生）。"""
    b = DelegationBudget(depth=0, spawned=7, max_depth=3, max_total=8)
    # 正常委派：spawned 7→8
    with child_scope(b):
        pass
    assert b.spawned == 8

    # 此时已达上限，再委派应抛 DelegationLimit 且 spawned 不变
    with pytest.raises(DelegationLimit), child_scope(b):
        pass
    assert b.spawned == 8


# ── TaskTool 隔离测试（mock LLM，无真实 API）─────────────────────

class _FakeLLM:
    model = "fake"

    async def async_stream_with_tools(self, messages=None, tools=None, **kwargs):
        from kittymind.core.llm_response import StreamEvent
        yield StreamEvent(type="text_delta", delta="任务完成：结果是42")


class _FakeTool:
    name = "echo"
    description = "回显输入。"
    param_class = None
    is_async = False

    def run(self, parameters):
        return "echo: ok"

    def to_schema(self):
        return {"type": "function", "function": {"name": "echo", "description": "echo", "parameters": {}}}


async def test_task_tool_returns_footnote():
    """TaskTool 正常委派：返回包含脚注的文字。"""
    from kittymind.tools.builtin.task_tool import TaskTool

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[_FakeTool()])
        from kittymind.tools.builtin.task_tool import TaskInput
        result = await tool.aexecute(TaskInput(prompt="测试任务"))
        assert "任务完成" in result.content
        assert "子任务完成" in result.content  # 脚注
        assert "工具" in result.content
        assert "tokens" in result.content
    finally:
        reset_root_budget(token)


async def test_task_tool_depth_limit_returns_text():
    """深度达上限时返回文字提示，不抛异常。"""
    from kittymind.tools.builtin.task_tool import TaskTool, TaskInput

    b = DelegationBudget(depth=3, spawned=0, max_depth=3, max_total=8)
    budget_token_inner = set_root_budget(3, 8)
    from kittymind.agent.delegation import _budget
    _budget.set(b)

    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[_FakeTool()])
        result = await tool.aexecute(TaskInput(prompt="超限任务"))
        assert "上限" in result.content
    finally:
        reset_root_budget(budget_token_inner)


async def test_task_tool_emit_no_crash_when_event_bus_none():
    """event_bus=None 时 _emit 不抛异常，静默降级。"""
    from kittymind.tools.builtin.task_tool import TaskTool

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[_FakeTool()], event_bus=None)
        await tool._emit("subagent.start", {"depth": 1})  # 静默降级，不崩
    finally:
        reset_root_budget(token)


async def test_async_stream_run_root_false_does_not_reset_budget():
    """子 Agent 传 root=False 时，async_stream_run 不得重置委派预算/根 session 标签。

    async_stream_run 默认 root=True 会无条件 set_root_budget（新建全零预算）——
    子 Agent 若直接调用它，递归防爆栏当场归零，等于给子 Agent 开无限委派权限。
    root=False 必须原样沿用父级已经建立的作用域。
    """
    from kittymind.agent.kitty_agent import KittyAgent
    from kittymind.agent.delegation import _budget, _root_session_id
    from kittymind.core.llm_response import StreamEvent

    class _DoneLLM:
        async def async_stream_with_tools(self, messages=None, tools=None, **kwargs):
            yield StreamEvent(type="text_delta", delta="done")

    b = DelegationBudget(depth=1, spawned=1, max_depth=3, max_total=8)
    budget_token = set_root_budget(3, 8)
    _budget.set(b)  # 覆盖为「子 Agent 视角下父级已递增过深度」的那份 budget
    session_token = set_root_session("root-session")

    try:
        agent = KittyAgent(
            name="sub", llm=_DoneLLM(), system_prompt="sp", tools=[],
            aux_llm=None, interactive=False,
        )
        result = "".join([
            chunk async for chunk in
            agent.async_stream_run(session_id=None, input_text="hi", root=False)
        ])

        assert result == "done"
        # 委派预算原样未动：既不是新对象，深度/总数也没被清零
        assert current_budget() is b
        assert b.depth == 1
        assert b.spawned == 1
        # 根 session 标签没被子 Agent 的 session_id=None 覆盖
        assert _root_session_id.get() == "root-session"
    finally:
        reset_root_session(session_token)
        reset_root_budget(budget_token)


async def test_task_tool_allowed_tools_filter():
    """allowed_tools 过滤正确；未知名字忽略不崩，过滤后为空则回退全量。"""
    from kittymind.tools.builtin.task_tool import TaskTool, TaskInput

    class ToolA:
        name = "tool_a"
        description = "A"
        param_class = None
        is_async = False
        def run(self, p): return "a"
        def to_schema(self):
            return {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {}}}

    class ToolB:
        name = "tool_b"
        description = "B"
        param_class = None
        is_async = False
        def run(self, p): return "b"
        def to_schema(self):
            return {"type": "function", "function": {"name": "tool_b", "description": "B", "parameters": {}}}

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[ToolA(), ToolB()])
        # 只允许 tool_a
        result = await tool.aexecute(TaskInput(prompt="过滤测试", allowed_tools=["tool_a"]))
        assert "任务完成" in result.content

        # 指定不存在的工具 → 回退全量，不崩
        result2 = await tool.aexecute(TaskInput(prompt="回退测试", allowed_tools=["nonexistent"]))
        assert "任务完成" in result2.content
    finally:
        reset_root_budget(token)


class _ToolThenTextLLM:
    """第一步发起一次工具调用，第二步给最终文本——驱动子 Agent 的计数路径。"""

    model = "fake"

    def __init__(self):
        self._step = 0

    async def async_stream_with_tools(self, messages=None, tools=None, **kwargs):
        from kittymind.core.llm_response import StreamEvent
        self._step += 1
        if self._step == 1:
            yield StreamEvent(type="tool_calls_done", tool_calls=[
                {"id": "c1", "type": "function",
                 "function": {"name": "echo", "arguments": "{}"}}
            ])
        else:
            yield StreamEvent(type="text_delta", delta="子任务完成：42")


async def test_task_tool_counts_subagent_tool_calls():
    """子 Agent 调 1 次工具 → 脚注记「1 工具」。

    计数器已从 callbacks 迁到 EventBus：counts 来自子 Agent 私有总线上的
    AGENT_TOOL_CALL 事件。私有总线（而非共享总线 + session_id 过滤）保证
    并行多个 task 时不串计数。
    """
    from kittymind.tools.base import ToolResult
    from kittymind.tools.builtin.task_tool import TaskTool, TaskInput

    class _Echo:
        name = "echo"
        description = "回显输入。"
        param_class = None
        is_async = False

        def run(self, parameters):
            return ToolResult(True, "echo: ok")

        def to_schema(self):
            return {"type": "function", "function": {
                "name": "echo", "description": "echo", "parameters": {}}}

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_ToolThenTextLLM(), sub_tools=[_Echo()])
        result = await tool.aexecute(TaskInput(prompt="测试计数"))
        assert "1 工具" in result.content
    finally:
        reset_root_budget(token)
