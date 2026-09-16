"""工具调用守护栏控制器（Tool Guardrails）。

纯逻辑、无副作用：记录每轮工具调用观测，返回决策（allow/warn/block）。
由执行器决定决策如何落地：block → 跳过执行 + 合成结果；warn → 追加指导文字。

检测三类异常模式：
  1. 精确失败循环：同工具同参数连续报错（warn N 次后 block）
  2. 同工具连环失败：同工具不同参数连续报错（warn M 次后 block）
  3. 无进展空转：幂等只读工具反复返回相同结果（warn N 次后 block）

额外：连续相同调用流（同 sig+同 result）达阈值则无条件 block，取代原 gate-0。

失败判定（failed）由 executor 从工具显式返回的 ToolResult.ok 得出，
本模块不再自行猜测文本内容。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..config import cfg


# ── 工具分类常量（不入 settings.json） ──────────────────────────

IDEMPOTENT_TOOLS: frozenset[str] = frozenset({
    "file_read", "glob", "grep", "ls", "get_current_time",
})

MUTATING_TOOLS: frozenset[str] = frozenset({
    "bash", "file_write", "file_edit", "git", "write_memory", "task", "clipboard",
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
    def from_call(cls, tool_name: str, args: Mapping[str, Any]) -> "ToolCallSignature":
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


# ── 控制器 ───────────────────────────────────────────────────

class GuardrailController:
    """每轮工具调用守护栏控制器。

    无副作用：只记录观测、返回决策。调用方在每轮开始前调 reset_turn()。
    """

    def __init__(self, interactive: bool = True):
        # 交互态默认只警告；非交互态或全局 HARD_STOP 配置时启用 block
        self.hard_stop_enabled: bool = cfg.TOOL_GUARDRAIL_HARD_STOP or (not interactive)
        self.warn_enabled: bool = cfg.TOOL_GUARDRAIL_WARN_ENABLED
        self.reset_turn()

    def reset_turn(self) -> None:
        """每轮开始前调用，清空全部每轮状态。"""
        # 精确失败：同 sig 连续失败计数（有进展后清零）
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        # 同工具失败：同工具名失败计数（不论参数）
        self._same_tool_failure_counts: dict[str, int] = {}
        # 无进展：幂等工具同 result 计数 {sig: (result_hash, count)}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        # 进展标记：变更类工具成功后，已失败 sig 标记为「有进展」
        self._progress_since_failure: dict[ToolCallSignature, bool] = {}
        # 连续相同调用流（tool-agnostic）：连续 (sig, result_hash) 相同时累加
        self._streak_sig: ToolCallSignature | None = None
        self._streak_result_hash: str = ""
        self._streak_count: int = 0

    # ── 执行前决策 ──────────────────────────────────────────

    def before_call(self, name: str, args: Mapping[str, Any]) -> GuardrailDecision:
        """执行前判断是否阻断。"""
        sig = ToolCallSignature.from_call(name, args)

        # 连续相同调用流 block（两态都生效）
        if (self._streak_sig == sig
                and self._streak_count >= cfg.GUARD_IDENTICAL_STREAK_BLOCK):
            return self._make("block", "identical_streak_block", name, self._streak_count)

        if not self.hard_stop_enabled:
            return GuardrailDecision(tool_name=name)

        # 精确失败 block（hard_stop 态）
        exact = 0 if self._progress_since_failure.get(sig) else self._exact_failure_counts.get(sig, 0)
        if exact >= cfg.GUARD_EXACT_FAIL_BLOCK:
            return self._make("block", "exact_failure_block", name, exact)

        # 同工具连环失败 block（hard_stop 态，不论参数是否相同）
        same = self._same_tool_failure_counts.get(name, 0)
        if same >= cfg.GUARD_SAME_TOOL_FAIL_BLOCK:
            return self._make("block", "same_tool_failure_block", name, same)

        # 无进展 block（hard_stop 态，仅幂等工具）
        if name in IDEMPOTENT_TOOLS and name not in MUTATING_TOOLS:
            rec = self._no_progress.get(sig)
            if rec is not None and rec[1] >= cfg.GUARD_NO_PROGRESS_BLOCK:
                return self._make("block", "no_progress_block", name, rec[1])

        return GuardrailDecision(tool_name=name)

    # ── 执行后观测 ──────────────────────────────────────────

    def after_call(
        self, name: str, args: Mapping[str, Any], result: str, failed: bool
    ) -> GuardrailDecision:
        """执行后更新计数，返回 warn 决策（或 allow）。"""
        sig = ToolCallSignature.from_call(name, args)
        result_hash = _sha256(result or "")

        # 更新连续相同调用流
        if self._streak_sig == sig and result_hash == self._streak_result_hash:
            self._streak_count += 1
        else:
            self._streak_sig = sig
            self._streak_result_hash = result_hash
            self._streak_count = 1

        if failed:
            return self._handle_failure(sig, name)
        else:
            return self._handle_success(sig, name, result, result_hash)

    # ── 内部 ────────────────────────────────────────────────

    def _handle_failure(self, sig: ToolCallSignature, name: str) -> GuardrailDecision:
        # 若此前有进展标记，此次失败视为新实验，清 exact 计数
        if self._progress_since_failure.pop(sig, False):
            self._exact_failure_counts.pop(sig, None)

        exact = self._exact_failure_counts[sig] = self._exact_failure_counts.get(sig, 0) + 1
        same = self._same_tool_failure_counts[name] = self._same_tool_failure_counts.get(name, 0) + 1
        self._no_progress.pop(sig, None)

        if self.warn_enabled:
            if exact >= cfg.GUARD_EXACT_FAIL_WARN:
                return self._make("warn", "exact_failure_warn", name, exact)
            if same >= cfg.GUARD_SAME_TOOL_FAIL_WARN:
                return self._make("warn", "same_tool_failure_warn", name, same)

        return GuardrailDecision(tool_name=name, count=exact)

    def _handle_success(
        self, sig: ToolCallSignature, name: str, result: str, result_hash: str
    ) -> GuardrailDecision:
        self._exact_failure_counts.pop(sig, None)

        # 变更类工具成功 → 所有已失败 sig 标记进展，清 same_tool 计数
        if name in MUTATING_TOOLS:
            self._progress_since_failure.update(dict.fromkeys(self._exact_failure_counts, True))
            self._same_tool_failure_counts.pop(name, None)

        # 幂等只读工具：无进展检测
        if name in IDEMPOTENT_TOOLS and name not in MUTATING_TOOLS:
            prev = self._no_progress.get(sig)
            repeat = (prev[1] + 1) if (prev is not None and prev[0] == result_hash) else 1
            self._no_progress[sig] = (result_hash, repeat)
            if self.warn_enabled and repeat >= cfg.GUARD_NO_PROGRESS_WARN:
                return self._make("warn", "no_progress_warn", name, repeat)
        else:
            self._no_progress.pop(sig, None)

        return GuardrailDecision(tool_name=name)

    def _make(self, action: str, code: str, name: str, count: int) -> GuardrailDecision:
        msg = _MESSAGES[code].format(tool=name, count=count)
        return GuardrailDecision(action=action, code=code, message=msg, tool_name=name, count=count)


# ── 工具函数 ─────────────────────────────────────────────────

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
