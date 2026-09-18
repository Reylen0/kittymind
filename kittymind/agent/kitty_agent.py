"""KittyMind 主 Agent。

直接继承 Agent，包含：
  - 工具 ReAct 引擎（同步 run / 异步流式 async_stream_run）
  - 上下文压缩（两条路径均可触发，压缩状态跨轮持久化到 SessionManager）
  - 桌面集成（EventBus / SessionManager / WorkspaceManager / MemoryStore）
  - 委派守护（DelegationBudget 根预算）

子 Agent 调 run(session_id=None)，不触发任何集成层副作用。

ReAct 循环的组织方式：
  - run() 是纯同步方法：子 Agent 委派走 ToolExecutor → 工具线程池 → run()，
    这条链路上没有事件循环，run() 不能依赖 asyncio。
  - async_stream_run() 是独立的异步生成器（需要 await / EventBus 广播），
    与 run() 共用 _begin_turn / _assistant_message / _ensure_final_text 等轮次规则，
    但循环体不共享——**刻意不做二合一**：让 run() 走 async 需要 task_tool.run
    变 async、ToolExecutor 与权限审批整条管线支持异步工具，属架构变更；
    而同步侧只剩一个消费者后，间接层的收益已小于成本（2026-09-18 决策）。

线程模型：
  - 异步路径的工具执行（含审批等待）跑在独立线程池 _get_tool_pool()，
    与 asyncio 默认线程池（压缩摘要 / 记忆召回 / 后台提取）隔离——
    集中弹审批最多占满工具池让后续调用排队，不会冻结其它会话。
  - 经 run_in_executor 进线程池时必须显式 copy_context().run：
    to_thread 会复制 contextvars 而 run_in_executor 不会，权限桥的 ask()
    靠 ContextVar 定位归属连接，漏了复制会让多连接审批退化。
"""

import asyncio
import contextvars
import functools
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..callbacks.base import BaseCallBack
from ..config import cfg
from ..context import ContextCompressor, TokenTracker, prune_tool_outputs
from ..context.compressor import _SUMMARY_MARKER
from ..core.exceptions import AgentException, LLMException
from ..core.llm import BaseAgentLLM, get_aux_llm
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
from ..tools.executor import ToolExecutor, TurnContext
from ..tools.audit import get_tool_audit_log
from ..tools.guardrails import GuardrailController
from ..tools.registry import ToolRegistry
from .base import Agent
from .delegation import (
    reset_root_budget, reset_root_session,
    set_root_budget, set_root_session,
)

logger = logging.getLogger(__name__)

# 剥离时要移除的内部键（不得泄漏给 LLM API）
_INTERNAL_KEYS = ("_seq", _SUMMARY_MARKER)

# 辅助模型哨兵：默认「按配置自动解析」，显式传 None 表示强制回退主模型
_AUX_AUTO = object()

# 记忆召回段缓存：值 _RECALL_STALE 表示「历史前缀已因压缩重写，需用下一轮输入重算」
_RECALL_STALE = object()
_RECALL_CACHE_MAX = 256   # 冻结召回段的会话数上限（LRU，超出淘汰最久未用的）


def _fresh_tracker() -> TokenTracker:
    """新建 TokenTracker：上限与保留额取自配置（调用时求值，cfg.reload() 后生效）。"""
    return TokenTracker(cfg.LLM_CONTEXT_WINDOW, cfg.LLM_RESERVED_OUTPUT_TOKENS)

@dataclass(frozen=True)
class _TurnSetup:
    """每轮开始时的共用运行态（run / async_stream_run 两处同构）。"""
    messages: list[dict]
    loaded_seqs: list
    tools_schema: list[dict] | None
    tracker: TokenTracker
    compressor: ContextCompressor
    ctx: TurnContext


