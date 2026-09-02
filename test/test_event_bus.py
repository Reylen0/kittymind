"""EventBus 单元测试（无需 LLM）"""

import asyncio
import pytest

from kittymind.events.bus import EventBus
from kittymind.events.types import AGENT_CHUNK, AGENT_DONE, AGENT_START


@pytest.mark.asyncio
async def test_subscribe_and_emit():
    bus = EventBus()
    received = []

    async def handler(event_type, data):
        received.append((event_type, data))

    bus.subscribe(AGENT_CHUNK, handler)
    await bus.emit(AGENT_CHUNK, {"delta": "hello"})

    assert received == [(AGENT_CHUNK, {"delta": "hello"})]


@pytest.mark.asyncio
async def test_decorator_on():
    bus = EventBus()
    received = []

    @bus.on(AGENT_START)
    async def handle_start(event_type, data):
        received.append(data["input"])

    await bus.emit(AGENT_START, {"session_id": "s1", "input": "你好"})
    assert received == ["你好"]


@pytest.mark.asyncio
async def test_wildcard_receives_all():
    bus = EventBus()
    all_events = []

    @bus.on("*")
    async def catch_all(event_type, data):
        all_events.append(event_type)

    await bus.emit(AGENT_START, {})
    await bus.emit(AGENT_CHUNK, {"delta": "x"})
    await bus.emit(AGENT_DONE, {})

    assert all_events == [AGENT_START, AGENT_CHUNK, AGENT_DONE]


@pytest.mark.asyncio
async def test_multiple_handlers():
    bus = EventBus()
    log = []

    async def h1(et, d): log.append("h1")
    async def h2(et, d): log.append("h2")

    bus.subscribe(AGENT_CHUNK, h1)
    bus.subscribe(AGENT_CHUNK, h2)
    await bus.emit(AGENT_CHUNK, {"delta": "x"})

    assert set(log) == {"h1", "h2"}


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    log = []

    async def handler(et, d): log.append("called")

    bus.subscribe(AGENT_CHUNK, handler)
    bus.unsubscribe(AGENT_CHUNK, handler)
    await bus.emit(AGENT_CHUNK, {"delta": "x"})

    assert log == []


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_others():
    """一个 handler 抛异常不应阻止其他 handler 执行。"""
    bus = EventBus()
    log = []

    async def bad(et, d): raise ValueError("boom")
    async def good(et, d): log.append("ok")

    bus.subscribe(AGENT_CHUNK, bad)
    bus.subscribe(AGENT_CHUNK, good)
    # emit 用 return_exceptions=True，不应抛出
    await bus.emit(AGENT_CHUNK, {"delta": "x"})

    assert log == ["ok"]


if __name__ == "__main__":
    asyncio.run(pytest.main([__file__, "-v"]))
