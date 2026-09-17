"""Phase 12 — 工具守护栏单元测试（无需 LLM）"""

import json
import pytest

from kittymind.tools.guardrails import (
    GuardrailController,
    GuardrailTurnState,
    ToolCallSignature,
)
from kittymind.tools.executor import ToolExecutor, TurnContext
from kittymind.tools.base import BaseTool, ToolResult


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def ctrl():
    """交互态控制器（warn 不 block）。"""
    return GuardrailController(interactive=True)


@pytest.fixture
def hard_ctrl():
    """非交互态控制器（启用 block）。"""
    return GuardrailController(interactive=False)


@pytest.fixture
def state():
    """每个测试独立的一份每轮状态（模拟一轮对话）。"""
    return GuardrailTurnState()


# ── GuardrailController — 精确失败循环 warn ──────────────────────────

def test_exact_failure_warn(ctrl, state):
    args = {"command": "cat /nope"}
    for i in range(1, 4):
        dec = ctrl.after_call(state, "bash", args, "Error: no such file", True)
        if i < 2:
            assert dec.action == "allow", f"should allow at count {i}"
        else:
            assert dec.action == "warn"
            assert "相同参数" in dec.message

def test_exact_failure_no_block_in_interactive(ctrl, state):
    """交互态：即使超过 block 阈值也不 block（only warn）。"""
    args = {"command": "cat /nope"}
    for _ in range(10):
        dec = ctrl.after_call(state, "bash", args, "Error: no such file", True)
    assert dec.action == "warn"  # 不是 block

def test_exact_failure_block_in_hard_stop(hard_ctrl, state):
    """非交互态：超 block 阈值时 before_call 应 block（任意 block code）。"""
    args = {"command": "cat /nope"}
    for _ in range(5):
        hard_ctrl.after_call(state, "bash", args, "Error: no such file", True)
    dec = hard_ctrl.before_call(state, "bash", args)
    assert dec.is_block


# ── GuardrailController — 同工具不同参失败 warn ─────────────────────

def test_same_tool_failure_warn(ctrl, state):
    for i in range(1, 5):
        dec = ctrl.after_call(state, "bash", {"command": f"cat /nope{i}"}, "Error: fail", True)
        if i < 3:
            assert dec.action in ("allow", "warn") or dec.count < 3
    # 第 3 次起应有 warn
    assert dec.action == "warn"


# ── GuardrailController — 无进展空转 ────────────────────────────────

def test_no_progress_warn_idempotent(ctrl, state):
    args = {"path": "/tmp/file.txt"}
    result = "hello"
    for i in range(1, 4):
        dec = ctrl.after_call(state, "file_read", args, result, False)
        if i < 2:
            assert dec.action == "allow"
        else:
            assert dec.action == "warn"
            assert "相同结果" in dec.message

def test_no_progress_not_triggered_for_mutating(ctrl, state):
    """变更工具（bash）即使返回相同结果也不触发无进展。"""
    for _ in range(5):
        dec = ctrl.after_call(state, "bash", {"command": "echo hi"}, "hi", False)
    assert dec.action == "allow"

def test_no_progress_different_results_resets(ctrl, state):
    """幂等工具返回不同结果时计数重置。"""
    for r in ["a", "b", "a", "a"]:
        dec = ctrl.after_call(state, "file_read", {"path": "x"}, r, False)
    # 最后只连续 2 次 "a"，应仅 warn（count=2）
    assert dec.count == 2


# ── GuardrailController — 连续相同调用流（取代 gate-0）──────────────

def test_identical_streak_block_interactive(ctrl, state):
    """交互态下连续 3 次相同 (sig, result) 也会 block。"""
    args = {"path": "x"}
    result = "same"
    # 第 1、2 次不触发（count=1,2）
    for _ in range(2):
        ctrl.after_call(state, "file_read", args, result, False)
        dec = ctrl.before_call(state, "file_read", args)
        assert not dec.is_block
    # 第 3 次 after_call 后，before_call 应 block
    ctrl.after_call(state, "file_read", args, result, False)
    dec = ctrl.before_call(state, "file_read", args)
    assert dec.is_block
    assert "identical_streak_block" in dec.code

def test_identical_streak_reset_on_different_result(ctrl, state):
    """不同结果打断连续流，重置计数。"""
    for _ in range(3):
        ctrl.after_call(state, "file_read", {"path": "x"}, "same", False)
    ctrl.after_call(state, "file_read", {"path": "x"}, "different", False)
    dec = ctrl.before_call(state, "file_read", {"path": "x"})
    assert not dec.is_block

def test_identical_streak_reset_on_different_args(ctrl, state):
    """不同参数打断连续流。"""
    for _ in range(3):
        ctrl.after_call(state, "file_read", {"path": "x"}, "r", False)
    ctrl.after_call(state, "file_read", {"path": "y"}, "r", False)
    dec = ctrl.before_call(state, "file_read", {"path": "x"})
    assert not dec.is_block