class KittyAgent(Agent):
    """KittyMind 统一 Agent。"""

    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: str | None = None,
        tools: list[BaseTool] | None = None,
        description: str | None = None,
        callbacks: list[BaseCallBack] | None = None,
        max_iterations: int | None = None,
        # 辅助小模型（压缩摘要 / 记忆提取 / 记忆召回筛选）；默认按 LLM_AUX_* 配置自动解析
        aux_llm=_AUX_AUTO,
        # 集成层（子 Agent 不传）
        event_bus: EventBus | None = None,
        session_manager: SessionManager | None = None,
        workspace_manager=None,
        memory: MemoryStore | None = None,
        ask_fn=None,
        # 是否是交互式，决定是否启用工具守护栏block（非交互态或全局 HARD_STOP 配置时启用 block）
        interactive: bool = True,
    ):
        super().__init__(name, llm, system_prompt, description, callbacks)
        # 默认值在调用时求值，避免写成函数默认参数被 import 期钉死（cfg.reload() 会改不动）
        self.max_iterations = cfg.AGENT_MAX_ITERATIONS if max_iterations is None else max_iterations
        self.tools = tools or []
        self.tool_registry = ToolRegistry()
        for tool in self.tools:
            self.tool_registry.register(tool)
        guardrail = GuardrailController(interactive) if cfg.TOOL_GUARDRAIL_ENABLED else None
        audit = get_tool_audit_log() if cfg.TOOL_AUDIT_ENABLED else None
        self.tool_executor = ToolExecutor(
            self.tool_registry, ask_fn=ask_fn, guardrail=guardrail, audit=audit
        )
        self.last_messages: list[dict] = []

        # 辅助小模型：未配置 LLM_AUX_MODEL_ID 时为 None，辅助任务自动回退主模型
        self.aux_llm: BaseAgentLLM | None = (
            get_aux_llm() if aux_llm is _AUX_AUTO else aux_llm
        )

        # 后台任务强引用：asyncio 只持弱引用，create_task 的返回值若不保存，
        # 任务可能在执行途中被 GC 掉（且异常被静默吞掉）。
        self._bg_tasks: set[asyncio.Task] = set()

        # 工具执行专用线程池（懒创建）：同步路径（run()）用不到，不预建。
        # 与 asyncio 默认线程池隔离的原因见模块 docstring「线程模型」。
        self._tool_pool: ThreadPoolExecutor | None = None

        self.event_bus: EventBus = event_bus or EventBus()
        self.session_manager: SessionManager | None = session_manager
        self.workspace_manager = workspace_manager
        self.memory: MemoryStore | None = memory
        self._memory_recall: MemoryRecall | None = (
            MemoryRecall(memory, self.aux_model) if memory else None
        )
        # 冻结的记忆召回段（session_id -> 文本 | _RECALL_STALE）
        self._recall_cache: OrderedDict[str, object] = OrderedDict()

    @property
    def aux_model(self) -> BaseAgentLLM:
        """辅助任务（压缩摘要 / 记忆提取 / 记忆召回筛选）使用的模型。

        优先小模型，未配置则回退主模型。主任务的推理与工具调用不受影响。
        """
        return self.aux_llm or self.llm

    # ── 每轮初始化（两条路径共用）─────────────────────────────────

    def _new_turn(self, session_id: str | None) -> tuple[TokenTracker, ContextCompressor, TurnContext]:
        """每轮开始时的运行态：tracker/compressor 从持久化状态种入（若有），guardrail 状态全新。"""
        tracker = _fresh_tracker()
        compressor = ContextCompressor(self.aux_model)
        if session_id and self.session_manager:
            state = self.session_manager.get_session_state(session_id)
            if state.get("compressed_once"):
                compressor._compressed_once = True
            if state.get("last_prompt_tokens") is not None:
                tracker._last_prompt_tokens = state["last_prompt_tokens"]
        return tracker, compressor, TurnContext(session_id=session_id)

    def _begin_turn(self, session_id: str | None, input_text: str) -> _TurnSetup:
        """每轮共用初始化：上下文 + 工具表 + 运行态。两条路径都从这里出发。

        `session_id` 为空（子 Agent）时历史为空、loaded_seqs=[]；记忆召回由
        async 路径在拿到 messages 之后另行注入。
        """
        messages, loaded_seqs = self._build_messages(session_id, input_text)
        tracker, compressor, ctx = self._new_turn(session_id)
        return _TurnSetup(
            messages=messages,
            loaded_seqs=loaded_seqs,
            tools_schema=self.tool_registry.get_schemas() or None,
            tracker=tracker,
            compressor=compressor,
            ctx=ctx,
        )

    def _resolve_workspace(
        self, session_id: str | None, workspace_id: str | None
    ) -> tuple[str | None, str]:
        """解析本轮工作目录：显式 workspace_id > 会话记录里的 workspace_id > 默认目录。"""
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

    def _get_tool_pool(self) -> ThreadPoolExecutor:
        """工具执行专用线程池（懒创建，尺寸取 TOOL_POOL_MAX_WORKERS）。

        只在异步路径首次执行工具时创建；同步路径（run()/子 Agent）全程不碰。
        """
        if self._tool_pool is None:
            self._tool_pool = ThreadPoolExecutor(
                max_workers=cfg.TOOL_POOL_MAX_WORKERS,
                thread_name_prefix="km-tool",
            )
        return self._tool_pool

    # ── 循环内共用规则 ────────────────────────────────────────────

    @staticmethod
    def _assistant_message(content: str | None, tool_calls: list[dict]) -> dict:
        """本轮 assistant 消息。没有工具调用时 `tool_calls` 写 None（OpenAI 协议如此）。"""
        return {"role": "assistant", "content": content, "tool_calls": tool_calls or None}

    def _ensure_final_text(self, final_text: str | None) -> str:
        """迭代耗尽仍未收尾 → 抛迭代上限异常（两条路径共用同一文案）。"""
        if final_text is None:
            raise AgentException(f"超过最大迭代次数 {self.max_iterations}")
        return final_text

    def _execute_tools(self, messages: list[dict], tool_calls: list[dict], ctx: TurnContext) -> None:
        """同步执行一批工具调用并把结果回灌 `messages`（run() / 子 Agent 使用）。

        异步路径不用这个：它要把每次调用广播到 EventBus，并把阻塞执行丢进线程池。
        """
        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            self._emit("on_tool_start", name, tool_call)
            result = self.tool_executor.execute(tool_call=tool_call, ctx=ctx)
            self._emit("on_tool_end", name, result)
            messages.append(result)

    # ── 同步 ReAct（子 Agent 使用）────────────────────────────────

    def run(self, session_id: str | None, input_text: str, **kwargs) -> str:
        """同步 ReAct（子 Agent 使用，纯内存，支持压缩但不落库）。

        不注入记忆召回、不落库、不起后台提取；压缩冷却固定 0.0（同步路径收不到
        usage 事件，反抖动无从判断）；事件只经 callbacks，不走 EventBus——
        这些差异一律显式保留，见 `_CAPABILITY_MATRIX`。
        """
        try:
            self._emit("on_agent_start", self.name, input_text)
            setup = self._begin_turn(session_id, input_text)
            messages, tracker, compressor = setup.messages, setup.tracker, setup.compressor
            ctx, tools_schema = setup.ctx, setup.tools_schema
            final_text: str | None = None

            for _ in range(self.max_iterations):
                messages, tracker, _ = self._maybe_compress(
                    messages, tracker, compressor, 0.0
                )
                self._emit("on_llm_start", messages)
                try:
                    response = self.llm.invoke(
                        messages=self._strip_internal(messages), tools=tools_schema, **kwargs
                    )
                except Exception as e:
                    self._emit("on_llm_error", e)
                    raise LLMException(f"LLM调用失败: {e}") from e
                self._emit("on_llm_end", response)

                messages.append(self._assistant_message(response.content, response.tool_calls))

                if response.is_tool_call():
                    self._execute_tools(messages, response.tool_calls, ctx)
                    continue

                final_text = response.content
                break

            final_text = self._ensure_final_text(final_text)
            self.last_messages = messages
            self._emit("on_agent_end", self.name, final_text)
            return final_text

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

            setup = self._begin_turn(session_id, input_text)
            messages, loaded_seqs = setup.messages, setup.loaded_seqs
            tools_schema, tracker = setup.tools_schema, setup.tracker
            compressor, ctx = setup.compressor, setup.ctx

            # 记忆召回是 async 路径**独有**的能力，
            # 作为独立 system 消息插在主 system 之后。
            await self._inject_memory_recall(messages, input_text, session_id)

            final_text: str | None = None
            turn_messages: list[dict] = [{"role": "user", "content": input_text}]
            compressed_this_turn = False
            cooldown_until = 0.0
            ineffective_count = 0

            try:
                for _ in range(self.max_iterations):
                    messages, tracker, did_compress = await self._maybe_compress_async(
                        messages, tracker, compressor, cooldown_until
                    )
                    if did_compress:
                        compressed_this_turn = True
                        # 历史前缀已被重写，跨轮缓存反正失效 —— 顺势标记召回段待重算
                        self._mark_recall_stale(session_id)

                    await self.event_bus.emit(AGENT_THINKING, {"session_id": session_id})

                    text_chunks: list[str] = []
                    tool_calls: list[dict] = []

                    try:
                        async for event in self.llm.async_stream_with_tools(
                            messages=self._strip_internal(messages),
                            tools=tools_schema, **kwargs,
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
                        raise LLMException(f"LLM流式调用失败: {e}") from e

                    step_text = "".join(text_chunks)

                    if not tool_calls:
                        final_text = step_text
                        break

                    assistant_msg = self._assistant_message(step_text or None, tool_calls)
                    messages.append(assistant_msg)
                    turn_messages.append(assistant_msg)

                    for tool_call in tool_calls:
                        name = tool_call["function"]["name"]
                        await self.event_bus.emit(AGENT_TOOL_CALL, {
                            "name": name,
                            "args": tool_call["function"].get("arguments"),
                            "session_id": session_id,
                        })
                        # 独立线程池（非默认池）：审批等待可长达 PERMISSION_ASK_TIMEOUT，
                        # 占满默认池会冻结压缩/召回/其它会话。必须 copy_context().run——
                        # run_in_executor 不复制 contextvars，权限桥 ask() 靠它定位连接。
                        run_in_ctx = contextvars.copy_context().run
                        result = await asyncio.get_running_loop().run_in_executor(
                            self._get_tool_pool(),
                            functools.partial(
                                run_in_ctx, self.tool_executor.execute,
                                tool_call=tool_call, ctx=ctx,
                            ),
                        )
                        await self.event_bus.emit(AGENT_TOOL_RESULT, {
                            "name": name,
                            "result": result.get("content"),
                            "session_id": session_id,
                        })
                        messages.append(result)
                        turn_messages.append(result)

                final_text = self._ensure_final_text(final_text)

                # 与 run() 一致：暴露本轮完整消息（不含最终 assistant 答复）。
                self.last_messages = messages

                turn_messages.append({"role": "assistant", "content": final_text})

                if session_id is not None:
                    self._commit_turn(
                        session_id, messages, turn_messages,
                        compressor, tracker, effective_workspace_id, input_text,
                        compressed_this_turn, loaded_seqs,
                    )

                await self.event_bus.emit(AGENT_DONE, {"session_id": session_id, "text": final_text})

                if self.memory is not None:
                    task = asyncio.create_task(self._extract_memories_bg(turn_messages))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)

            except Exception as e:
                await self.event_bus.emit(AGENT_ERROR, {"session_id": session_id, "error": str(e)})
                raise

        finally:
            bash_cwd.reset(cwd_token)
            reset_root_budget(budget_token)
            reset_root_session(session_token)

    # ── 记忆召回（async 路径独有的能力）────

    async def _inject_memory_recall(
        self, messages: list[dict], input_text: str, session_id: str | None
    ) -> None:
        """注入记忆召回段 —— 独立 system 消息，插在主 system 之后。

        缓存关键设计：
          - messages[0]（主 system prompt）跨轮字节级稳定，不做任何改写；
          - 召回内容按会话生命周期冻结（跨轮不变）：若每轮都把新召回文本并入
            system，system 内容逐轮变化会破坏提示缓存的前缀匹配，长会话成本
            近似翻倍；
          - 仅在会话首次出现、或压缩之后（历史前缀反正已重写，缓存必失效）
            用当轮输入重算，重算时机零额外缓存损失。
        """
        if not self._memory_recall:
            return
        section = await self._recall_section(session_id, input_text)
        if not section or not messages:
            return
        insert_at = 1 if messages[0].get("role") == "system" else 0
        messages.insert(insert_at, {"role": "system", "content": section})

    async def _recall_section(self, session_id: str | None, input_text: str) -> str:
        """返回本会话冻结的召回段；未命中（首次/被淘汰/压缩后）才重算。"""
        if not session_id:
            # 无会话（无从跨轮）：现算现用，不缓存
            return await self._compute_recall_section(input_text)
        cached = self._recall_cache.get(session_id)
        if cached is not None and cached is not _RECALL_STALE:
            self._recall_cache.move_to_end(session_id)
            return cached
        section = await self._compute_recall_section(input_text)
        self._recall_cache[session_id] = section  # 空串也缓存，避免每轮重算
        self._recall_cache.move_to_end(session_id)
        while len(self._recall_cache) > _RECALL_CACHE_MAX:
            self._recall_cache.popitem(last=False)
        return section

    async def _compute_recall_section(self, input_text: str) -> str:
        try:
            relevant = await asyncio.to_thread(
                self._memory_recall.select_relevant, input_text
            )
            return self._memory_recall.build_recall_section(relevant) or ""
        except Exception:
            return ""  # 召回是增强能力，失败静默降级

    def _mark_recall_stale(self, session_id: str | None) -> None:
        """压缩后调用：下一轮用当轮输入重算召回段。"""
        if session_id:
            self._recall_cache[session_id] = _RECALL_STALE

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
        for msg in messages:
            # 含内部键才重建字典（浅拷贝），其余原样透传，避免无谓的 dict 复制
            cleaned = (
                {k: v for k, v in msg.items() if k not in _INTERNAL_KEYS}
                if any(k in msg for k in _INTERNAL_KEYS)
                else msg
            )
            result.append(cleaned)
        return result

    # ── 压缩 ──────────────────────────────────────────────────────

    def _maybe_compress(
        self,
        messages: list[dict],
        tracker: TokenTracker,
        compressor: ContextCompressor,
        cooldown_until: float,
    ) -> tuple[list[dict], TokenTracker, bool]:
        """同步压缩（子 Agent 路径）。返回 (messages, tracker, did_compress)。"""
        messages, ratio = self._compression_trigger(messages, tracker, cooldown_until)
        if ratio is None:
            return messages, tracker, False
        return self._compress_result(messages, tracker, ratio, compressor.compress(messages, tracker))

    async def _maybe_compress_async(
        self,
        messages: list[dict],
        tracker: TokenTracker,
        compressor: ContextCompressor,
        cooldown_until: float,
    ) -> tuple[list[dict], TokenTracker, bool]:
        """异步路径的压缩。

        compress() 内部要调 LLM 生成摘要（同步阻塞调用），直接在此调用会卡死
        整个事件循环——期间其它会话的事件推送、心跳、新请求全部冻结。故仅把
        摘要这一步丢到线程里执行，修剪与阈值判断仍在循环内（纯 CPU、开销小）。
        """
        messages, ratio = self._compression_trigger(messages, tracker, cooldown_until)
        if ratio is None:
            return messages, tracker, False
        compressed = await asyncio.to_thread(compressor.compress, messages, tracker)
        return self._compress_result(messages, tracker, ratio, compressed)

    def _compress_result(
        self,
        messages: list[dict],
        tracker: TokenTracker,
        ratio: float,
        compressed: list[dict],
    ) -> tuple[list[dict], TokenTracker, bool]:
        """两条压缩路径的共同收尾：无变化原样返回；有变化记日志、换新 tracker。"""
        if compressed is messages:
            return messages, tracker, False
        logger.info("压缩 %d → %d 条消息（占用比 %.2f）", len(messages), len(compressed), ratio)
        return compressed, _fresh_tracker(), True

    @staticmethod
    def _compression_trigger(
        messages: list[dict],
        tracker: TokenTracker,
        cooldown_until: float,
    ) -> tuple[list[dict], float | None]:
        """微压缩修剪 + 阈值/冷却判断。

        返回 (修剪后的 messages, ratio)；ratio 为 None 表示本轮无需压缩。
        """
        messages = prune_tool_outputs(messages)
        ratio = tracker.ratio(messages)
        if ratio < cfg.COMPRESS_THRESHOLD_RATIO or time.monotonic() < cooldown_until:
            return messages, None
        return messages, ratio

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
                logger.info("压缩反抖动：冷却 %.0fs（连续两次压缩未降占用）", cfg.COMPRESS_COOLDOWN_SECONDS)
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
                summary_rows.append({
                    "seq":  self._midpoint_seq(prev_seq, next_seq),
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

        # 持久化压缩状态
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

    @staticmethod
    def _midpoint_seq(prev_seq: float | None, next_seq: float | None) -> float:
        """摘要行插入序号：夹在相邻两条 _seq 之间取中点。

        缺一侧时向存在的一侧偏移 0.5；两侧皆无（异常）落在开头 0.5。
        """
        if prev_seq is not None and next_seq is not None:
            return (prev_seq + next_seq) / 2.0
        if next_seq is not None:
            return next_seq - 0.5
        if prev_seq is not None:
            return prev_seq + 0.5
        return 0.5

    # ── 后台任务 ──────────────────────────────────────────────────

    async def _extract_memories_bg(self, turn_messages: list[dict]) -> None:
        try:
            await asyncio.to_thread(
                extract_memories, turn_messages, self.aux_model, self.memory
            )
        except Exception as e:
            logger.warning("后台记忆提取失败: %s", e)
