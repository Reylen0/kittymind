"""KittyMind 主 Agent。

直接继承 Agent，包含：
  - 工具 ReAct 引擎（同步 run / 同步流式 stream_run / 异步流式 async_stream_run）
  - 上下文压缩（三条路径均可触发，压缩状态跨轮持久化到 SessionManager）
  - 桌面集成（EventBus / SessionManager / WorkspaceManager / MemoryStore）
  - 委派守护（DelegationBudget 根预算）

子 Agent 调 run(session_id=None)，不触发任何集成层副作用。

Phase 14 阶段二：
  - 删除 _history 内存镜像，每轮从 SQLite active 视图重建上下文。
  - _build_messages 返回 (messages, loaded_seqs)；历史消息带瞬态 _seq 标记。
  - LLM 调用前剥离 _seq / _compressed_summary（_strip_internal）。
  - _commit_turn：有压缩时走 archive_and_compact 落库，否则 append_turn。
"""

import asyncio
import time
from typing import AsyncIterator, Iterator, Optional

from ..callbacks.base import BaseCallBack
from ..config import cfg
from ..context import ContextCompressor, TokenTracker, prune_tool_outputs
from ..context.compressor import _SUMMARY_MARKER
from ..core.exceptions import AgentException, LLMException
from ..core.llm import BaseAgentLLM
from ..events.bus import EventBus
from ..events.types import (
    AGENT_CHUNK, AGENT_CONTEXT_USAGE, AGENT_DONE, AGENT_ERROR,
    AGENT_START, AGENT_THINKING, AGENT_TOOL_CALL, AGENT_TOOL_RESULT,
)
from ..memory.extract import extract_memories
from ..memory.recall import MemoryRecall
from ..memory.store import MemoryStore
from ..session.manager import SessionManager
from ..tools.base import BaseTool
from ..tools.builtin.bash_tool import bash_cwd
from ..tools.executor import ToolExecutor
from ..tools.permission import PermissionToolExecutor
from ..tools.registry import ToolRegistry
from ..workspace.manager import WorkspaceManager
from .base import Agent
from .delegation import (
    reset_root_budget, reset_root_session,
    set_root_budget, set_root_session,
)

# 剥离时要移除的内部键（不得泄漏给 LLM API）
_INTERNAL_KEYS = ("_seq", _SUMMARY_MARKER)


