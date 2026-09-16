"""delegation.py 单元测试（无需 LLM）"""

import threading
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
    with pytest.raises(DelegationLimit):
        with child_scope(b):
            pass


def test_total_limit_raises():
    """spawned 已到上限时 child_scope 抛 DelegationLimit。"""
    b = DelegationBudget(depth=0, spawned=8, max_depth=3, max_total=8)
    with pytest.raises(DelegationLimit):
        with child_scope(b):
            pass


def test_depth_restored_on_limit():
    """超限抛异常后，depth 不会残留（但此处 depth 未被修改）。"""
    b = DelegationBudget(depth=3, spawned=0, max_depth=3, max_total=8)
    with pytest.raises(DelegationLimit):
        with child_scope(b):
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
    with pytest.raises(DelegationLimit):
        with child_scope(b):
            pass
    assert b.spawned == 8


# ── TaskTool 隔离测试（mock LLM，无真实 API）─────────────────────

class _FakeLLM:
    model = "fake"

    def invoke(self, messages, tools=None, **kwargs):
        from kittymind.core.llm_response import LLMResponse
        return LLMResponse(content="任务完成：结果是42", tool_calls=[])


class _FakeTool:
    name = "echo"
    description = "回显输入。"
    param_class = None

    def run(self, parameters):
        return "echo: ok"

    def to_schema(self):
        return {"type": "function", "function": {"name": "echo", "description": "echo", "parameters": {}}}


def test_task_tool_returns_footnote():
    """TaskTool 正常委派：返回包含脚注的文字。"""
    from kittymind.tools.builtin.task_tool import TaskTool

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[_FakeTool()])
        from kittymind.tools.builtin.task_tool import TaskInput
        result = tool.execute(TaskInput(prompt="测试任务"))
        assert "任务完成" in result.content
        assert "子任务完成" in result.content  # 脚注
        assert "工具" in result.content
        assert "tokens" in result.content
    finally:
        reset_root_budget(token)


def test_task_tool_depth_limit_returns_text():
    """深度达上限时返回文字提示，不抛异常。"""
    from kittymind.tools.builtin.task_tool import TaskTool, TaskInput

    b = DelegationBudget(depth=3, spawned=0, max_depth=3, max_total=8)
    budget_token_inner = set_root_budget(3, 8)
    from kittymind.agent.delegation import _budget
    _budget.set(b)

    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[_FakeTool()])
        result = tool.execute(TaskInput(prompt="超限任务"))
        assert "上限" in result.content
    finally:
        reset_root_budget(budget_token_inner)


def test_task_tool_emit_safe_no_crash_when_loop_none():
    """loop=None 时 _emit_safe 不抛异常。"""
    from kittymind.tools.builtin.task_tool import TaskTool

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[_FakeTool()], loop=None, event_bus=None)
        tool._emit_safe("subagent.start", {"depth": 1})  # 静默降级，不崩
    finally:
        reset_root_budget(token)


def test_task_tool_allowed_tools_filter():
    """allowed_tools 过滤正确；未知名字忽略不崩，过滤后为空则回退全量。"""
    from kittymind.tools.builtin.task_tool import TaskTool, TaskInput

    class ToolA:
        name = "tool_a"
        description = "A"
        param_class = None
        def run(self, p): return "a"
        def to_schema(self): return {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {}}}

    class ToolB:
        name = "tool_b"
        description = "B"
        param_class = None
        def run(self, p): return "b"
        def to_schema(self): return {"type": "function", "function": {"name": "tool_b", "description": "B", "parameters": {}}}

    token = set_root_budget(3, 8)
    try:
        tool = TaskTool(llm=_FakeLLM(), sub_tools=[ToolA(), ToolB()])
        # 只允许 tool_a
        result = tool.execute(TaskInput(prompt="过滤测试", allowed_tools=["tool_a"]))
        assert "任务完成" in result.content

        # 指定不存在的工具 → 回退全量，不崩
        result2 = tool.execute(TaskInput(prompt="回退测试", allowed_tools=["nonexistent"]))
        assert "任务完成" in result2.content
    finally:
        reset_root_budget(token)
