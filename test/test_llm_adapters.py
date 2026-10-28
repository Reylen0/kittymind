"""AnthropicAdapter 与 OpenAIAdapter 的 base_url 处理。

背景：两家 SDK 对 base_url 的约定不同——OpenAI SDK 端点不带版本号
（`/chat/completions`），base_url 必须自己带 `/v1`；Anthropic SDK 端点
硬编码带版本号（`/v1/messages`），base_url 不能再带 `/v1`，否则拼成
`/v1/v1/messages`。项目里主模型只有一份 LLM_BASE_URL，用户按 OpenAI 惯例
填一次（形如 `.../v1`），换 Claude 模型时不该还要手动去掉这个尾巴。
"""
from kittymind.core.llm_adapters import AnthropicAdapter, OpenAIAdapter, _anthropic_base_url


def test_strips_trailing_v1():
    assert _anthropic_base_url("https://newapi.dzkjm.cn/v1") == "https://newapi.dzkjm.cn"


def test_strips_trailing_v1_with_slash():
    assert _anthropic_base_url("https://newapi.dzkjm.cn/v1/") == "https://newapi.dzkjm.cn"


def test_leaves_url_without_v1_untouched():
    assert _anthropic_base_url("https://api.anthropic.com") == "https://api.anthropic.com"


def test_does_not_touch_v1beta_or_mid_path_v1():
    assert _anthropic_base_url("https://x.com/v1beta") == "https://x.com/v1beta"
    assert _anthropic_base_url("https://x.com/v1/anthropic") == "https://x.com/v1/anthropic"


def test_empty_or_none_passthrough():
    assert _anthropic_base_url(None) is None
    assert _anthropic_base_url("") == ""


def test_anthropic_adapter_client_gets_v1_stripped():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "https://newapi.dzkjm.cn/v1", 30)
    client = adapter._create_client()
    assert client.base_url == "https://newapi.dzkjm.cn"


def test_anthropic_async_adapter_client_gets_v1_stripped():
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "https://newapi.dzkjm.cn/v1", 30)
    client = adapter._create_async_client()
    assert client.base_url == "https://newapi.dzkjm.cn"


def test_openai_adapter_client_keeps_v1_as_is():
    """OpenAI SDK 端点不带版本号，base_url 里的 /v1 必须原样保留。"""
    adapter = OpenAIAdapter("gpt-4o", "key", "https://newapi.dzkjm.cn/v1", 30)
    client = adapter._create_client()
    assert str(client.base_url).rstrip("/") == "https://newapi.dzkjm.cn/v1"


# ── Anthropic 缓存命中下的 prompt token 统计 ──────────────────────
#
# 启用 cache_control 断点后，usage.input_tokens 只统计断点之后"没被缓存"
# 的新增尾巴，命中缓存读出的大头记在 cache_read_input_tokens 里（写缓存记
# 在 cache_creation_input_tokens 里）。三者互斥，只读 input_tokens 会把真实
# 占用严重低估成个位数，导致上下文压缩的触发条件永远达不到。


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0,
                 cache_creation_input_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _UsageNoCacheFields:
    """模拟未启用缓存时的 usage 对象：压根没有 cache_* 属性。"""
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def test_prompt_tokens_from_usage_sums_cache_segments():
    usage = _Usage(input_tokens=3, cache_creation_input_tokens=0, cache_read_input_tokens=50_000)
    assert AnthropicAdapter._prompt_tokens_from_usage(usage) == 50_003


def test_prompt_tokens_from_usage_without_cache_fields_falls_back_to_input_tokens():
    usage = _UsageNoCacheFields(input_tokens=120, output_tokens=40)
    assert AnthropicAdapter._prompt_tokens_from_usage(usage) == 120


class _Msg:
    def __init__(self, usage):
        self.usage = usage


class _MessageStartEvent:
    type = "message_start"

    def __init__(self, usage):
        self.message = _Msg(usage)


class _MessageDeltaEvent:
    type = "message_delta"

    def __init__(self, usage):
        self.usage = usage


class _FakeStream:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration


async def test_async_stream_usage_counts_cache_read_tokens():
    """流式路径：message_start 里的缓存命中大头必须汇入最终 usage 事件。"""
    adapter = AnthropicAdapter("claude-sonnet-4", "key", "https://x/", 30)
    events = [
        _MessageStartEvent(_Usage(input_tokens=3, cache_read_input_tokens=50_000)),
        _MessageDeltaEvent(_Usage(output_tokens=2202)),
    ]

    class _Client:
        class messages:
            @staticmethod
            async def create(**kw):
                return _FakeStream(events)

    adapter._async_client = _Client()

    usage_events = [
        e async for e in adapter.async_stream_with_tools([{"role": "user", "content": "hi"}])
        if e.type == "usage"
    ]

    assert len(usage_events) == 1
    assert usage_events[0].usage == {
        "prompt_tokens": 50_003, "completion_tokens": 2202, "total_tokens": 52_205,
    }


def test_invoke_traces_usage_with_cache_tokens(monkeypatch, tmp_path):
    """非流式 invoke() 此前完全不读 usage；补上后留档里应能看到同样的缓存口径。"""
    from kittymind.core import llm_trace as lt

    adapter = AnthropicAdapter("claude-sonnet-4", "key", "https://x/", 30)

    class _Resp:
        content = []
        usage = _Usage(input_tokens=3, cache_read_input_tokens=50_000, output_tokens=2202)

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    adapter._client = _Client()

    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "all")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_path / "t.jsonl")
    lt._instance[0] = None
    lt._WARNED_LEVELS.clear()

    adapter.invoke([{"role": "user", "content": "hi"}])

    import json
    rows = [json.loads(l) for l in (tmp_path / "t.jsonl").open(encoding="utf-8") if l.strip()]
    assert len(rows) == 1
    assert rows[0]["response"]["usage"] == {"prompt_tokens": 50_003, "completion_tokens": 2202}

    lt._instance[0] = None
    lt._WARNED_LEVELS.clear()
