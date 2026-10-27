"""KittyMind 主 Agent。

直接继承 Agent，包含：
  - 工具 ReAct 引擎（`run_conversation` 为唯一入口：解析工作目录、组装本轮运行态、
    管理根作用域生命周期；循环本体委派给 `async_stream_run`）
  - 上下文压缩（压缩状态跨轮持久化到 SessionManager）
  - 桌面集成（EventBus / SessionManager / WorkspaceManager / MemoryStore）
  - 委派守护（DelegationBudget 根预算）

子 Agent 调 `run_conversation(session_id=None, root=False)`：`session_id=None`
跳过落库/记忆召回（这两者本就要求真实会话），`root=False` 复用父级已建立的
根作用域（cwd / 委派预算 / 根 session 标签），不重新解析或重置。

全程无跨线程调度：工具体（真阻塞）经 `ToolExecutor.execute` 内部的
`asyncio.to_thread` 丢线程；权限审批（等人点按钮）直接 `await`，不占用线程。
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..config import cfg
from ..context import ContextCompressor, TokenTracker, prune_tool_outputs
from ..context.compressor import _SUMMARY_MARKER
from ..core.exceptions import AgentException, BaseAgentException, LLMException
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
from ..workspace.manager import WorkspaceManager
from ..tools.base import BaseTool
from ..usage import UsageRecorder
from ..tools.builtin.bash_tool import bash_cwd
from ..tools.executor import ToolExecutor
from ..tools.audit import get_tool_audit_log
from ..tools.guardrails import GuardrailController, GuardrailTurnState
from ..tools.registry import ToolRegistry
from .base import Agent
from .delegation import (
    reset_root_budget, reset_root_session,
    reset_root_usage_recorder,
    set_root_budget, set_root_session,
    set_root_usage_recorder,
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

@dataclass
class TurnContext:
    """一轮 ReAct 循环的完整运行态，`run_conversation` 统一组装，循环体与收尾段
    （在 `async_stream_run` 里）原地读写。

    生命周期等同一次 `run_conversation` 调用，轮结束即弃，不需要任何跨轮隔离。
    不是 frozen——`messages`/`tracker` 会在压缩后被整体替换，`final_text` 等
    字段在循环推进中逐步写入。
    """
    messages: list[dict]
    loaded_seqs: list
    tools_schema: list[dict] | None
    tracker: TokenTracker
    compressor: ContextCompressor
    session_id: str | None
    effective_workspace_id: str | None
    turn_messages: list[dict]
    final_text: str | None = None
    compressed_this_turn: bool = False
    cooldown_until: float = 0.0
    ineffective_count: int = 0
    guardrail_state: GuardrailTurnState = field(default_factory=GuardrailTurnState)
    # usage 事件多次来，这里累加后随 _commit_turn 落库；子 Agent（session_id=None）
    # 同样持有一个，其聚合结果经委派回传父级合并。
    recorder: UsageRecorder = field(default_factory=UsageRecorder)


@dataclass
class RunResult:
    """`KittyAgent.run()` 的返回值：本轮最终文本 + 回灌消息 + 用量聚合器。

    这三者是一轮内的瞬时产出，不挂在 agent 实例上——`run()` 内部把它们传给
    `run_conversation` 就地填充，跑完直接整体返回给调用方（如 task_tool）。
    """
    text: str = ""
    messages: list[dict] = field(default_factory=list)
    usage_recorder: UsageRecorder | None = None


class KittyAgent(Agent):
    """KittyMind 统一 Agent。"""

    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: str | None = None,
        tools: list[BaseTool] | None = None,
        description: str | None = None,
        max_iterations: int | None = None,
        # 辅助小模型（压缩摘要 / 记忆提取 / 记忆召回筛选）；默认按 LLM_AUX_* 配置自动解析
        aux_llm=_AUX_AUTO,
        # 集成层（子 Agent 不传）
        event_bus: EventBus | None = None,
        session_manager: SessionManager | None = None,
        workspace_manager: WorkspaceManager | None = None,
        memory: MemoryStore | None = None,
        ask_fn=None,
        # 是否是交互式，决定是否启用工具守护栏block（非交互态或全局 HARD_STOP 配置时启用 block）
        interactive: bool = True,
    ):
        super().__init__(name, llm, system_prompt, description)
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

        # 辅助小模型：未配置 LLM_AUX_MODEL_ID 时为 None，辅助任务自动回退主模型
        self.aux_llm: BaseAgentLLM | None = (
            get_aux_llm() if aux_llm is _AUX_AUTO else aux_llm
        )

        # 后台任务强引用：asyncio 只持弱引用，create_task 的返回值若不保存，
        # 任务可能在执行途中被 GC 掉（且异常被静默吞掉）。
        self._bg_tasks: set[asyncio.Task] = set()

        self.event_bus: EventBus = event_bus or EventBus()
        self.session_manager: SessionManager | None = session_manager
        self.workspace_manager: WorkspaceManager | None = workspace_manager
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

    # ── 循环内共用规则 ────────────────────────────────────────────

    @staticmethod
    def _assistant_message(content: str | None, tool_calls: list[dict]) -> dict:
        """本轮 assistant 消息。没有工具调用时 `tool_calls` 写 None（OpenAI 协议如此）。"""
        return {"role": "assistant", "content": content, "tool_calls": tool_calls or None}

    def _ensure_final_text(self, final_text: str | None) -> str:
        """迭代耗尽仍未收尾 → 抛迭代上限异常。"""
        if final_text is None:
            raise AgentException(f"超过最大迭代次数 {self.max_iterations}")
        return final_text

    # ── 异步流式 ReAct（唯一入口，含所有集成层）─────────────────

    async def run_conversation(
        self, session_id: str | None, input_text: str,
        workspace_id: str | None = None, root: bool = True,
        _result: RunResult | None = None, **kwargs,
    ) -> AsyncIterator[str]:
        """解析工作目录、组装本轮运行态（`TurnContext`）、管理根作用域生命周期；
        循环本体委派给 `async_stream_run`。

        root=False：子 Agent 复用父级已建立的根作用域——不重新设置 cwd（沿用父级
        工作目录）、不重置委派预算（`child_scope` 已经递增过深度，重置等于让递归
        防爆栏归零）、不覆盖根 session 标签（子事件仍打父级真实 session_id）。

        `_result`：仅供 `run()` 内部传入，用于回填本轮 messages / usage_recorder——
        这俩是一轮内的瞬时状态，不挂在 agent 实例上，外部调用方不需要关心此参数。
        """
        # ── 解析本轮工作目录：显式 workspace_id > 会话记录里的 workspace_id > 默认目录 ──
        effective_workspace_id = workspace_id
        if not effective_workspace_id and self.session_manager and session_id:
            header = self.session_manager.get_session_header(session_id)
            if header:
                effective_workspace_id = header.get("workspace_id")
        cwd_path = str(cfg.DEFAULT_WORKSPACE_DIR)
        if effective_workspace_id and self.workspace_manager:
            ws = self.workspace_manager.get_workspace(effective_workspace_id)
            if ws and ws.get("path"):
                cwd_path = ws["path"]

        # ── 组装本轮运行态：session_id 为空（子 Agent）时历史为空、loaded_seqs=[]；
        # 记忆召回由 async_stream_run 在拿到 messages 之后另行注入。
        # tracker/compressor 从持久化状态种入（若有），guardrail 状态全新。
        messages, loaded_seqs = self._build_messages(session_id, input_text)
        tracker = _fresh_tracker()
        compressor = ContextCompressor(self.aux_model)
        if session_id and self.session_manager:
            state = self.session_manager.get_session_state(session_id)
            if state.get("compressed_once"):
                compressor._compressed_once = True
            if state.get("last_prompt_tokens") is not None:
                tracker._last_prompt_tokens = state["last_prompt_tokens"]
        turn = TurnContext(
            messages=messages,
            loaded_seqs=loaded_seqs,
            tools_schema=self.tool_registry.get_schemas() or None,
            tracker=tracker,
            compressor=compressor,
            session_id=session_id,
            effective_workspace_id=effective_workspace_id,
            turn_messages=[{"role": "user", "content": input_text}],
        )

        cwd_token = budget_token = session_token = usage_token = None
        if root:
            cwd_token     = bash_cwd.set(cwd_path)
            budget_token  = set_root_budget(cfg.SUBAGENT_MAX_DEPTH, cfg.SUBAGENT_MAX_TOTAL)
            session_token = set_root_session(session_id)
            usage_token   = set_root_usage_recorder(turn.recorder)
        try:
            async for chunk in self.async_stream_run(
                turn, session_id, input_text, _result=_result, **kwargs
            ):
                yield chunk
        finally:
            if root:
                bash_cwd.reset(cwd_token)
                reset_root_budget(budget_token)
                reset_root_session(session_token)
                reset_root_usage_recorder(usage_token)

    async def async_stream_run(
        self, turn: TurnContext, session_id: str | None, input_text: str,
        _result: RunResult | None = None, **kwargs,
    ) -> AsyncIterator[str]:
        """给定已组装好的本轮运行态 `turn`，驱动 ReAct 循环直到收尾。

        由 `run_conversation` 负责解析工作目录、组装 `turn`、管理根作用域生命周期后
        调用，不单独对外暴露。

        `_result`：仅供 `run()` 内部传入，用于回填本轮 messages / usage_recorder——
        这俩是一轮内的瞬时状态，不挂在 agent 实例上，外部调用方不需要关心此参数。
        """
        await self.event_bus.emit(AGENT_START, {"session_id": session_id, "input": input_text})

        # 记忆召回作为独立 system 消息插在主 system 之后。
        await self._inject_memory_recall(turn.messages, input_text, session_id)

        try:
            for _ in range(self.max_iterations):
                turn.messages, turn.tracker, did_compress = await self._maybe_compress_async(
                    turn.messages, turn.tracker, turn.compressor, turn.cooldown_until
                )
                if did_compress:
                    turn.compressed_this_turn = True
                    # 历史前缀已被重写，跨轮缓存反正失效 —— 顺势标记召回段待重算
                    self._mark_recall_stale(session_id)

                await self.event_bus.emit(AGENT_THINKING, {"session_id": session_id})

                text_chunks: list[str] = []
                tool_calls: list[dict] = []

                try:
                    async for event in self.llm.async_stream_with_tools(
                        messages=self._strip_internal(turn.messages),
                        tools=turn.tools_schema, **kwargs,
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
                            turn.tracker.update_from_usage(event.usage)
                            turn.recorder.record(event.usage, self.llm.model)
                            await self.event_bus.emit(AGENT_CONTEXT_USAGE, {
                                "session_id":   session_id,
                                "used_tokens":  turn.tracker.used_tokens(turn.messages),
                                "total_tokens": turn.tracker._effective,
                                "ratio":        min(turn.tracker.ratio(turn.messages), 1.0),
                            })
                            if did_compress:
                                turn.cooldown_until, turn.ineffective_count = self._check_anti_thrash(
                                    event.usage, turn.tracker, turn.cooldown_until, turn.ineffective_count
                                )
                                did_compress = False
                except BaseAgentException:
                    raise  # llm.py 已经包过一次，不再重复包装前缀
                except Exception as e:
                    raise LLMException(f"LLM流式调用失败: {e}") from e

                step_text = "".join(text_chunks)

                if not tool_calls:
                    turn.final_text = step_text
                    break

                assistant_msg = self._assistant_message(step_text or None, tool_calls)
                turn.messages.append(assistant_msg)
                turn.turn_messages.append(assistant_msg)

                # 同批工具调用：分区并行执行（executor 内部决策串行、只读区并行、
                # 按声明序回灌）。事件按声明序：先推全部 tool_call，执行完再按序推
                # tool_result——前端工具卡片按模型声明顺序出现；同进程内的轻量
                # 探针（如 task_tool 统计子 Agent 工具数）也订阅这些事件。
                for tool_call in tool_calls:
                    name = tool_call["function"]["name"]
                    await self.event_bus.emit(AGENT_TOOL_CALL, {
                        "name": name,
                        "args": tool_call["function"].get("arguments"),
                        "session_id": session_id,
                    })
                results = await self.tool_executor.execute_batch(
                    tool_calls, turn.session_id, turn.guardrail_state
                )
                for tool_call, result in zip(tool_calls, results, strict=True):
                    name = tool_call["function"]["name"]
                    await self.event_bus.emit(AGENT_TOOL_RESULT, {
                        "name": name,
                        "result": result.get("content"),
                        "session_id": session_id,
                    })
                    turn.messages.append(result)
                    turn.turn_messages.append(result)

            turn.final_text = self._ensure_final_text(turn.final_text)

            if _result is not None:
                _result.messages = turn.messages + [self._assistant_message(turn.final_text, [])]
                _result.usage_recorder = turn.recorder

            turn.turn_messages.append({"role": "assistant", "content": turn.final_text})

            if session_id is not None:
                self._commit_turn(
                    session_id, turn.messages, turn.turn_messages,
                    turn.compressor, turn.tracker, turn.effective_workspace_id, input_text,
                    turn.compressed_this_turn, turn.loaded_seqs, turn.recorder,
                )

            await self.event_bus.emit(AGENT_DONE, {"session_id": session_id, "text": turn.final_text})

            if self.memory is not None:
                task = asyncio.create_task(self._extract_memories_bg(turn.turn_messages))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

        except Exception as e:
            await self.event_bus.emit(AGENT_ERROR, {"session_id": session_id, "error": str(e)})
            raise

    async def run(
        self, session_id: str | None, input_text: str,
        workspace_id: str | None = None, root: bool = True, **kwargs,
    ) -> RunResult:
        """驱动 `run_conversation` 跑到完成，一次性返回文本 + 消息 + 用量聚合器。

        给只要最终结果、不需要逐字消费流式输出的调用方用（如 task_tool 跑子 Agent）。
        """
        result = RunResult()
        chunks = [
            chunk async for chunk in self.run_conversation(
                session_id, input_text, workspace_id=workspace_id, root=root,
                _result=result, **kwargs,
            )
        ]
        result.text = "".join(chunks)
        return result

    # ── 记忆召回 ────

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
            return await self._compute_recall_section(input_text, session_id=None)
        cached = self._recall_cache.get(session_id)
        if cached is not None and cached is not _RECALL_STALE:
            self._recall_cache.move_to_end(session_id)
            return cached
        section = await self._compute_recall_section(input_text, session_id=session_id)
        self._recall_cache[session_id] = section  # 空串也缓存，避免每轮重算
        self._recall_cache.move_to_end(session_id)
        while len(self._recall_cache) > _RECALL_CACHE_MAX:
            self._recall_cache.popitem(last=False)
        return section

    async def _compute_recall_section(self, input_text: str,
                                      session_id: str | None = None) -> str:
        """算召回段。**记忆召回绝不能阻塞一个 turn**，所以这里是超时一等公民。

        向量路首建索引要一次网络往返（实测 12 条 1.7s）、端点抖动、额度耗尽都会
        真实发生。超时就用本地关键词结果先注入，而 `to_thread` 里的向量任务**不会
        被取消**——它继续把索引补进 `memory.db`，下一轮（或压缩后重算时）就
        命中缓存了。代价只是这一轮召回质量略低。

        `session_id` 只用于召回留痕（`memory_recall_log`）：没有它，"这轮注入了
        哪几条"就无从追溯，后面门控与重排的任何调参都无法评估。绑定走可选钩子
        `bind_session`，**不给 `select_relevant` 加参数**——替身签名不匹配会抛
        TypeError 并被当成"召回失败"静默吞掉（见 `MemoryRecall.bind_session`）。
        """
        timeout = cfg.MEMORY_RECALL_TIMEOUT
        bind = getattr(self._memory_recall, "bind_session", None)
        if callable(bind):
            bind(session_id)
        try:
            relevant = await asyncio.wait_for(
                asyncio.to_thread(self._memory_recall.select_relevant, input_text),
                timeout=timeout if timeout and timeout > 0 else None,
            )
        except asyncio.TimeoutError:
            logger.warning("记忆召回超时（%.1fs），本轮降级为关键词召回", timeout)
            try:
                relevant = await asyncio.to_thread(
                    self._memory_recall.select_relevant_keyword, input_text
                )
            except Exception:
                return ""
        except Exception:
            return ""  # 召回是增强能力，失败静默降级
        return self._memory_recall.build_recall_section(relevant) or ""

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
        """压缩结果收尾：无变化原样返回；有变化记日志、换新 tracker。"""
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
        tracker: TokenTracker,
        cooldown_until: float,
        ineffective_count: int,
    ) -> tuple[float, int]:
        pt = usage.get("prompt_tokens", 0)
        # 分母必须与触发压缩的闸门一致（_compression_trigger 用 tracker.ratio，
        # 即 pt / effective_window）。原先用 LLM_CONTEXT_WINDOW（未减保留输出）
        # 当分母，门槛偏高，pt 落在 [eff*0.7, window*0.7) 时占用比例其实仍
        # ≥0.70（压缩没把它压下来），却会被漏判成"有效"而不计失败，连抖两次
        # 也不进冷却。改用 effective_window 才能及时识别压不下去的情况。
        threshold = int(tracker.effective_window * cfg.COMPRESS_THRESHOLD_RATIO)
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
        recorder: UsageRecorder,
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

        # 用量成本落库：一个 turn 内多次 LLM 调用已由 recorder 聚合，
        # 按模型展开写入。子 Agent 的用量也并进本 recorder（见 task_tool 回传）。
        if not recorder.is_empty:
            for row in recorder.rows():
                self.session_manager.record_usage(
                    session_id=session_id,
                    model_id=row["model_id"],
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    n_calls=row["n_calls"],
                    kind="turn",
                )

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
