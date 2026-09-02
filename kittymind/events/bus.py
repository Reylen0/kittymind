import asyncio
from collections import defaultdict
from typing import Callable


class EventBus:
    """asyncio Pub/Sub 事件总线。

    用法::

        bus = EventBus()

        @bus.on("agent.chunk")
        async def handle_chunk(event_type, data):
            print(data["delta"])

        await bus.emit("agent.chunk", {"delta": "hello"})

    支持通配符订阅（event_type="*"）接收所有事件。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    def on(self, event_type: str) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.subscribe(event_type, handler)
            return handler
        return decorator

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, event_type: str, data: dict | None = None) -> None:
        """触发事件，并发执行所有匹配的处理器。异常静默捕获不中断其他处理器。"""
        data = data or {}
        tasks = [h(event_type, data) for h in self._handlers.get(event_type, [])]
        tasks += [h(event_type, data) for h in self._handlers.get("*", [])]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
