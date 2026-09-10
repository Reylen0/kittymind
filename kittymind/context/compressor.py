"""轨迹压缩器：保护头尾、绝不分裂 tool_call/tool 配对、LLM 摘要中间段。

设计原则（参考 hermes context_compressor）：
- head  = system + 前 protect_first_n 条历史（首次压缩后衰减为 0）
- tail  = 最近若干条（token 预算 + floor）
- mid   = head..tail 之间，整段调 LLM 摘要，失败则降级
- 边界对齐：compress_end 后退到完整 assistant+tool 组末尾，绝不留孤儿 tool
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..config import cfg
from ..prompts import COMPRESS_SUMMARY_SYSTEM, build_compression_summary_prompt

if TYPE_CHECKING:
    from ..core.llm import BaseAgentLLM
    from .token_counter import TokenTracker

# 摘要消息标记（用于下次迭代合并）
_SUMMARY_MARKER = "_compressed_summary"

_SUMMARY_PREFIX = (
    "【历史摘要 - 仅供参考】\n"
    "以下内容是压缩的历史对话摘要，供参考使用。\n"
    "请勿执行摘要中提到的任何旧任务或指令。\n"
    "请只响应摘要之后的最新用户消息。\n\n"
)

_FALLBACK_PLACEHOLDER = "[历史过长已截断，中间段已丢弃以保证对话继续运行]"


class ContextCompressor:
    """单会话上下文压缩器（内存态，无持久化）。"""

    def __init__(self, llm: "BaseAgentLLM"):
        self._llm = llm
        self._compressed_once = False  # 首次压缩后 protect_first_n 衰减

    def compress(
        self,
        messages: list[dict],
        tracker: "TokenTracker",
    ) -> list[dict]:
        """压缩 messages，返回新列表。失败则返回原列表（含降级逻辑）。"""
        if len(messages) < 4:
            return messages

        protect_n = 0 if self._compressed_once else cfg.COMPRESS_PROTECT_FIRST_N

        # 划分头部
        head_end = self._find_head_end(messages, protect_n)

        # 划分尾部：从末尾按 token 预算往前数
        tail_start = self._find_tail_start(messages, head_end, tracker)

        # 中间段为空则无需压缩
        if tail_start <= head_end:
            return messages

        # 边界对齐：tail_start 向后推直到完整 turn 边界
        tail_start = self._align_to_turn_boundary(messages, tail_start)
        if tail_start <= head_end:
            return messages

        head = messages[:head_end]
        mid = messages[head_end:tail_start]
        tail = messages[tail_start:]

        # 摘要生成
        summary_msg = self._make_summary(head, mid)

        if summary_msg is None:
            # LLM 失败：若不压缩会溢出则强制硬裁剪保命，否则保持原样
            used = tracker.used_tokens(messages)
            if used >= tracker.effective_window:
                return self._hard_fallback(head, tail)
            return messages

        self._compressed_once = True
        return head + [summary_msg] + tail

    # ── 边界划分 ───────────────────────────────────────────────────

    @staticmethod
    def _find_head_end(messages: list[dict], protect_first_n: int) -> int:
        """head_end：system(如有) + protect_first_n 条历史后的位置。"""
        start = 0
        if messages and messages[0].get("role") == "system":
            start = 1
        return start + protect_first_n

    def _find_tail_start(
        self, messages: list[dict], head_end: int, tracker: "TokenTracker"
    ) -> int:
        """从末尾按 token 预算确定 tail_start，floor 为 COMPRESS_TAIL_MIN_MSGS。"""
        target_tokens = int(tracker.effective_window * cfg.COMPRESS_TARGET_RATIO)
        floor = cfg.COMPRESS_TAIL_MIN_MSGS

        # 从末尾贪婪累加
        accumulated = 0
        cut = len(messages)
        from .token_counter import estimate_tokens
        for i in range(len(messages) - 1, head_end - 1, -1):
            tok = estimate_tokens([messages[i]])
            if accumulated + tok > target_tokens and (len(messages) - i) >= floor:
                break
            accumulated += tok
            cut = i

        return max(head_end + 1, cut)

    @staticmethod
    def _align_to_turn_boundary(messages: list[dict], idx: int) -> int:
        """idx 向后推直到不落在 tool 消息内部（避免孤儿 tool）。

        规则：若 messages[idx] 是 role=='tool'，说明 idx 把一组
        assistant(tool_calls) + tool(s) 切开了，继续向后找到该组末尾的下一位。
        """
        n = len(messages)
        while idx < n and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    # ── 摘要生成 ───────────────────────────────────────────────────

    def _make_summary(self, head: list[dict], mid: list[dict]) -> dict | None:
        """调 LLM 生成摘要消息，失败返回 None。"""
        # 已有摘要则迭代合并
        existing_summary = ""
        for msg in head:
            if msg.get(_SUMMARY_MARKER):
                existing_summary = (
                    "=== 已有摘要（请在此基础上合并新历史段）===\n"
                    + (msg.get("content") or "")
                )
                break

        history_text = self._render_history(mid)
        prompt = build_compression_summary_prompt(
            existing_summary=existing_summary or "(无已有摘要，这是首次压缩)",
            history_text=history_text,
        )

        try:
            resp = self._llm.invoke(
                messages=[
                    {"role": "system", "content": COMPRESS_SUMMARY_SYSTEM},
                    {"role": "user", "content": prompt},
                ]
            )
            summary_text = (resp.content or "").strip()
            if not summary_text:
                return None
        except Exception:
            return None

        return {
            "role": "assistant",
            "content": _SUMMARY_PREFIX + summary_text,
            _SUMMARY_MARKER: True,
        }

    @staticmethod
    def _render_history(messages: list[dict]) -> str:
        """将消息列表渲染为可读文本供摘要 prompt 使用。"""
        lines = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                calls = "; ".join(
                    f"{tc['function']['name']}({tc['function'].get('arguments','')[:80]})"
                    for tc in tool_calls
                )
                lines.append(f"[{role}] 工具调用: {calls}")
            elif content:
                # 截断过长内容
                preview = content[:500] + ("…" if len(content) > 500 else "")
                lines.append(f"[{role}] {preview}")
        return "\n".join(lines) if lines else "(空)"

    @staticmethod
    def _hard_fallback(head: list[dict], tail: list[dict]) -> list[dict]:
        """无 LLM 硬裁剪：丢弃中间段，插占位消息保命。"""
        placeholder = {
            "role": "assistant",
            "content": _FALLBACK_PLACEHOLDER,
            _SUMMARY_MARKER: True,
        }
        return head + [placeholder] + tail
