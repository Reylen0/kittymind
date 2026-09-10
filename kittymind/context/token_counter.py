"""Token 计量：字符估算 + 真实 usage 校准。"""

import json
import re

# CJK Unicode 范围：CJK 统一汉字 + 扩展 A/B + 兼容
_CJK_RE = re.compile(r'[一-鿿㐀-䶿\U00020000-\U0002a6df＀-￯]')

# CJK 字符大约 1.5~2 字符/token，取 1.7 为折中
_CJK_CHARS_PER_TOKEN = 1.7
_ASCII_CHARS_PER_TOKEN = 4.0


def _str_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    ascii_count = len(text) - cjk_count
    return int(cjk_count / _CJK_CHARS_PER_TOKEN + ascii_count / _ASCII_CHARS_PER_TOKEN) + 1


def estimate_tokens(messages: list[dict]) -> int:
    """估算消息列表的 token 数（无 tiktoken 依赖）。"""
    total = 0
    for msg in messages:
        # role overhead
        total += 4
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += _str_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += _str_tokens(part.get("text", ""))

        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function", {})
            total += _str_tokens(fn.get("name", ""))
            total += _str_tokens(fn.get("arguments", ""))
    return total


class TokenTracker:
    """追踪上下文 token 占用，支持真实 usage 校准。"""

    def __init__(self, context_window: int, reserved_output: int):
        self._effective = max(1, context_window - reserved_output)
        self._last_prompt_tokens: int | None = None

    def update_from_usage(self, usage: dict) -> None:
        """收到真实 API usage 时校准。"""
        pt = usage.get("prompt_tokens")
        if pt is not None:
            self._last_prompt_tokens = int(pt)

    def ratio(self, messages: list[dict]) -> float:
        """当前 messages 占有效窗口的比例（0.0~1.0+）。"""
        if self._last_prompt_tokens is not None:
            used = self._last_prompt_tokens
        else:
            used = estimate_tokens(messages)
        return used / self._effective

    def used_tokens(self, messages: list[dict]) -> int:
        if self._last_prompt_tokens is not None:
            return self._last_prompt_tokens
        return estimate_tokens(messages)

    @property
    def effective_window(self) -> int:
        return self._effective
