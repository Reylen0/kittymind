"""工具调用守护栏控制器（Tool Guardrails）。

纯逻辑、无副作用：记录每轮工具调用观测，返回决策（allow/warn/block）。
由执行器决定决策如何落地：block → 跳过执行 + 合成结果；warn → 追加指导文字。

检测三类异常模式：
  1. 精确失败循环：同工具同参数连续报错（warn N 次后 block）
  2. 同工具连环失败：同工具不同参数连续报错（warn M 次后 block）
  3. 无进展空转：幂等只读工具反复返回相同结果（warn N 次后 block）

额外：连续相同调用流（同 sig+同 result）达阈值则无条件 block，不受 warn/hard_stop 两态区分影响。

失败判定（failed）的唯一来源是 executor 从工具显式返回的 ToolResult.ok，
本模块只消费这个判定，不解析工具输出文本内容。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Mapping

from ..config import cfg


# ── 工具分类常量（不入 settings.json） ──────────────────────────
# 两集合必须互斥：幂等（只读、重复调用无害）与变更类是两种互斥的归类。
# test_guardrails.py 有 disjoint 断言——若日后出现跨集合的工具，说明分类错了，
# 而不是给下面的判断加回 `and name not in MUTATING_TOOLS`（那是恒真冗余）。

IDEMPOTENT_TOOLS: frozenset[str] = frozenset({
    "file_read", "glob", "grep", "ls", "get_current_time",
})

MUTATING_TOOLS: frozenset[str] = frozenset({
    "bash", "file_write", "file_edit", "git", "write_memory", "task", "clipboard",
    # screenshot 会写盘（_unique_path 存在 TOCTOU），verify 等价于执行任意命令——
    # 两者都不可视为只读（此前漏分类，已补）。
    "screenshot", "verify",
})


# ── 决策文案 ──────────────────────────────────────────────────

_MESSAGES: dict[str, str] = {
    "exact_failure_warn": (
        "工具 {tool} 已用相同参数失败 {count} 次，疑似循环；"
        "请检查错误并改变策略，勿原样重试。"
    ),
    "exact_failure_block": (
        "已阻断 {tool}：相同参数已失败 {count} 次。"
        "请改变策略或向用户说明阻塞原因，勿再原样重试。"
    ),
    "same_tool_failure_warn": (
        "工具 {tool} 本轮已失败 {count} 次；"
        "请先诊断错误（尝试其他参数或不同工具），再继续。"
    ),
    "same_tool_failure_block": (
        "已阻断 {tool}：本轮已失败 {count} 次。"
        "请选择不同工具或向用户报告阻塞，勿继续重试同一工具。"
    ),
    "no_progress_warn": (
        "工具 {tool} 已连续 {count} 次返回相同结果；"
        "请换用不同查询/路径，不要重复相同调用。"
    ),
    "no_progress_block": (
        "已阻断 {tool}：已连续 {count} 次返回相同结果。"
        "请使用已有结果或改变查询，勿重复无进展的调用。"
    ),
    "identical_streak_block": (
        "已阻断 {tool}：连续 {count} 次以完全相同的参数和结果调用。"
        "请使用已有结果、改变参数或换用其他工具。"
    ),
}


# ── 数据类 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolCallSignature:
    """工具名 + 规范化参数哈希，作为失败/无进展计数的键。"""
    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any]) -> ToolCallSignature:
        return cls(tool_name=tool_name, args_hash=_sha256(_canonical_json(args or {})))


@dataclass(frozen=True)
class GuardrailDecision:
    """守护栏决策。action: allow | warn | block。"""
    action: str = "allow"
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def is_block(self) -> bool:
        return self.action == "block"


@dataclass
class GuardrailTurnState:
    """单轮观测状态（可变）。调用方每轮新建一份，随调用显式传递，轮结束即弹。"""
    exact_failure_counts: dict[ToolCallSignature, int] = field(default_factory=dict)
    same_tool_failure_counts: dict[str, int] = field(default_factory=dict)
    no_progress: dict[ToolCallSignature, tuple[str, int]] = field(default_factory=dict)
    progress_since_failure: dict[ToolCallSignature, bool] = field(default_factory=dict)
    streak_sig: ToolCallSignature | None = None
    streak_result_hash: str = ""
    streak_count: int = 0


# ── 控制器 ───────────────────────────────────────────────────

class GuardrailController:
    """每轮工具调用守护栏控制器。

    无状态、无副作用：只按传入的 `GuardrailTurnState` 记录观测、返回决策。
    每轮状态由调用方（`KittyAgent`）持有并显式传入——是个跟着调用栈走的普通值，
    不需要任何隔离机制：两个会话各自的 `GuardrailTurnState` 是两个不同的对象，
    天然不会互相影响，无论是否并发执行。
    """

    def __init__(self, interactive: bool = True):
        # 交互态默认只警告；非交互态或全局 HARD_STOP 配置时启用 block
        self.hard_stop_enabled: bool = cfg.TOOL_GUARDRAIL_HARD_STOP or (not interactive)
        self.warn_enabled: bool = cfg.TOOL_GUARDRAIL_WARN_ENABLED

    # ── 执行前决策 ──────────────────────────────────────────

    def before_call(
        self, state: GuardrailTurnState, name: str, args: Mapping[str, Any]
    ) -> GuardrailDecision:
        """执行前判断是否阻断。"""
        sig = ToolCallSignature.from_call(name, args)

        # 连续相同调用流 block（两态都生效）
        if (state.streak_sig == sig
                and state.streak_count >= cfg.GUARD_IDENTICAL_STREAK_BLOCK):
            return self._make("block", "identical_streak_block", name, state.streak_count)

        if not self.hard_stop_enabled:
            return GuardrailDecision(tool_name=name)

        # 精确失败 block（hard_stop 态）
        exact = 0 if state.progress_since_failure.get(sig) else state.exact_failure_counts.get(sig, 0)
        if exact >= cfg.GUARD_EXACT_FAIL_BLOCK:
            return self._make("block", "exact_failure_block", name, exact)

        # 同工具连环失败 block（hard_stop 态，不论参数是否相同）
        same = state.same_tool_failure_counts.get(name, 0)
        if same >= cfg.GUARD_SAME_TOOL_FAIL_BLOCK:
            return self._make("block", "same_tool_failure_block", name, same)

        # 无进展 block（hard_stop 态，仅幂等工具）
        if name in IDEMPOTENT_TOOLS:
            rec = state.no_progress.get(sig)
            if rec is not None and rec[1] >= cfg.GUARD_NO_PROGRESS_BLOCK:
                return self._make("block", "no_progress_block", name, rec[1])

        return GuardrailDecision(tool_name=name)

    # ── 执行后观测 ──────────────────────────────────────────

    def after_call(
        self, state: GuardrailTurnState, name: str, args: Mapping[str, Any], result: str, failed: bool
    ) -> GuardrailDecision:
        """执行后更新计数，返回 warn 决策（或 allow）。"""
        sig = ToolCallSignature.from_call(name, args)
        result_hash = _sha256(result or "")

        # 更新连续相同调用流
        if state.streak_sig == sig and result_hash == state.streak_result_hash:
            state.streak_count += 1
        else:
            state.streak_sig = sig
            state.streak_result_hash = result_hash
            state.streak_count = 1

        if failed:
            return self._handle_failure(state, sig, name)
        return self._handle_success(state, sig, name, result_hash)

    # ── 内部 ────────────────────────────────────────────────

    def _handle_failure(self, state: GuardrailTurnState, sig: ToolCallSignature, name: str) -> GuardrailDecision:
        # 若此前有进展标记，此次失败视为新实验，清 exact 计数
        if state.progress_since_failure.pop(sig, False):
            state.exact_failure_counts.pop(sig, None)

        exact = state.exact_failure_counts[sig] = state.exact_failure_counts.get(sig, 0) + 1
        same = state.same_tool_failure_counts[name] = state.same_tool_failure_counts.get(name, 0) + 1
        state.no_progress.pop(sig, None)

        if self.warn_enabled:
            if exact >= cfg.GUARD_EXACT_FAIL_WARN:
                return self._make("warn", "exact_failure_warn", name, exact)
            if same >= cfg.GUARD_SAME_TOOL_FAIL_WARN:
                return self._make("warn", "same_tool_failure_warn", name, same)

        return GuardrailDecision(tool_name=name, count=exact)

    def _handle_success(
        self, state: GuardrailTurnState, sig: ToolCallSignature, name: str, result_hash: str
    ) -> GuardrailDecision:
        state.exact_failure_counts.pop(sig, None)

        # 变更类工具成功 → 所有已失败 sig 标记进展，清 same_tool 计数
        if name in MUTATING_TOOLS:
            state.progress_since_failure.update(dict.fromkeys(state.exact_failure_counts, True))
            state.same_tool_failure_counts.pop(name, None)

        # 幂等只读工具：无进展检测
        if name in IDEMPOTENT_TOOLS:
            prev = state.no_progress.get(sig)
            repeat = (prev[1] + 1) if (prev is not None and prev[0] == result_hash) else 1
            state.no_progress[sig] = (result_hash, repeat)
            if self.warn_enabled and repeat >= cfg.GUARD_NO_PROGRESS_WARN:
                return self._make("warn", "no_progress_warn", name, repeat)
        else:
            state.no_progress.pop(sig, None)

        return GuardrailDecision(tool_name=name)

    def _make(self, action: str, code: str, name: str, count: int) -> GuardrailDecision:
        msg = _MESSAGES[code].format(tool=name, count=count)
        return GuardrailDecision(action=action, code=code, message=msg, tool_name=name, count=count)


# ── 工具函数 ─────────────────────────────────────────────────

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
