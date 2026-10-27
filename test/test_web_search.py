"""联网搜索能力测试（无真实网络请求）。

覆盖两层：
  1. get_web_search_backend() 的解析与回退（env 优先、未配置返回 None、
     缺 key 返回 None、构造失败不缓存、构造成功缓存）
  2. WebSearchTool 在后端缺失 / 搜索失败 / 正常返回时的工具结果
"""

import pytest

from kittymind.config import cfg
from kittymind.tools.builtin.web_search_tool import WebSearchTool, WebSearchToolParam
from kittymind.web_search import SearchResult, get_web_search_backend, reset_web_search_backend

_ENV_KEYS = ("WEB_SEARCH_BACKEND", "WEB_SEARCH_BASE_URL", "WEB_SEARCH_API_KEY")


@pytest.fixture(autouse=True)
def clean_web_search(monkeypatch):
    """每个用例前清空缓存与相关环境变量 / 配置。"""
    reset_web_search_backend()
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cfg, "WEB_SEARCH_BACKEND", "", raising=False)
    monkeypatch.setattr(cfg, "WEB_SEARCH_BASE_URL", "", raising=False)
    yield
    reset_web_search_backend()


def _set_env(monkeypatch, **kwargs):
    for key, value in kwargs.items():
        monkeypatch.setenv(key, value)


# ── get_web_search_backend 解析 ──────────────────────────────────

def test_disabled_by_default():
    assert get_web_search_backend() is None


def test_unknown_backend_returns_none(monkeypatch):
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="not-a-real-backend", WEB_SEARCH_API_KEY="k")
    assert get_web_search_backend() is None


def test_tavily_missing_key_returns_none(monkeypatch):
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="tavily")
    assert get_web_search_backend() is None


def test_searxng_missing_base_url_returns_none(monkeypatch):
    """SearXNG 没有官方公共端点，没给 base_url 就不能用。"""
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="searxng")
    assert get_web_search_backend() is None


def test_searxng_builds_without_api_key(monkeypatch):
    """SearXNG 自建实例通常不需要 key。"""
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="searxng", WEB_SEARCH_BASE_URL="http://localhost:8888")
    backend = get_web_search_backend()
    assert backend is not None


def test_tavily_builds_with_key(monkeypatch):
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="tavily", WEB_SEARCH_API_KEY="k")
    backend = get_web_search_backend()
    assert backend is not None


def test_backend_env_overrides_settings(monkeypatch):
    monkeypatch.setattr(cfg, "WEB_SEARCH_BACKEND", "bing", raising=False)
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="tavily", WEB_SEARCH_API_KEY="k")
    from kittymind.web_search import web_search_settings
    backend, _ = web_search_settings()
    assert backend == "tavily"


def test_failure_not_cached(monkeypatch):
    """缺 key 的失败结果不入缓存：补齐 key 后应能立刻生效。"""
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="tavily")
    assert get_web_search_backend() is None
    _set_env(monkeypatch, WEB_SEARCH_API_KEY="k")
    assert get_web_search_backend() is not None


def test_success_cached(monkeypatch):
    _set_env(monkeypatch, WEB_SEARCH_BACKEND="tavily", WEB_SEARCH_API_KEY="k")
    assert get_web_search_backend() is get_web_search_backend()


# ── WebSearchTool ─────────────────────────────────────────────────

class _FakeBackend:
    def __init__(self, results=None, error=None):
        self._results = results or []
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def search(self, query, top_k):
        self.calls.append((query, top_k))
        if self._error:
            raise self._error
        return self._results


def test_tool_reports_not_configured(monkeypatch):
    """未配置后端：工具返回失败结果，不抛异常中断主循环。"""
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_search_tool.get_web_search_backend", lambda: None
    )
    result = WebSearchTool().run({"query": "天气"})
    assert result.ok is False
    assert "未配置" in result.content


def test_tool_formats_results(monkeypatch):
    fake = _FakeBackend(results=[
        SearchResult(title="标题一", url="http://a.com", snippet="摘要一"),
        SearchResult(title="标题二", url="http://b.com", snippet="摘要二"),
    ])
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_search_tool.get_web_search_backend", lambda: fake
    )
    result = WebSearchTool().run({"query": "天气", "top_k": 2})
    assert result.ok is True
    assert "标题一" in result.content and "http://a.com" in result.content
    assert "标题二" in result.content
    assert fake.calls == [("天气", 2)]


def test_tool_caps_top_k_to_server_limit(monkeypatch):
    monkeypatch.setattr(cfg, "WEB_SEARCH_MAX_RESULTS", 3, raising=False)
    fake = _FakeBackend(results=[])
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_search_tool.get_web_search_backend", lambda: fake
    )
    WebSearchTool().run({"query": "q", "top_k": 10})
    # pydantic 的 le=10 不会拦，工具内部按 cfg.WEB_SEARCH_MAX_RESULTS 再夹一次
    assert fake.calls == [("q", 3)]


def test_tool_empty_results_is_not_a_failure(monkeypatch):
    fake = _FakeBackend(results=[])
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_search_tool.get_web_search_backend", lambda: fake
    )
    result = WebSearchTool().run({"query": "一个查不到任何东西的怪查询"})
    assert result.ok is True
    assert "无搜索结果" in result.content


def test_tool_search_exception_becomes_failed_result(monkeypatch):
    fake = _FakeBackend(error=RuntimeError("网络超时"))
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_search_tool.get_web_search_backend", lambda: fake
    )
    result = WebSearchTool().run({"query": "q"})
    assert result.ok is False
    assert "网络超时" in result.content


def test_param_rejects_top_k_out_of_range():
    with pytest.raises(Exception):
        WebSearchToolParam(query="q", top_k=0)
    with pytest.raises(Exception):
        WebSearchToolParam(query="q", top_k=11)


def test_is_concurrency_safe_and_idempotent():
    """只读无副作用：并行执行安全 + 守护栏按幂等工具对待。"""
    from kittymind.tools.guardrails import IDEMPOTENT_TOOLS, MUTATING_TOOLS

    assert WebSearchTool.is_concurrency_safe is True
    assert "web_search" in IDEMPOTENT_TOOLS
    assert "web_search" not in MUTATING_TOOLS