class KittyAgent(Agent):
    """KittyMind 统一 Agent。"""

    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        tools: Optional[list[BaseTool]] = None,
        description: Optional[str] = None,
        callbacks: Optional[list[BaseCallBack]] = None,
        max_iterations: int = cfg.AGENT_MAX_ITERATIONS,
        # 集成层（子 Agent 不传）
        event_bus: Optional[EventBus] = None,
        session_manager: Optional[SessionManager] = None,
        workspace_manager=None,
        memory: Optional[MemoryStore] = None,
        ask_fn=None,
    ):
        super().__init__(name, llm, system_prompt, description, callbacks)
        self.max_iterations = max_iterations
        self.tools = tools or []
        self.tool_registry = ToolRegistry()
        for tool in self.tools:
            self.tool_registry.register(tool)
        self.tool_executor: ToolExecutor = (
            PermissionToolExecutor(self.tool_registry, ask_fn=ask_fn)
            if ask_fn is not None
            else ToolExecutor(self.tool_registry)
        )
        self.last_messages: list[dict] = []

        self.event_bus: EventBus = event_bus or EventBus()
        self.session_manager: Optional[SessionManager] = session_manager
        self.workspace_manager = workspace_manager
        self.memory: Optional[MemoryStore] = memory
        self._memory_recall: Optional[MemoryRecall] = (
            MemoryRecall(memory, llm) if memory else None
        )

    # ── 同步 ReAct（子 Agent 使用，支持压缩）────────────────────

    def run(self, session_id: str | None, input_text: str, **kwargs) -> str:
        try:
            self._emit("on_agent_start", self.name, input_text)
            messages, loaded_seqs = self._build_messages(session_id, input_text)
            tools_schema = self.tool_registry.get_schemas() or None
            final_text: str | None = None

            tracker = TokenTracker(cfg.LLM_CONTEXT_WINDOW, cfg.LLM_RESERVED_OUTPUT_TOKENS)
            compressor = ContextCompressor(self.llm)
            compressed_this_turn = False

            for _ in range(self.max_iterations):
                messages, tracker, did_compress = self._maybe_compress(
                    messages, tracker, compressor, 0.0
                )
                if did_compress:
                    compressed_this_turn = True
                self._emit("on_llm_start", messages)
                try:
                    response = self.llm.invoke(
                        messages=self._strip_internal(messages),
                        tools=tools_schema, **kwargs,
                    )
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

            self.last_messages = messages
            if session_id is not None and self.session_manager:
                turn_messages = self._extract_turn_messages(messages, loaded_seqs, input_text, final_text)
                self._commit_turn(
                    session_id, messages, turn_messages,
                    compressor, tracker, None, input_text,
                    compressed_this_turn, loaded_seqs,
                )
            self._emit("on_agent_end", self.name, final_text)
            return final_text

        except Exception as e:
            self._emit("on_agent_error", self.name, e)
            raise

    # ── 同步流式 ReAct（CLI 使用）────────────────────────────────

    def stream_run(
        self, session_id: str | None, input_text: str, **kwargs
    ) -> Iterator[str]:
        try:
            self._emit("on_agent_start", self.name, input_text)
            messages, loaded_seqs = self._build_messages(session_id, input_text)
            tools_schema = self.tool_registry.get_schemas() or None
            final_text: str | None = None
            compressed_this_turn = False

            tracker = TokenTracker(cfg.LLM_CONTEXT_WINDOW, cfg.LLM_RESERVED_OUTPUT_TOKENS)
            compressor = ContextCompressor(self.llm)

            for _ in range(self.max_iterations):
                messages, tracker, did_compress = self._maybe_compress(
                    messages, tracker, compressor, 0.0
                )
                if did_compress:
                    compressed_this_turn = True
                self._emit("on_llm_start", messages)
                text_chunks: list[str] = []
                tool_calls: list[dict] = []

                try:
                    for event in self.llm.stream_with_tools(
                        messages=self._strip_internal(messages),
                        tools=tools_schema, **kwargs,
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

            self.last_messages = messages
            if session_id is not None and self.session_manager:
                turn_messages = self._extract_turn_messages(messages, loaded_seqs, input_text, final_text)
                self._commit_turn(
                    session_id, messages, turn_messages,
                    compressor, tracker, None, input_text,
                    compressed_this_turn, loaded_seqs,
                )
            self._emit("on_agent_end", self.name, final_text)

        except Exception as e:
            self._emit("on_agent_error", self.name, e)
            raise

    # ── 异步流式 ReAct（生产路径，含所有集成层）─────────────────

    async def async_stream_run(
        self, session_id: str | None, input_text: str,
        workspace_id: str | None = None, **kwargs
    ) -> AsyncIterator[str]:
        """带事件广播的异步流式 ReAct 循环。"""
        effective_workspace_id, cwd_path = self._resolve_workspace(session_id, workspace_id)

        cwd_token     = bash_cwd.set(cwd_path)
        budget_token  = set_root_budget(cfg.SUBAGENT_MAX_DEPTH, cfg.SUBAGENT_MAX_TOTAL)
        session_token = set_root_session(session_id)
        try:
            await self.event_bus.emit(AGENT_START, {"session_id": session_id, "input": input_text})

            messages, loaded_seqs = self._build_messages(session_id, input_text)
            await self._inject_memory_recall(messages, input_text)

            tools_schema = self.tool_registry.get_schemas() or None
            final_text: str | None = None
            turn_messages: list[dict] = [{"role": "user", "content": input_text}]
            compressed_this_turn = False

            tracker = TokenTracker(cfg.LLM_CONTEXT_WINDOW, cfg.LLM_RESERVED_OUTPUT_TOKENS)
            compressor = ContextCompressor(self.llm)
            cooldown_until = 0.0
            ineffective_count = 0

            # 14.7 — 从持久化状态种入压缩校准基线
            if session_id and self.session_manager:
                state = self.session_manager.get_session_state(session_id)
                if state.get("compressed_once"):
                    compressor._compressed_once = True
                if state.get("last_prompt_tokens") is not None:
                    tracker._last_prompt_tokens = state["last_prompt_tokens"]

            try:
                for _ in range(self.max_iterations):
                    messages, tracker, did_compress = self._maybe_compress(
                        messages, tracker, compressor, cooldown_until
                    )
                    if did_compress:
                        compressed_this_turn = True

                    await self.event_bus.emit(AGENT_THINKING, {"session_id": session_id})

                    text_chunks: list[str] = []
                    tool_calls: list[dict] = []

                    try:
                        async for event in self._stream_with_tools_async(
                            self._strip_internal(messages), tools_schema, **kwargs
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
                                await self.event_bus.emit(AGENT_CONTEXT_USAGE, {
                                    "session_id":   session_id,
                                    "used_tokens":  tracker.used_tokens(messages),
                                    "total_tokens": tracker._effective,
                                    "ratio":        min(tracker.ratio(messages), 1.0),
                                })
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
                        compressor, tracker, effective_workspace_id, input_text,
                        compressed_this_turn, loaded_seqs,
                    )

                await self.event_bus.emit(AGENT_DONE, {"session_id": session_id, "text": final_text})

                if self.memory is not None:
                    asyncio.create_task(self._extract_memories_bg(turn_messages))

            except Exception as e:
                await self.event_bus.emit(AGENT_ERROR, {"session_id": session_id, "error": str(e)})
                raise

        finally:
            bash_cwd.reset(cwd_token)
            reset_root_budget(budget_token)
            reset_root_session(session_token)

    # ── 会话 / 工作区准备 ─────────────────────────────────────────

    def _resolve_workspace(
        self, session_id: str | None, workspace_id: str | None
    ) -> tuple[str | None, str]:
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

    # ── 消息构建 ──────────────────────────────────────────────────

    def _build_messages(
        self, session_id: str | None, input_text: str
    ) -> tuple[list[dict], list]:
        """返回 (messages, loaded_seqs)。

        历史消息上打瞬态 _seq 标记，供 _commit_turn 对账压缩范围。
        session_id=None（子 Agent）时历史为空，loaded_seqs=[]。
        """
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        loaded_seqs: list = []
        if session_id and self.session_manager:
            for row in self.session_manager.load_messages(session_id):
                seq = row["seq"]
                msg: dict = {"role": row["role"], "content": row.get("content"), "_seq": seq}
                if row.get("tool_calls"):
                    msg["tool_calls"] = row["tool_calls"]
                if row.get("tool_call_id"):
                    msg["tool_call_id"] = row["tool_call_id"]
                messages.append(msg)
                loaded_seqs.append(seq)

        messages.append({"role": "user", "content": input_text})
        return messages, loaded_seqs

    @staticmethod
    def _strip_internal(messages: list[dict]) -> list[dict]:
        """剥离 _seq / _compressed_summary 等内部键，返回浅拷贝列表。

        必须在每次 LLM 调用前执行，防止内部标记泄漏给 API。
        """
        result = []
        for m in messages:
            if any(k in m for k in _INTERNAL_KEYS):
                m = {k: v for k, v in m.items() if k not in _INTERNAL_KEYS}
            result.append(m)
        return result

    @staticmethod
    def _extract_turn_messages(
        messages: list[dict],
        loaded_seqs: list,
        input_text: str,
        final_text: str,
    ) -> list[dict]:
        """从完整工作集中提取本轮新增消息（无 _seq 标记的部分）。"""
        # 跳过最后的 user 输入消息（由调用方负责加入）
        new_msgs = [m for m in messages if "_seq" not in m and m.get("role") != "system"]
        # 去掉尾部的 final_text assistant 消息（调用方会单独追加）
        # 其实我们直接返回全部新消息（含 user+tool+assistant），外部再加 final assistant
        return new_msgs

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
        tracker: TokenTracker,
        effective_workspace_id: str | None,
        input_text: str,
        compressed_this_turn: bool,
        loaded_seqs: list,
    ) -> None:
        """将本轮结果原子落库。

        有压缩：archive_and_compact（中段标 compacted，摘要用小数 seq，新消息追加）。
        无压缩：append_turn（普通追加）。
        """
        if not self.session_manager:
            return

        if compressed_this_turn:
            # ── 压缩路径 ──────────────────────────────────────────
            # 仍在工作集里的历史 seq
            kept_seqs = {m["_seq"] for m in messages if "_seq" in m}
            # 被压缩掉的历史 seq = 已加载 - 仍保留
            compacted_seqs = set(loaded_seqs) - kept_seqs

            # 找出摘要行（有 _SUMMARY_MARKER、无 _seq）并计算小数 seq
            summary_rows: list[dict] = []
            for i, m in enumerate(messages):
                if not m.get(_SUMMARY_MARKER) or "_seq" in m:
                    continue
                # 找相邻的 _seq 来计算插入位置
                prev_seq = self._find_prev_seq(messages, i)
                next_seq = self._find_next_seq(messages, i)
                mid_seq = (prev_seq + next_seq) / 2.0 if prev_seq is not None and next_seq is not None \
                    else (next_seq - 0.5 if next_seq is not None else (prev_seq + 0.5 if prev_seq is not None else 0.5))
                summary_rows.append({
                    "seq":  mid_seq,
                    "role": m["role"],
                    "content": m.get("content"),
                })

            # 本轮新消息直接用 turn_messages（已含 final_text assistant 消息）
            # 不能从 messages 派生——messages 的 ReAct 循环在 final_text 确定后 break，
            # 不会把最终 assistant 消息 append 进去，从 messages 派生会丢这条。
            new_active = [m for m in turn_messages if not m.get(_SUMMARY_MARKER)]

            if not self.session_manager.session_exists(session_id):
                self.session_manager.create_session(
                    title=self.session_manager.generate_title(input_text),
                    session_id=session_id, workspace_id=effective_workspace_id,
                )
            self.session_manager.archive_and_compact(
                session_id, compacted_seqs, summary_rows, new_active
            )
        else:
            # ── 普通追加路径 ──────────────────────────────────────
            self.session_manager.append_turn(
                session_id, turn_messages, input_text,
                workspace_id=effective_workspace_id,
            )

        # 14.7 — 持久化压缩状态
        # last_prompt_tokens 只在有实际值时更新：Bedrock 代理可能返回 0，
        # 写入 0 会导致下轮误判上下文占用为 0，保留旧值更安全。
        state_fields: dict = {"compressed_once": compressor._compressed_once}
        if tracker._last_prompt_tokens:
            state_fields["last_prompt_tokens"] = tracker._last_prompt_tokens
        self.session_manager.save_session_state(session_id, **state_fields)

    @staticmethod
    def _find_prev_seq(messages: list[dict], idx: int) -> float | None:
        """在 messages[idx] 之前找最近一个有 _seq 的消息。"""
        for i in range(idx - 1, -1, -1):
            if "_seq" in messages[i]:
                return messages[i]["_seq"]
        return None

    @staticmethod
    def _find_next_seq(messages: list[dict], idx: int) -> float | None:
        """在 messages[idx] 之后找最近一个有 _seq 的消息。"""
        for i in range(idx + 1, len(messages)):
            if "_seq" in messages[i]:
                return messages[i]["_seq"]
        return None

    # ── 后台任务 ──────────────────────────────────────────────────

    async def _extract_memories_bg(self, turn_messages: list[dict]) -> None:
        try:
            await asyncio.to_thread(
                extract_memories, turn_messages, self.llm, self.memory
            )
        except Exception as e:
            print(f"[memory] background extraction error: {e}", flush=True)

    async def _stream_with_tools_async(self, messages, tools, **kwargs):
        async for event in self.llm.async_stream_with_tools(
            messages=messages, tools=tools, **kwargs
        ):
            yield event
