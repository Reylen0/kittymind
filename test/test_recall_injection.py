"""E1 修复回归测试：记忆召回不再破坏提示缓存（无需 LLM）。

覆盖三块：
  1. 注入方式 —— 召回段是独立 system 消息、插在主 system 之后，messages[0] 不再被改写
  2. 冻结语义 —— 会话内跨轮字节级一致（只算一次）；压缩后（_mark_recall_stale）用下一轮输入重算；
     空召回段也缓存；无会话（session_id=None）不缓存；LRU 淘汰
  3. 适配层 —— Anthropic 合并多条 system 消息（旧行为只取第一条、其余静默丢弃）
"""


from unittest.mock import MagicMock

from kittymind.agent import kitty_agent as ka_module
from kittymind.agent.kitty_agent import KittyAgent
from kittymind.core.llm_adapters import AnthropicAdapter


class StubRecall:
    """替身 MemoryRecall：记录 select_relevant 调用，返回可配置的召回段。"""

    def __init__(self, section: str = "RECALL-SECTION"):
        self.section = section
        self.select_calls: list[str] = []

    def select_relevant(self, user_input: str) -> list[dict]:
        self.select_calls.append(user_input)
        return [{"type": "pref", "name": "n", "body": "b"}] if self.section else []

    def build_recall_section(self, memories: list[dict]) -> str:
        return self.section if memories else ""


def make_agent(section: str = "RECALL-SECTION") -> tuple[KittyAgent, StubRecall]:
    main = MagicMock()
    main.model = "main-model"
    agent = KittyAgent(name="kitty", llm=main, system_prompt="SYS", tools=[])
    stub = StubRecall(section)
    agent._memory_recall = stub  # 直接注入替身，绕过 MemoryStore
    return agent, stub


def base_messages() -> list[dict]:
    return [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
    ]


# ── 注入方式 ─────────────────────────────────────────────────────

async def test_recall_is_separate_system_message():
    agent, _ = make_agent()
    messages = base_messages()
    await agent._inject_memory_recall(messages, "turn-1", "s1")
    assert messages[0]["content"] == "SYS", "主 system prompt 不应再被改写"
    assert messages[1] == {"role": "system", "content": "RECALL-SECTION"}
    assert messages[2]["role"] == "user"


async def test_recall_inserted_first_when_no_system_prompt():
    agent, _ = make_agent()
    messages = [{"role": "user", "content": "hi"}]
    await agent._inject_memory_recall(messages, "hi", "s1")
    assert messages[0] == {"role": "system", "content": "RECALL-SECTION"}


async def test_recall_with_empty_section_leaves_messages_untouched():
    agent, _ = make_agent(section="")
    messages = base_messages()
    await agent._inject_memory_recall(messages, "hi", "s1")
    assert messages == base_messages()


async def test_no_recall_capability_noop():
    agent, _ = make_agent()
    agent._memory_recall = None
    messages = base_messages()
    await agent._inject_memory_recall(messages, "hi", "s1")
    assert messages == base_messages()


# ── 冻结语义 ─────────────────────────────────────────────────────

async def test_recall_frozen_within_session():
    """同一会话跨轮：内容字节级一致，且只计算一次。"""
    agent, stub = make_agent()
    m1, m2 = base_messages(), base_messages()
    await agent._inject_memory_recall(m1, "问题一", "s1")
    await agent._inject_memory_recall(m2, "问题二", "s1")
    assert m1[1]["content"] == m2[1]["content"]
    assert stub.select_calls == ["问题一"], "冻结期内不应重算召回"


async def test_recall_recomputed_after_compression():
    """压缩后历史前缀已重写、缓存必失效：下一轮用当轮输入重算（零额外缓存损失）。"""
    agent, stub = make_agent()
    m1 = base_messages()
    await agent._inject_memory_recall(m1, "第一轮", "s1")

    agent._mark_recall_stale("s1")
    m2 = base_messages()
    await agent._inject_memory_recall(m2, "第二轮", "s1")

    assert stub.select_calls == ["第一轮", "第二轮"]
    assert m2[1]["content"] == "RECALL-SECTION"


async def test_compression_marks_recall_stale_in_run_loop():
    """async_stream_run 里的 did_compress 分支确实会标记重算（源码级回归锚点）。"""
    import inspect
    src = inspect.getsource(ka_module.KittyAgent.async_stream_run)
    assert "_mark_recall_stale" in src
    assert "did_compress" in src


async def test_sessions_cached_independently():
    agent, stub = make_agent()
    await agent._inject_memory_recall(base_messages(), "a", "s1")
    await agent._inject_memory_recall(base_messages(), "b", "s2")
    assert stub.select_calls == ["a", "b"]  # 两个会话各算一次


