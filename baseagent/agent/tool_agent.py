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
from ..core.agent import Agent


class ToolAgent(Agent):
    """带工具的 Agent，使用全程流式 ReAct 循环。

    每步调用 llm.stream_with_tools()：
      - 文字 delta 实时 yield 给调用者
      - 流结束后检测是否有工具调用
      - 有则执行工具并追加结果，继续下一步
      - 无则本步即为最终回答，循环结束
    """

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
    # 非流式 run()：工具循环 + invoke()，适合后台/子 Agent 调用
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
                    response = self.llm.invoke(
                        messages=messages,
                        tools=tools_schema or None,
                        **kwargs,
                    )
                except Exception as e:
                    self._emit("on_llm_error", e)
                    raise LLMException(f"LLM调用失败: {str(e)}")
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
                raise AgentException(
                    f"超过最大迭代次数 {self.max_iterations}，未能得到最终答案"
                )

            if session_id is not None:
                self._save_turn(session_id, input_text, messages, final_text)

            self._emit("on_agent_end", self.name, final_text)
            return final_text

        except Exception as e:
            self._emit("on_agent_error", self.name, e)
            raise

    # ──────────────────────────────────────────────────────────────
    # 全程流式 stream_run()：每步 stream_with_tools()
    # ──────────────────────────────────────────────────────────────

    def stream_run(
        self, session_id: str | None, input_text: str, **kwargs
    ) -> Iterator[str]:
        """全程流式 ReAct 循环。

        每步使用 stream_with_tools()：
          - 文字 delta 立即 yield 给调用者（实时显示）
          - 流结束后检测工具调用，有则执行并继续，无则结束
        不会重复调用 LLM 来"换成流式"。
        """
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
                        messages=messages,
                        tools=tools_schema or None,
                        **kwargs,
                    ):
                        if event.type == "text_delta":
                            text_chunks.append(event.delta)
                            yield event.delta          # 实时推给调用者
                        elif event.type == "tool_calls_done":
                            tool_calls = event.tool_calls
                except Exception as e:
                    self._emit("on_llm_error", e)
                    raise LLMException(f"LLM流式调用失败: {str(e)}")

                self._emit("on_llm_end", None)

                step_text = "".join(text_chunks)

                if not tool_calls:
                    # 无工具调用：本步流式内容就是最终回答，已 yield 完毕
                    final_text = step_text
                    break

                # 有工具调用：追加 assistant 消息，执行工具，继续循环
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
                raise AgentException(
                    f"超过最大迭代次数 {self.max_iterations}，未能得到最终答案"
                )

            if session_id is not None:
                self._save_turn(session_id, input_text, messages, final_text)

            self._emit("on_agent_end", self.name, final_text)

        except Exception as e:
            self._emit("on_agent_error", self.name, e)
            raise

    # ──────────────────────────────────────────────────────────────
    # 异步流式 async_stream_run()：适用于 WebSocket server
    # ──────────────────────────────────────────────────────────────

    async def async_stream_run(
        self, session_id: str | None, input_text: str, **kwargs
    ) -> AsyncIterator[str]:
        """stream_run() 的异步包装，供 asyncio WebSocket server 使用。

        把同步流式生成器跑在后台线程，通过 asyncio.Queue 桥接到协程。
        """
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

        thread = threading.Thread(target=_producer, daemon=True)
        thread.start()

        while True:
            item = await aqueue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    # ──────────────────────────────────────────────────────────────
    # 内部工具方法
    # ──────────────────────────────────────────────────────────────

    def _build_messages(self, session_id: str | None, input_text: str) -> list[dict]:
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if session_id is not None:
            messages.extend(self.get_context(session_id))
        messages.append({"role": "user", "content": input_text})
        return messages

    def _save_turn(
        self,
        session_id: str,
        input_text: str,
        messages: list[dict],
        final_text: str,
    ) -> None:
        """把本轮所有消息（含工具调用/结果）保存到 memory。"""
        # user 消息始终是 messages 里倒数第二段之前的最后一条 user 消息
        # 这里简单地保存 user + 所有工具相关消息 + assistant 最终回答
        self.add_message(session_id, Message(input_text, "user"))
        # 保存本轮新增的 assistant/tool 消息（system 和历史已在 memory 里）
        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                self.add_message(session_id, Message(
                    content=msg.get("content"),
                    role="assistant",
                    tool_calls=msg.get("tool_calls"),
                ))
            elif role == "tool":
                self.add_message(session_id, Message(
                    content=msg.get("content", ""),
                    role="tool",
                    tool_call_id=msg.get("tool_call_id"),
                ))
        self.add_message(session_id, Message(final_text, "assistant"))
