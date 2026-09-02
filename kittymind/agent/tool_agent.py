import asyncio
import threading
from typing import AsyncIterator, Iterator, Optional

from ..callbacks.base import BaseCallBack
from ..core.exceptions import AgentException, LLMException
from ..core.llm import BaseAgentLLM
from ..core.message import Message
from ..memory.base import BaseMemory
from ..tools.base import BaseTool
from ..tools.executor import ToolExecutor
from ..tools.registry import ToolRegistry
from .base import Agent


class ToolAgent(Agent):
    """带工具的 Agent，使用全程流式 ReAct 循环。"""

    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        tools: Optional[list[BaseTool]] = None,
        memory: Optional[BaseMemory] = None,
        description: Optional[str] = None,
        callbacks: Optional[list[BaseCallBack]] = None,
        max_iterations: int = 10,
    ):
        super().__init__(name, llm, system_prompt, memory, description, callbacks)
        self.max_iterations = max_iterations
        self.tools = tools or []
        self.tool_registry = ToolRegistry()
        for tool in self.tools:
            self.tool_registry.register(tool)
        self.tool_executor = ToolExecutor(self.tool_registry)

    # ──────────────────────────────────────────────────────────────
    # 非流式 run()
    # ──────────────────────────────────────────────────────────────

    def run(self, session_id: str | None, input_text: str, **kwargs) -> str:
        try:
            self._emit("on_agent_start", self.name, input_text)
            messages = self._build_messages(session_id, input_text)
            tools_schema = self.tool_registry.get_schemas()
            final_text: str | None = None

            for _ in range(self.max_iterations):
                self._emit("on_llm_start", messages)
                try:
                    response = self.llm.invoke(messages=messages, tools=tools_schema or None, **kwargs)
                except Exception as e:
                    self._emit("on_llm_error", e)
                    raise LLMException(f"LLM调用失败: {e}")
                self._emit("on_llm_end", response)

                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": response.tool_calls or None,
                })

                if response.is_tool_call():
                    for tool_call in response.tool_calls:
                        name = tool_call["function"]["name"]
                        self._emit("on_tool_start", name, tool_call)
                        result = self.tool_executor.execute(tool_call=tool_call)
                        self._emit("on_tool_end", name, result)
                        messages.append(result)
                else:
                    final_text = response.content
                    break

            if final_text is None:
                raise AgentException(f"超过最大迭代次数 {self.max_iterations}")

            if session_id is not None:
                self._save_turn(session_id, input_text, messages, final_text)
            self._emit("on_agent_end", self.name, final_text)
            return final_text

        except Exception as e:
            self._emit("on_agent_error", self.name, e)
            raise

    # ──────────────────────────────────────────────────────────────
    # 全程流式 stream_run()
    # ──────────────────────────────────────────────────────────────

    def stream_run(
        self, session_id: str | None, input_text: str, **kwargs
    ) -> Iterator[str]:
        try:
            self._emit("on_agent_start", self.name, input_text)
            messages = self._build_messages(session_id, input_text)
            tools_schema = self.tool_registry.get_schemas()
            final_text: str | None = None

            for _ in range(self.max_iterations):
                self._emit("on_llm_start", messages)
                text_chunks: list[str] = []
                tool_calls: list[dict] = []

                try:
                    for event in self.llm.stream_with_tools(
                        messages=messages, tools=tools_schema or None, **kwargs
                    ):
                        if event.type == "text_delta":
                            text_chunks.append(event.delta)
                            yield event.delta
                        elif event.type == "tool_calls_done":
                            tool_calls = event.tool_calls
                except Exception as e:
                    self._emit("on_llm_error", e)
                    raise LLMException(f"LLM流式调用失败: {e}")

                self._emit("on_llm_end", None)
                step_text = "".join(text_chunks)

                if not tool_calls:
                    final_text = step_text
                    break

                messages.append({
                    "role": "assistant",
                    "content": step_text or None,
                    "tool_calls": tool_calls,
                })
                for tool_call in tool_calls:
                    name = tool_call["function"]["name"]
                    self._emit("on_tool_start", name, tool_call)
                    result = self.tool_executor.execute(tool_call=tool_call)
                    self._emit("on_tool_end", name, result)
                    messages.append(result)

            if final_text is None:
                raise AgentException(f"超过最大迭代次数 {self.max_iterations}")

            if session_id is not None:
                self._save_turn(session_id, input_text, messages, final_text)
            self._emit("on_agent_end", self.name, final_text)

        except Exception as e:
            self._emit("on_agent_error", self.name, e)
            raise

    # ──────────────────────────────────────────────────────────────
    # 异步包装 async_stream_run()（Thread + asyncio.Queue 桥接）
    # ──────────────────────────────────────────────────────────────

    async def async_stream_run(
        self, session_id: str | None, input_text: str, **kwargs
    ) -> AsyncIterator[str]:
        loop = asyncio.get_event_loop()
        aqueue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def _producer():
            try:
                for chunk in self.stream_run(session_id, input_text, **kwargs):
                    loop.call_soon_threadsafe(aqueue.put_nowait, chunk)
            except Exception as exc:
                loop.call_soon_threadsafe(aqueue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(aqueue.put_nowait, _DONE)

        threading.Thread(target=_producer, daemon=True).start()

        while True:
            item = await aqueue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    # ──────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────

    def _build_messages(self, session_id: str | None, input_text: str) -> list[dict]:
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if session_id is not None:
            messages.extend(self.get_context(session_id))
        messages.append({"role": "user", "content": input_text})
        return messages

    def _save_turn(self, session_id: str, input_text: str, messages: list[dict], final_text: str) -> None:
        self.add_message(session_id, Message(input_text, "user"))
        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                self.add_message(session_id, Message(
                    content=msg.get("content"), role="assistant",
                    tool_calls=msg.get("tool_calls"),
                ))
            elif role == "tool":
                self.add_message(session_id, Message(
                    content=msg.get("content", ""), role="tool",
                    tool_call_id=msg.get("tool_call_id"),
                ))
        self.add_message(session_id, Message(final_text, "assistant"))