# ── GuardrailController — 进展重置 ──────────────────────────────────

def test_progress_resets_exact_failure(ctrl, state):
    """变更类工具成功后，相同 sig 的失败计数视为新实验。"""
    args = {"command": "cat /nope"}
    # 先触发精确失败 warn
    for _ in range(3):
        ctrl.after_call(state, "bash", args, "Error: fail", True)
    # 一次成功的变更操作（不同命令）
    ctrl.after_call(state, "bash", {"command": "echo ok"}, "ok", False)
    # 再失败，exact 计数应已清零，不再是 warn
    dec = ctrl.after_call(state, "bash", args, "Error: fail", True)
    assert dec.count == 1  # 视为第 1 次，非延续


# ── GuardrailController — 每轮状态由调用方持有 ──────────────────────

def test_new_turn_state_starts_fresh(ctrl):
    """每轮状态是调用方新建的普通对象：换一份新的 state 就是从零开始，不需要专门的 reset 方法。"""
    args = {"command": "cat /nope"}
    old_state = GuardrailTurnState()
    for _ in range(4):
        ctrl.after_call(old_state, "bash", args, "Error: fail", True)

    new_state = GuardrailTurnState()
    dec = ctrl.after_call(new_state, "bash", args, "Error: fail", True)
    assert dec.action == "allow"
    assert dec.count == 1
    # 旧的 state 对象本身不受影响（证明状态确实是独立对象，不是共享单例）
    assert old_state.exact_failure_counts[ToolCallSignature.from_call("bash", args)] == 4


# ── ToolExecutor 集成 ────────────────────────────────────────────────

class _FakeTool(BaseTool):
    """可配置返回值的假工具，直接覆写 run 绕过 param_class 解析。"""
    name: str = "fake"
    description: str = "test"

    def __init__(self, result="ok", ok=True):
        super().__init__()
        self._result = result
        self._ok = ok

    def execute(self, parameters):
        return ToolResult(self._ok, self._result)

    def run(self, args: dict) -> ToolResult:
        return ToolResult(self._ok, self._result)


class _FakeRegistry:
    def __init__(self, tool):
        self._tool = tool
    def get(self, name):
        return self._tool


def _make_call(name="fake", args=None):
    return {
        "id": "call_1",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {}),
        },
    }


def test_executor_block_skips_execution():
    """守护栏 block 时工具不执行，返回合成结果。"""
    tool = _FakeTool("ok")
    ctrl = GuardrailController(interactive=True)
    reg = _FakeRegistry(tool)
    executor = ToolExecutor(reg, guardrail=ctrl)

    args = {}
    call = _make_call(args=args)
    ctx = TurnContext()

    # 跑满 streak（after_call 3 次相同）
    for _ in range(3):
        ctrl.after_call(ctx.guardrail_state, "fake", args, "same", False)

    result = executor.execute(call, ctx=ctx)
    assert "已阻断" in result["content"]
    # 确认工具本身没被调用（返回值不含 "ok"）
    assert "ok" not in result["content"]


def test_executor_warn_appends_guidance():
    """守护栏 warn 时工具结果尾部追加中文指导。"""
    tool = _FakeTool("Error: fail", ok=False)
    ctrl = GuardrailController(interactive=True)
    reg = _FakeRegistry(tool)
    executor = ToolExecutor(reg, guardrail=ctrl)

    args = {"cmd": "x"}
    call = _make_call(args=args)
    ctx = TurnContext()

    # 执行 2 次触发 warn（exact_failure_warn 阈值 = 2）
    executor.execute(call, ctx=ctx)
    result = executor.execute(call, ctx=ctx)
    assert "守护栏警告" in result["content"]


def test_executor_hard_deny_without_ask_fn():
    """arm -rf / 硬拒绝即使 ask_fn=None 也生效。"""
    tool = _FakeTool("ok")
    reg = _FakeRegistry(tool)
    executor = ToolExecutor(reg)  # 无 guardrail 无 ask_fn
    call = {
        "id": "x",
        "function": {"name": "bash", "arguments": json.dumps({"command": "rm -rf /"})}
    }
    result = executor.execute(call, ctx=TurnContext())
    assert "Permission denied" in result["content"]


def test_executor_fresh_turn_context_resets_guardrail():
    """新建一份 TurnContext 就是从零开始，不需要专门的 begin_turn 方法。"""
    tool = _FakeTool("Error: fail", ok=False)
    ctrl = GuardrailController(interactive=True)
    reg = _FakeRegistry(tool)
    executor = ToolExecutor(reg, guardrail=ctrl)

    call = _make_call()
    ctx = TurnContext()
    for _ in range(4):
        executor.execute(call, ctx=ctx)

    # 新的一轮：新建 TurnContext，第 1 次不应有 warn
    new_ctx = TurnContext()
    result = executor.execute(call, ctx=new_ctx)
    assert "守护栏警告" not in result["content"]
