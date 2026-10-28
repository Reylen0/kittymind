"""压缩器头部保护回归测试：_find_head_end 需跳过所有前导 system 消息。

背景：Phase 25 引入了第二条前导 system 消息（记忆召回段，见
kitty_agent._inject_memory_recall），旧实现 _find_head_end 只跳过
messages[0]，导致 protect_first_n 衰减为 0 后（跨轮持久化，见
KittyAgent.run_conversation 里的本轮运行态组装），召回段会被当成"历史"落进
mid 段参与摘要、被摘要替换掉。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.config import cfg
from kittymind.context.compressor import ContextCompressor, _SUMMARY_MARKER
from kittymind.context.token_counter import TokenTracker, estimate_tokens


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


# ── 第二次压缩必须识别出落在 mid 里的已有摘要 ──────────────────────

def test_second_compression_finds_existing_summary_in_mid(monkeypatch):
    """首次压缩后 protect_first_n 衰减为 0，上一轮的摘要消息会落进新一轮
    的 mid（而非 head）。_make_summary 必须也在 mid 里找已有摘要，否则
    它会被当成普通历史行重新喂给 LLM，既丢失"已有摘要"标记，又在 prompt
    里和 existing_summary 字段重复出现。
    """
    monkeypatch.setattr(cfg, "COMPRESS_TAIL_MIN_MSGS", 2)

    llm = MagicMock()
    llm.invoke.return_value = SimpleNamespace(content="第一次摘要")
    compressor = ContextCompressor(llm)

    tracker = TokenTracker(context_window=80, reserved_output=10)
    messages = [
        {"role": "system", "content": "MAIN-SYS"},
        {"role": "user", "content": "历史问题一"},
        {"role": "assistant", "content": "历史回答一"},
        {"role": "user", "content": "历史问题二"},
        {"role": "assistant", "content": "历史回答二"},
        {"role": "user", "content": "历史问题三"},
        {"role": "assistant", "content": "历史回答三"},
        {"role": "user", "content": "最新问题"},
        {"role": "assistant", "content": "最新回答"},
    ]

    result1 = compressor.compress(messages, tracker)
    assert any(m.get(_SUMMARY_MARKER) for m in result1)

    result1 += [
        {"role": "user", "content": "新问题A"},
        {"role": "assistant", "content": "新回答A"},
        {"role": "user", "content": "新问题B"},
        {"role": "assistant", "content": "新回答B"},
    ]

    llm.invoke.return_value = SimpleNamespace(content="第二次摘要")
    compressor.compress(result1, tracker)

    assert llm.invoke.call_count == 2
    _, kwargs = llm.invoke.call_args
    prompt = kwargs["messages"][1]["content"]

    assert "无已有摘要" not in prompt, "第二次压缩应识别出已有摘要，而不是误判为首次压缩"
    assert "第一次摘要" in prompt
    assert prompt.count("第一次摘要") == 1, "已有摘要不应同时出现在 existing_summary 字段和历史渲染里"


# ── update_from_usage：真实占用要把 completion 也算进去 ────────────

def test_update_from_usage_sums_prompt_and_completion():
    """只记 prompt_tokens 会漏掉这次新生成、马上要追加进历史的回复——
    下一次调用看到的真实体量是 prompt + completion 之和。"""
    tracker = TokenTracker(context_window=1000, reserved_output=100)
    tracker.update_from_usage({"prompt_tokens": 500, "completion_tokens": 200})
    assert tracker._last_prompt_tokens == 700


def test_update_from_usage_without_completion_field_falls_back_to_prompt_only():
    """旧网关/非流式路径可能压根不带 completion_tokens 字段，不应报错。"""
    tracker = TokenTracker(context_window=1000, reserved_output=100)
    tracker.update_from_usage({"prompt_tokens": 500})
    assert tracker._last_prompt_tokens == 500


# ── 溢出闸门必须量「当前列表」，不能用上一轮的实测值 ─────────────────

def _failing_llm():
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("aux LLM unreachable")
    return llm


def test_current_tokens_ignores_last_prompt_tokens():
    """current_tokens 与 used_tokens 的分工：前者只回答"这个列表多大"，
    后者有实测值时返回上一轮的 usage。闸门必须用前者。"""
    tracker = TokenTracker(context_window=1000, reserved_output=100)
    messages = [{"role": "user", "content": "短"}]
    assert tracker.current_tokens(messages) == estimate_tokens(messages)

    tracker._last_prompt_tokens = 999
    assert tracker.used_tokens(messages) == 999
    assert tracker.current_tokens(messages) == estimate_tokens(messages), \
        "current_tokens 不得被 _last_prompt_tokens 影响"


def test_hard_fallback_not_triggered_by_stale_high_measurement(monkeypatch):
    """上一轮实测占比已超窗口，但当前列表经 prune 后其实很小 → 不该硬裁剪。

    这是用户报的场景：比较用的是压缩前的值。旧实现会 `_hard_fallback`，
    把 mid 段整体丢成一行占位符——不可逆的数据丢失，换一个并不存在的溢出。
    """
    monkeypatch.setattr(cfg, "COMPRESS_TAIL_MIN_MSGS", 2)
    monkeypatch.setattr(cfg, "COMPRESS_PROTECT_FIRST_N", 1)

    tracker = TokenTracker(context_window=100, reserved_output=10)  # effective = 90
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "历史一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "历史二"},
        {"role": "assistant", "content": "回答二"},
        {"role": "user", "content": "最新"},
        {"role": "assistant", "content": "最新答"},
    ]
    tracker._last_prompt_tokens = 95  # 陈旧：ratio 1.056 > 1.0

    assert estimate_tokens(messages) < tracker.effective_window, "前提：当前列表其实没超窗口"

    out = ContextCompressor(_failing_llm()).compress(messages, tracker)

    assert not any(
        (m.get("content") or "").startswith("[历史过长") for m in out
    ), "当前体量没超窗口，不该触发硬裁剪"
    assert out is messages, "摘要失败且未溢出时应原样返回"
    assert any(m.get("content") == "历史二" for m in out), "mid 段内容必须存活"


def test_no_protection_when_stale_low(monkeypatch):
    """反向对照：上一轮实测占比很低，但当前列表（含超长 tool 输出）其实
    已远超窗口 → 必须触发硬裁剪保命。只看陈旧值会漏掉这次保护。"""
    monkeypatch.setattr(cfg, "COMPRESS_TAIL_MIN_MSGS", 2)
    monkeypatch.setattr(cfg, "COMPRESS_PROTECT_FIRST_N", 1)

    tracker = TokenTracker(context_window=1000, reserved_output=100)  # effective = 900
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "历史一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "历史二"},
        {"role": "assistant", "content": "回答二"},
        {"role": "tool", "content": "x" * 20000},
        {"role": "user", "content": "最新"},
        {"role": "assistant", "content": "最新答"},
    ]
    tracker._last_prompt_tokens = 100  # 陈旧：ratio 0.111

    assert tracker.used_tokens(messages) < tracker.effective_window, "前提：陈旧值看不到溢出"
    assert tracker.current_tokens(messages) >= tracker.effective_window, "前提：当前列表确实溢出"

    out = ContextCompressor(_failing_llm()).compress(messages, tracker)

    assert any(
        (m.get("content") or "").startswith("[历史过长") for m in out
    ), "当前列表已溢出，必须触发硬裁剪保命"


# ── 反抖动闸门与触发闸门必须同分母 ────────────────────────────────

def test_anti_thrash_uses_effective_window_denominator(monkeypatch):
    """反抖动必须与触发闸门同分母（tracker.effective_window）。

    两个分母的阈值：新 int(900*0.7)=630，旧 int(1000*0.7)=700。
    pt 落在 [630, 700) 时，占有效窗口的比例其实已 ≥0.70（压缩后仍没降下来，
    确实无效），但旧分母用 700 当门槛会把它漏判成"有效压缩"而不计失败，
    连抖两次也不会进冷却。本用例就取该区间内的值。
    """
    monkeypatch.setattr(cfg, "LLM_CONTEXT_WINDOW", 1000)
    monkeypatch.setattr(cfg, "LLM_RESERVED_OUTPUT_TOKENS", 100)
    monkeypatch.setattr(cfg, "COMPRESS_THRESHOLD_RATIO", 0.70)
    monkeypatch.setattr(cfg, "COMPRESS_COOLDOWN_SECONDS", 300)

    agent = KittyAgent(name="t", llm=MagicMock(), aux_llm=None)
    tracker = TokenTracker(1000, 100)  # effective = 900

    pt = 665
    assert int(900 * 0.70) <= pt < int(1000 * 0.70), \
        "前提：该值在新分母下超阈值、在旧分母下不超——正是两者判定分歧的区间"

    # 一次：计量
    _, count = agent._check_anti_thrash({"prompt_tokens": pt}, tracker, 0.0, 0)
    assert count == 1, "占用仍超触发阈值，应计为一次压缩无效"

    # 第二次：达到阈值后进冷却（旧分母此刻仍认为有效，不会冷却）
    cooldown, count = agent._check_anti_thrash({"prompt_tokens": pt}, tracker, 0.0, count)
    assert count >= 2
    assert cooldown > 0, "连续两次未降占用应进入冷却"

    # 对照：真正降下来了（低于 effective_window*0.7）不该计为无效
    _, count = agent._check_anti_thrash(
        {"prompt_tokens": int(900 * 0.70) - 1}, tracker, 0.0, 0
    )
    assert count == 0, "占用已降到触发阈值之下，应重置计数"


def test_anti_thrash_counts_completion_tokens_too(monkeypatch):
    """反抖动的分子也要和 ratio() 一致：prompt+completion，不是单纯 prompt。

    tracker.ratio() 存的是 update_from_usage 里 prompt+completion 的和，
    _check_anti_thrash 若只拿 prompt_tokens 比较，分子口径就和触发闸门
    错位——prompt 本身不过阈值、但加上 completion 后其实已经过线的情况，
    会被漏判成"压缩有效"。
    """
    monkeypatch.setattr(cfg, "LLM_CONTEXT_WINDOW", 1000)
    monkeypatch.setattr(cfg, "LLM_RESERVED_OUTPUT_TOKENS", 100)
    monkeypatch.setattr(cfg, "COMPRESS_THRESHOLD_RATIO", 0.70)
    monkeypatch.setattr(cfg, "COMPRESS_COOLDOWN_SECONDS", 300)

    agent = KittyAgent(name="t", llm=MagicMock(), aux_llm=None)
    tracker = TokenTracker(1000, 100)  # effective = 900, threshold = 630

    # prompt 单独不过线，但加上 completion 后过线
    usage = {"prompt_tokens": 600, "completion_tokens": 50}
    assert usage["prompt_tokens"] < int(900 * 0.70) <= usage["prompt_tokens"] + usage["completion_tokens"]

    _, count = agent._check_anti_thrash(usage, tracker, 0.0, 0)
    assert count == 1, "只看 prompt_tokens 会漏判——必须把 completion_tokens 也算进去"



