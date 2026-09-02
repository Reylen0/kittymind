import asyncio
import threading
from typing import AsyncIterator, Optional

from baseagent.agent.tool_agent import ToolAgent
from baseagent.core.llm import BaseAgentLLM
from baseagent.core.exceptions import AgentException, LLMException
from baseagent.core.message import Message
from baseagent.events.bus import EventBus
from baseagent.events.types import (
    AGENT_START, AGENT_THINKING, AGENT_CHUNK,
    AGENT_TOOL_CALL, AGENT_TOOL_RESULT, AGENT_DONE, AGENT_ERROR,
)
from baseagent.memory.base import BaseMemory
from baseagent.session.manager import SessionManager
from baseagent.tools.base import BaseTool


class KittyAgent(ToolAgent):
    """KittyMind 主 Agent。

    在 ToolAgent 全程流式 ReAct 基础上，通过 EventBus 广播状态事件：
      - agent.start / agent.thinking / agent.chunk
      - agent.tool_call / agent.tool_result
      - agent.done / agent.error

    可选注入 SessionManager 实现跨重启的会话持久化：
      - 首次访问某 session_id 时自动从 JSONL 加载历史
      - 每轮结束后将本轮消息追加写入 JSONL
    """

    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        tools: Optional[list[BaseTool]] = None,
        memory: Optional[BaseMemory] = None,
        description: Optional[str] = None,
        event_bus: Optional[EventBus] = None,
        session_manager: Optional[SessionManager] = None,
        max_iterations: int = 10,
    ):
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
            memory=memory,
            description=description,
            max_iterations=max_iterations,
        )
        self.event_bus: EventBus = event_bus or EventBus()
        self.session_manager: Optional[SessionManager] = session_manager
        # 已从 JSONL 加载过历史的 session，避免重复加载
        self._loaded_sessions: set[str] = set()

    # ──────────────────────────────────────────────────────────────
    # 带事件广播的异步流式 ReAct 循环
    # ──────────────────────────────────────────────────────────────

    async def async_stream_run(
        self, session_id: str | None, input_text: str, **kwargs
    ) -> AsyncIterator[str]:
        """带事件广播的异步流式 ReAct 循环。

        文字 delta 实时 yield 同时 emit agent.chunk；
        工具调用前后 emit agent.tool_call / agent.tool_result。
        若注入了 SessionManager，自动加载历史并在每轮结束后持久化。
        """
        bus = self.event_bus

        # 首次访问该 session：从 JSONL 恢复历史到内存
        if self.session_manager and session_id and session_id not in self._loaded_sessions:
            self._loaded_sessions.add(session_id)
            if self.session_manager.session_exists(session_id):
                for msg in self.session_manager.load_history(session_id):
                    self.add_message(session_id, Message(
                        msg.get("content"), msg["role"],
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                    ))

        await bus.emit(AGENT_START, {"session_id": session_id, "input": input_text})

        messages = self._build_messages(session_id, input_text)
        tools_schema = self.tool_registry.get_schemas()
        final_text: str | None = None
        # 本轮新增消息（user → 中间工具步 → final assistant），用于持久化和更新内存
        turn_messages: list[dict] = [{"role": "user", "content": input_text}]

        try:
            for _ in range(self.max_iterations):
                await bus.emit(AGENT_THINKING, {"session_id": session_id})

                text_chunks: list[str] = []
                tool_calls: list[dict] = []

                try:
                    async for event in self._stream_with_tools_async(
                        messages, tools_schema or None, **kwargs
                    ):
                        if event.type == "text_delta":
                            text_chunks.append(event.delta)
                            await bus.emit(AGENT_CHUNK, {
                                "delta": event.delta, "session_id": session_id
                            })
                            yield event.delta
                        elif event.type == "tool_calls_done":
                            tool_calls = event.tool_calls
                except Exception as e:
                    raise LLMException(f"LLM流式调用失败: {e}")

                step_text = "".join(text_chunks)

                if not tool_calls:
                    final_text = step_text
                    break

                assistant_msg = {
                    "role": "assistant",
                    "content": step_text or None,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_msg)
                turn_messages.append(assistant_msg)

                for tool_call in tool_calls:
                    name = tool_call["function"]["name"]
                    await bus.emit(AGENT_TOOL_CALL, {
                        "name": name,
                        "args": tool_call["function"].get("arguments"),
                        "session_id": session_id,
                    })
                    result = await asyncio.to_thread(
                        self.tool_executor.execute, tool_call=tool_call
                    )
                    await bus.emit(AGENT_TOOL_RESULT, {
                        "name": name,
                        "result": result.get("content"),
                        "session_id": session_id,
                    })
                    messages.append(result)
                    turn_messages.append(result)

            if final_text is None:
                raise AgentException(f"超过最大迭代次数 {self.max_iterations}")

            turn_messages.append({"role": "assistant", "content": final_text})

            if session_id is not None:
                # 更新内存（供同进程内后续轮次使用）
                for msg in turn_messages:
                    self.add_message(session_id, Message(
                        msg.get("content"), msg["role"],
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                    ))
                # 持久化到 JSONL
                if self.session_manager:
                    self.session_manager.append_turn(session_id, turn_messages, input_text)

            await bus.emit(AGENT_DONE, {"session_id": session_id, "text": final_text})

        except Exception as e:
            await bus.emit(AGENT_ERROR, {"session_id": session_id, "error": str(e)})
            raise

    # ──────────────────────────────────────────────────────────────
    # 内部：同步生成器 → 异步生成器桥接（Thread + asyncio.Queue）
    # ──────────────────────────────────────────────────────────────

    async def _stream_with_tools_async(self, messages, tools, **kwargs):
        """将同步 stream_with_tools() 包装为异步生成器。

        生产者线程通过 call_soon_threadsafe 将 StreamEvent 投入队列，
        消费者在事件循环里 await 取出并 yield。
        """
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def _producer():
            try:
                for event in self.llm.stream_with_tools(
                    messages=messages, tools=tools, **kwargs
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        threading.Thread(target=_producer, daemon=True).start()

        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
