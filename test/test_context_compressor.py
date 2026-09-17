"""压缩器头部保护回归测试：_find_head_end 需跳过所有前导 system 消息。

背景：Phase 25 引入了第二条前导 system 消息（记忆召回段，见
kitty_agent._inject_memory_recall），旧实现 _find_head_end 只跳过
messages[0]，导致 protect_first_n 衰减为 0 后（跨轮持久化，见
KittyAgent._new_turn），召回段会被当成"历史"落进 mid 段参与摘要、
被摘要替换掉。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from kittymind.config import cfg
from kittymind.context.compressor import ContextCompressor
from kittymind.context.token_counter import TokenTracker


# ── _find_head_end 单元测试 ────────────────────────────────────────

def test_find_head_end_skips_all_leading_system_messages():
    messages = [
        {"role": "system", "content": "MAIN"},
        {"role": "system", "content": "RECALL"},
        {"role": "user", "content": "h1"},
        {"role": "assistant", "content": "h2"},
    ]
    assert ContextCompressor._find_head_end(messages, protect_first_n=0) == 2
    assert ContextCompressor._find_head_end(messages, protect_first_n=1) == 3


def test_find_head_end_single_system_unchanged():
    messages = [{"role": "system", "content": "MAIN"}, {"role": "user", "content": "h1"}]
    assert ContextCompressor._find_head_end(messages, protect_first_n=0) == 1
    assert ContextCompressor._find_head_end(messages, protect_first_n=2) == 3


def test_find_head_end_no_system():
    messages = [{"role": "user", "content": "h1"}, {"role": "assistant", "content": "h2"}]
    assert ContextCompressor._find_head_end(messages, protect_first_n=1) == 1


# ── 端到端回归：压缩不应吞掉记忆召回段 ─────────────────────────────

def test_recall_message_survives_compression_after_protect_n_decayed(monkeypatch):
    """模拟会话已压缩过一次（protect_first_n 衰减为 0）后再次压缩：
    召回段必须还在 head 里，不能变成摘要文本的一部分。
    """
    monkeypatch.setattr(cfg, "COMPRESS_TAIL_MIN_MSGS", 2)

    llm = MagicMock()
    llm.invoke.return_value = SimpleNamespace(content="摘要内容")
    compressor = ContextCompressor(llm)
    compressor._compressed_once = True  # 跨轮持久化状态：protect_n 已衰减为 0

    tracker = TokenTracker(context_window=60, reserved_output=10)
    messages = [
        {"role": "system", "content": "MAIN-SYS"},
        {"role": "system", "content": "RECALL-SECTION"},
        {"role": "user", "content": "历史问题一"},
        {"role": "assistant", "content": "历史回答一"},
        {"role": "user", "content": "历史问题二"},
        {"role": "assistant", "content": "历史回答二"},
        {"role": "user", "content": "最新问题"},
        {"role": "assistant", "content": "最新回答"},
    ]

    result = compressor.compress(messages, tracker)

    assert result[0]["content"] == "MAIN-SYS"
    assert result[1]["content"] == "RECALL-SECTION", "召回段不应被折进摘要"
    assert result[2]["content"] == "摘要内容" or "摘要内容" in (result[2].get("content") or "")