async def test_no_session_not_cached():
    """session_id=None：无从跨轮冻结，每次现算。"""
    agent, stub = make_agent()
    await agent._inject_memory_recall(base_messages(), "a", None)
    await agent._inject_memory_recall(base_messages(), "b", None)
    assert stub.select_calls == ["a", "b"]


async def test_empty_recall_cached_too():
    """空召回段也缓存：记忆库命中率为 0 时不应每轮都跑选择调用。"""
    agent, stub = make_agent(section="")
    await agent._inject_memory_recall(base_messages(), "a", "s1")
    await agent._inject_memory_recall(base_messages(), "b", "s1")
    assert stub.select_calls == ["a"]


async def test_lru_eviction_recomputes(monkeypatch):
    monkeypatch.setattr(ka_module, "_RECALL_CACHE_MAX", 2)
    agent, stub = make_agent()
    for sid in ("s1", "s2", "s3"):
        await agent._inject_memory_recall(base_messages(), sid, sid)
    assert stub.select_calls == ["s1", "s2", "s3"]
    await agent._inject_memory_recall(base_messages(), "s1", "s1")  # s1 已被淘汰
    assert stub.select_calls[-1] == "s1"


async def test_recall_failure_degrades_silently():
    """召回抛异常：静默降级为空段并缓存，不向上抛、不阻断主流程。"""
    agent, _ = make_agent()

    class BrokenRecall:
        def select_relevant(self, _):
            raise RuntimeError("boom")

        def build_recall_section(self, _):
            return "never"

    agent._memory_recall = BrokenRecall()
    messages = base_messages()
    await agent._inject_memory_recall(messages, "hi", "s1")
    assert messages == base_messages()
    assert agent._recall_cache["s1"] == ""


# ── Anthropic 适配层 ─────────────────────────────────────────────

def test_anthropic_merges_multiple_system_messages():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    params = adapter._make_params(
        [
            {"role": "system", "content": "MAIN-SYS"},
            {"role": "system", "content": "RECALL-SECTION"},
            {"role": "user", "content": "hi"},
        ],
        None, {},
    )
    assert params["system"] == [
        {"type": "text", "text": "MAIN-SYS\n\nRECALL-SECTION", "cache_control": {"type": "ephemeral"}}
    ]
    assert [m["role"] for m in params["messages"]] == ["user"]


def test_anthropic_single_system_unchanged():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    params = adapter._make_params(
        [{"role": "system", "content": "MAIN-SYS"}, {"role": "user", "content": "hi"}],
        None, {},
    )
    assert params["system"] == [
        {"type": "text", "text": "MAIN-SYS", "cache_control": {"type": "ephemeral"}}
    ]


def test_anthropic_no_system_no_param():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    params = adapter._make_params([{"role": "user", "content": "hi"}], None, {})
    assert "system" not in params


# ── Anthropic 提示词缓存断点（Phase 25.3）─────────────────────────

def test_anthropic_cache_breakpoint_on_last_message():
    """最后一条消息末尾打缓存断点，之前的完整历史可被下一轮复用。"""
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    params = adapter._make_params(
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "第二轮"},
        ],
        None, {},
    )
    msgs = params["messages"]
    assert msgs[0]["content"] == "第一轮", "非末尾消息不应被改写"
    assert msgs[-1]["content"] == [
        {"type": "text", "text": "第二轮", "cache_control": {"type": "ephemeral"}}
    ]


def test_anthropic_cache_breakpoint_on_last_tool_result_block():
    """最后一条消息是合并后的 tool_result 列表时，断点打在列表最后一块上。"""
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    params = adapter._make_params(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "结果"},
        ],
        None, {},
    )
    last_content = params["messages"][-1]["content"]
    assert last_content[-1]["cache_control"] == {"type": "ephemeral"}
    assert last_content[-1]["type"] == "tool_result"


def test_anthropic_cache_breakpoint_skipped_on_empty_last_message():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    params = adapter._make_params(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}],
        None, {},
    )
    assert params["messages"][-1]["content"] == ""


def test_anthropic_cache_breakpoint_on_last_tool_definition():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "http://x", 30)
    tools = [
        {"type": "function", "function": {"name": "a", "parameters": {}}},
        {"type": "function", "function": {"name": "b", "parameters": {}}},
    ]
    params = adapter._make_params([{"role": "user", "content": "hi"}], tools, {})
    assert "cache_control" not in params["tools"][0]
    assert params["tools"][1]["cache_control"] == {"type": "ephemeral"}
