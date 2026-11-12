"""LLM 调用瞬时错误重试的特征测试。

核心主张：
  - 只重试网络层 / 429 / 5xx / 超时这类瞬时错误，业务错误（400 等）不重试；
  - `invoke`（非流式）失败后按指数退避重试，次数耗尽才真正抛出；
  - `async_stream_with_tools`（流式）的重试边界卡在第一个事件产出之前——
    一旦已经有事件流给调用方，失败就不再重试，直接抛出。
"""

import httpx
import openai
import pytest

from kittymind.config import cfg
from kittymind.core.exceptions import LLMException
from kittymind.core.llm import BaseAgentLLM
from kittymind.core.llm_response import LLMResponse, StreamEvent

pytestmark = pytest.mark.usefixtures("_tiny_retry_delay")


@pytest.fixture
def _tiny_retry_delay(monkeypatch):
    """所有用例统一把退避基础延迟降到几乎为 0，不然重试测试会真的睡几秒。"""
    monkeypatch.setattr(cfg, "LLM_RETRY_BASE_DELAY", 0.001)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://example.com/v1/chat/completions")


def _connection_error() -> Exception:
    return openai.APIConnectionError(request=_request())


def _timeout_error() -> Exception:
    return openai.APITimeoutError(_request())


def _rate_limit_error() -> Exception:
    resp = httpx.Response(429, request=_request())
    return openai.RateLimitError("rate limited", response=resp, body=None)


def _internal_server_error() -> Exception:
    resp = httpx.Response(500, request=_request())
    return openai.InternalServerError("internal error", response=resp, body=None)


def _bad_request_error() -> Exception:
    resp = httpx.Response(400, request=_request())
    return openai.BadRequestError("bad request", response=resp, body=None)


def _make_llm() -> BaseAgentLLM:
    return BaseAgentLLM(model="gpt-4", api_key="test-key", base_url="https://example.com/v1")


class _FakeInvokeAdapter:
    """`invoke()` 前 N 次抛瞬时错误，之后返回固定结果。"""

    def __init__(self, raise_n_times: int, exc_factory, result: LLMResponse):
        self._raise_n_times = raise_n_times
        self._exc_factory = exc_factory
        self._result = result
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self._raise_n_times:
            raise self._exc_factory()
        return self._result


# ── invoke（非流式）──────────────────────────────────────────────

@pytest.mark.parametrize("exc_factory", [_connection_error, _timeout_error,
                                          _rate_limit_error, _internal_server_error])
def test_invoke_retries_transient_error_then_succeeds(monkeypatch, exc_factory):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 3)
    llm = _make_llm()
    fake = _FakeInvokeAdapter(raise_n_times=2, exc_factory=exc_factory,
                               result=LLMResponse(content="ok", tool_calls=[]))
    llm._client = fake

    result = llm.invoke([{"role": "user", "content": "hi"}])

    assert result.content == "ok"
    assert fake.calls == 3  # 首发失败 2 次 + 第 3 次成功


def test_invoke_raises_llm_exception_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 2)
    llm = _make_llm()
    fake = _FakeInvokeAdapter(raise_n_times=99, exc_factory=_connection_error,
                               result=LLMResponse(content="unreachable"))
    llm._client = fake

    with pytest.raises(LLMException):
        llm.invoke([{"role": "user", "content": "hi"}])

    assert fake.calls == 3  # 首发 1 次 + 重试 2 次，耗尽后才真正放弃


def test_invoke_does_not_retry_business_errors(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 5)
    llm = _make_llm()
    fake = _FakeInvokeAdapter(raise_n_times=99, exc_factory=_bad_request_error,
                               result=LLMResponse(content="unreachable"))
    llm._client = fake

    with pytest.raises(LLMException):
        llm.invoke([{"role": "user", "content": "hi"}])

    assert fake.calls == 1, "400 这类业务错误不该被当成瞬时错误重试"


# ── async_stream_with_tools（流式）────────────────────────────────

class _FakeStreamAdapter:
    """`async_stream_with_tools()` 前 N 次在产出任何事件之前就抛瞬时错误。"""

    def __init__(self, raise_n_times: int, exc_factory, events: list[StreamEvent]):
        self._raise_n_times = raise_n_times
        self._exc_factory = exc_factory
        self._events = events
        self.calls = 0

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= self._raise_n_times:
            raise self._exc_factory()
        for ev in self._events:
            yield ev


class _MidStreamFailAdapter:
    """先产出一个事件，再抛错——模拟"已经开始流给前端"之后才失败的场景。"""

    def __init__(self, exc_factory):
        self._exc_factory = exc_factory
        self.calls = 0

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        yield StreamEvent(type="text_delta", delta="partial")
        raise self._exc_factory()


async def test_stream_retries_before_first_event_then_succeeds(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 3)
    llm = _make_llm()
    fake = _FakeStreamAdapter(
        raise_n_times=1, exc_factory=_internal_server_error,
        events=[StreamEvent(type="text_delta", delta="hi"), StreamEvent(type="usage", usage={})],
    )
    llm._client = fake

    events = [e async for e in llm.async_stream_with_tools([{"role": "user", "content": "hi"}])]

    assert [e.type for e in events] == ["text_delta", "usage"]
    assert fake.calls == 2


async def test_stream_raises_after_exhausting_retries_without_any_event(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 2)
    llm = _make_llm()
    fake = _FakeStreamAdapter(raise_n_times=99, exc_factory=_rate_limit_error, events=[])
    llm._client = fake

    with pytest.raises(LLMException):
        async for _ in llm.async_stream_with_tools([{"role": "user", "content": "hi"}]):
            pass

    assert fake.calls == 3


async def test_stream_does_not_retry_once_an_event_has_already_been_yielded(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 5)
    llm = _make_llm()
    fake = _MidStreamFailAdapter(exc_factory=_rate_limit_error)
    llm._client = fake

    received = []
    with pytest.raises(LLMException):
        async for ev in llm.async_stream_with_tools([{"role": "user", "content": "hi"}]):
            received.append(ev)

    assert [e.delta for e in received] == ["partial"]
    assert fake.calls == 1, "已经有事件流给调用方后，失败不能重试（会导致内容重复/错乱）"


async def test_stream_does_not_retry_business_errors(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_RETRY_MAX_ATTEMPTS", 5)
    llm = _make_llm()
    fake = _FakeStreamAdapter(raise_n_times=99, exc_factory=_bad_request_error, events=[])
    llm._client = fake

    with pytest.raises(LLMException):
        async for _ in llm.async_stream_with_tools([{"role": "user", "content": "hi"}]):
            pass

    assert fake.calls == 1
