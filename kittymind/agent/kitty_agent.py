import asyncio
import time
from typing import AsyncIterator, Optional

from ..config import cfg
from ..context import ContextCompressor, TokenTracker, prune_tool_outputs
from ..core.exceptions import AgentException, LLMException
from ..core.llm import BaseAgentLLM
from ..core.message import Message
from ..events.bus import EventBus
from ..events.types import (
    AGENT_START, AGENT_THINKING, AGENT_CHUNK,
    AGENT_TOOL_CALL, AGENT_TOOL_RESULT, AGENT_DONE, AGENT_ERROR,
)
from ..memory.extract import extract_memories
from ..memory.recall import MemoryRecall
from ..memory.store import MemoryStore
from ..session.manager import SessionManager
from ..tools.base import BaseTool
from ..tools.builtin.bash_tool import bash_cwd
from ..tools.permission import PermissionToolExecutor
from .tool_agent import ToolAgent


class KittyAgent(ToolAgent):
    """KittyMind 主 Agent。

    在 ToolAgent 全程流式 ReAct 基础上，通过 EventBus 广播状态事件：
      - agent.start / agent.thinking / agent.chunk
      - agent.tool_call / agent.tool_result
      - agent.done / agent.error

    可选注入 SessionManager 实现跨重启的会话持久化。
    可选注入 WorkspaceManager 实现工作区 cwd 绑定。
    """

    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        tools: Optional[list[BaseTool]] = None,
        description: Optional[str] = None,
        event_bus: Optional[EventBus] = None,
        session_manager: Optional[SessionManager] = None,
        workspace_manager=None,
        memory: Optional[MemoryStore] = None,
        max_iterations: int = cfg.AGENT_MAX_ITERATIONS,
        ask_fn=None,
    ):
        super().__init__(
            name=name, llm=llm, system_prompt=system_prompt,
            tools=tools, description=description,
            max_iterations=max_iterations,
        )
        if ask_fn is not None:
            self.tool_executor = PermissionToolExecutor(
                self.tool_registry, ask_fn=ask_fn
            )
        self.event_bus: EventBus = event_bus or EventBus()
        self.session_manager: Optional[SessionManager] = session_manager
        self.workspace_manager = workspace_manager
        self.memory: Optional[MemoryStore] = memory
        self._memory_recall: Optional[MemoryRecall] = (
            MemoryRecall(memory, llm) if memory else None
        )
        self._loaded_sessions: set[str] = set()

    # ── 主循环 ────────────────────────────────────────────────────

    async def async_stream_run(
        self, session_id: str | None, input_text: str,
        workspace_id: str | None = None, **kwargs
    ) -> AsyncIterator[str]:
        """带事件广播的异步流式 ReAct 循环。"""
        self._load_session_history(session_id)
        effective_workspace_id, cwd_path = self._resolve_workspace(session_id, workspace_id)

        cwd_token = bash_cwd.set(cwd_path)
        try:
            await self.event_bus.emit(AGENT_START, {"session_id": session_id, "input": input_text})

            messages = self._build_messages(session_id, input_text)
            await self._inject_memory_recall(messages, input_text)

            tools_schema = self.tool_registry.get_schemas() or None
            final_text: str | None = None
            turn_messages: list[dict] = [{"role": "user", "content": input_text}]

            tracker = TokenTracker(cfg.LLM_CONTEXT_WINDOW, cfg.LLM_RESERVED_OUTPUT_TOKENS)
            compressor = ContextCompressor(self.llm)
            cooldown_until = 0.0
            ineffective_count = 0

            try:
                for _ in range(self.max_iterations):
                    messages, tracker, did_compress = self._maybe_compress(
                        messages, tracker, compressor, cooldown_until
                    )

                    await self.event_bus.emit(AGENT_THINKING, {"session_id": session_id})

                    text_chunks: list[str] = []
                    tool_calls: list[dict] = []

                    try:
                        async for event in self._stream_with_tools_async(
                            messages, tools_schema, **kwargs
                        ):
                            if event.type == "text_delta":
                                text_chunks.append(event.delta)
                                await self.event_bus.emit(AGENT_CHUNK, {
                                    "delta": event.delta, "session_id": session_id,
                                })
                                yield event.delta
                            elif event.type == "tool_calls_done":
                                tool_calls = event.tool_calls
                            elif event.type == "usage" and event.usage:
                                tracker.update_from_usage(event.usage)
                                if did_compress:
                                    cooldown_until, ineffective_count = self._check_anti_thrash(
                                        event.usage, cooldown_until, ineffective_count
                                    )
                                    did_compress = False
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
                        await self.event_bus.emit(AGENT_TOOL_CALL, {
                            "name": name,
                            "args": tool_call["function"].get("arguments"),
                            "session_id": session_id,
                        })
                        result = await asyncio.to_thread(
                            self.tool_executor.execute, tool_call=tool_call
                        )
                        await self.event_bus.emit(AGENT_TOOL_RESULT, {
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
                    self._commit_turn(
                        session_id, messages, turn_messages,
                        compressor, effective_workspace_id, input_text,
                    )

                await self.event_bus.emit(AGENT_DONE, {"session_id": session_id, "text": final_text})

                if self.memory is not None:
                    asyncio.create_task(self._extract_memories_bg(turn_messages))

            except Exception as e:
                await self.event_bus.emit(AGENT_ERROR, {"session_id": session_id, "error": str(e)})
                raise

        finally:
            bash_cwd.reset(cwd_token)

    # ── 会话 / 工作区准备 ─────────────────────────────────────────

    def _load_session_history(self, session_id: str | None) -> None:
        """首次访问 session 时从 JSONL 恢复历史到内存。"""
        if not (self.session_manager and session_id and session_id not in self._loaded_sessions):
            return
        self._loaded_sessions.add(session_id)
        if self.session_manager.session_exists(session_id):
            for msg in self.session_manager.load_history(session_id):
                self.add_message(session_id, Message(
                    msg.get("content"), msg["role"],
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                ))

    def _resolve_workspace(
        self, session_id: str | None, workspace_id: str | None
    ) -> tuple[str | None, str]:
        """解析生效的 workspace_id 和对应 cwd 路径。"""
        effective = workspace_id
        if not effective and self.session_manager and session_id:
            session_data = self.session_manager.get_session(session_id)
            if session_data:
                effective = session_data["header"].get("workspace_id")
        if effective and self.workspace_manager:
            ws = self.workspace_manager.get_workspace(effective)
            if ws and ws.get("path"):
                return effective, ws["path"]
        return effective, str(cfg.DEFAULT_WORKSPACE_DIR)

    async def _inject_memory_recall(self, messages: list[dict], input_text: str) -> None:
        """召回相关长期记忆，追加到 messages[0] system prompt 末尾。"""
        if not self._memory_recall:
            return
        try:
            relevant = await asyncio.to_thread(
                self._memory_recall.select_relevant, input_text
            )
            recall_section = self._memory_recall.build_recall_section(relevant)
            if not recall_section or not messages:
                return
            if messages[0]["role"] == "system":
                messages[0] = {**messages[0], "content": messages[0]["content"] + recall_section}
            else:
                messages.insert(0, {"role": "system", "content": recall_section})
        except Exception:
            pass

    # ── 压缩 ──────────────────────────────────────────────────────

    def _maybe_compress(
        self,
        messages: list[dict],
        tracker: TokenTracker,
        compressor: ContextCompressor,
        cooldown_until: float,
    ) -> tuple[list[dict], TokenTracker, bool]:
        """微压缩 + 主压缩。返回 (messages, tracker, did_compress)。"""
        messages = prune_tool_outputs(messages)
        ratio = tracker.ratio(messages)
        if ratio < cfg.COMPRESS_THRESHOLD_RATIO or time.monotonic() < cooldown_until:
            return messages, tracker, False
        compressed = compressor.compress(messages, tracker)
        if compressed is messages:
            return messages, tracker, False
        print(f"[compress] ratio={ratio:.2f}，{len(messages)} → {len(compressed)}", flush=True)
        return compressed, TokenTracker(cfg.LLM_CONTEXT_WINDOW, cfg.LLM_RESERVED_OUTPUT_TOKENS), True

    def _check_anti_thrash(
        self,
        usage: dict,
        cooldown_until: float,
        ineffective_count: int,
    ) -> tuple[float, int]:
        """压缩后收到真实 usage 时检查是否无效，更新反抖动状态。"""
        pt = usage.get("prompt_tokens", 0)
        threshold = int(cfg.LLM_CONTEXT_WINDOW * cfg.COMPRESS_THRESHOLD_RATIO)
        if pt >= threshold:
            ineffective_count += 1
            if ineffective_count >= 2:
                cooldown_until = time.monotonic() + cfg.COMPRESS_COOLDOWN_SECONDS
                print(f"[compress] 反抖动：冷却 {cfg.COMPRESS_COOLDOWN_SECONDS}s", flush=True)
        else:
            ineffective_count = 0
        return cooldown_until, ineffective_count

    # ── 结果落库 ──────────────────────────────────────────────────

    def _commit_turn(
        self,
        session_id: str,
        messages: list[dict],
        turn_messages: list[dict],
        compressor: ContextCompressor,
        effective_workspace_id: str | None,
        input_text: str,
    ) -> None:
        """将本轮结果写回 _history 和 session_manager。"""
        if compressor._compressed_once:
            # messages   = [system?, ...压缩后历史..., user_curr, ...中间工具轮...]
            # turn_messages = [user_curr, ...中间工具轮..., final_assistant]
            # final_assistant 已追加到 turn_messages 但未追加到 messages，
            # 所以当前轮在 messages 里的条数 = len(turn_messages) - 1
            start = 1 if messages and messages[0].get("role") == "system" else 0
            end = len(messages) - (len(turn_messages) - 1)
            if end > start:
                self.replace_history(session_id, messages[start:end])

        for msg in turn_messages:
            self.add_message(session_id, Message(
                msg.get("content"), msg["role"],
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            ))
        if self.session_manager:
            self.session_manager.append_turn(
                session_id, turn_messages, input_text,
                workspace_id=effective_workspace_id,
            )

    # ── 后台任务 ──────────────────────────────────────────────────

    async def _extract_memories_bg(self, turn_messages: list[dict]) -> None:
        """在后台线程中运行记忆提取，不阻塞当前对话。"""
        try:
            await asyncio.to_thread(
                extract_memories, turn_messages, self.llm, self.memory
            )
        except Exception as e:
            print(f"[memory] background extraction error: {e}", flush=True)

    async def _stream_with_tools_async(self, messages, tools, **kwargs):
        """直接调用 AsyncOpenAI 流式接口的异步生成器。"""
        async for event in self.llm.async_stream_with_tools(
            messages=messages, tools=tools, **kwargs
        ):
            yield event
