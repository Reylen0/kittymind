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
