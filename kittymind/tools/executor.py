"""工具执行器 — 守护栏前置 → 权限闸门 → 执行 → 脱敏 → 守护栏后置 → 审计。

单一 ToolExecutor 类涵盖全部场景：
  - ask_fn=None：纯路径（无 GUI 时的调用方），跳过用户审批（gate-2/3），保留硬拒绝（gate-1）
  - ask_fn 有值：完整三道权限闸门（gate-3 是 `await ask_fn(...)`，不占用任何线程）
  - guardrail 有值：启用失败循环/无进展检测
  - audit 有值：记录每次调用（脱敏后）到 SQLite + JSONL

工具体本身（`tool.run`）是同步阻塞调用，`execute()` 用 `asyncio.to_thread` 丢进
默认线程池；task 工具是例外（它本身要 `await` 子 Agent），走 `tool.arun`。
**必须用 `asyncio.to_thread` 而不是裸 `run_in_executor`**：前者复制 contextvars，
`bash_cwd` 等 ContextVar 在工具体内读取，裸换会静默丢 cwd。

一次调用的六段管线拆成三个阶段（execute_batch 分区并行的基础）：
  - decide()   决策段：守护栏 before_call + 三道权限闸门。**串行**——守护栏记账
    是顺序状态机（streak 依赖前序结果），审批也按声明序逐个弹出。
  - _run_body() 执行段：工具体 + 截断 + 脱敏。**可并行**——只依赖 (name, args)。
  - finalize() 收尾段：守护栏 after_call + 审计。**按声明序串行**——记账与
    审计落库的顺序确定性。
"""

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field

from ..config import cfg
from .audit import ToolAuditLog
from .base import BaseTool
from .guardrails import GuardrailController, GuardrailTurnState
from .permission import check_permission
from .redaction import redact, redact_args_for_audit
from .registry import ToolRegistry


@dataclass
class TurnContext:
    """一轮工具调用期间的运行态，随调用显式传递；轮结束即弹，不需要任何隔离机制。"""
    session_id: str | None = None
    guardrail_state: GuardrailTurnState = field(default_factory=GuardrailTurnState)


@dataclass
class _Decision:
    """决策段产物：decide 串行产出，_run_body / finalize 消费。

    blocked 非 None 表示调用在决策段已被拦截（守护栏 block / 权限拒绝），
    该值即最终 tool_result，不再进入执行段。
    """
    tool_call_id: str
    name: str
    args: dict
    t0: float
    tool: BaseTool | None   # 放行时解析出的工具对象；registry 未命中为 None（执行段报错）
    blocked: dict | None = None


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        ask_fn=None,
        guardrail: GuardrailController | None = None,
        audit: ToolAuditLog | None = None,
    ):
        self.registry = registry
        self._ask_fn = ask_fn
        self._guardrail = guardrail
        self._audit = audit

    # ── 决策段（串行）────────────────────────────────────────────

    async def decide(self, tool_call: dict, ctx: TurnContext) -> _Decision:
        t0 = time.monotonic()
        tool_call_id = tool_call.get("id", "")
        function = tool_call.get("function", {})
        name = function.get("name", "")
        try:
            args = json.loads(function.get("arguments", "{}") or "{}")
        except Exception:
            args = {}

        # ── 1. 守护栏前置：循环/无进展 block ──────────────────
        if self._guardrail is not None:
            decision = self._guardrail.before_call(ctx.guardrail_state, name, args)
            if decision.is_block:
                self._record(ctx.session_id, name, args, "block", decision.message, False, t0)
                return _Decision(tool_call_id, name, args, t0, tool=None,
                                 blocked=_tool_result(tool_call_id, decision.message))

        # ── 2. 权限闸门（硬拒绝始终生效；ask_fn=None 跳过用户审批）──
        reason = await check_permission(name, args, self._ask_fn)
        if reason is not None:
            self._record(ctx.session_id, name, args, "denied", reason, False, t0)
            return _Decision(tool_call_id, name, args, t0, tool=None,
                             blocked=_tool_result(tool_call_id, f"Permission denied: {reason}"))

        # 工具对象在决策段解析（分区并行需要它的并发安全标记）；未命中留给执行段报错
        try:
            tool: BaseTool | None = self.registry.get(name=name)
        except Exception:
            tool = None
        return _Decision(tool_call_id, name, args, t0, tool=tool)

    # ── 执行段（可并行）──────────────────────────────────────────

    async def _run_body(self, d: _Decision) -> tuple[bool, str]:
        """执行工具体并截断/脱敏，返回 (ok, content)。只依赖 (name, args)。"""
        try:
            tool = d.tool if d.tool is not None else self.registry.get(name=d.name)
            if tool.is_async:
                tool_result = await tool.arun(d.args)
            else:
                # 必须是 to_thread（复制 contextvars），见模块 docstring
                tool_result = await asyncio.to_thread(tool.run, d.args)
            ok, content = tool_result.ok, str(tool_result.content)
            # 全局统一截断（对所有工具生效，故配置名是 TOOL_ 而非 BASH_）
            if len(content) > cfg.TOOL_MAX_OUTPUT:
                content = content[:cfg.TOOL_MAX_OUTPUT] + f"\n…[输出过长，已截断至 {cfg.TOOL_MAX_OUTPUT} 字符]"
        except Exception as e:
            content = str(e)
            ok = False

        # 脱敏（进上下文/守护栏/审计之前统一替换敏感信息）
        if cfg.TOOL_REDACT_ENABLED:
            content = redact(content)
        return ok, content

    # ── 收尾段（按声明序串行）────────────────────────────────────

    async def finalize(self, d: _Decision, ctx: TurnContext, ok: bool, content: str) -> dict:
        failed = not ok
        decision_str, audit_reason = "allow", ""
        if self._guardrail is not None:
            decision = self._guardrail.after_call(ctx.guardrail_state, d.name, d.args, content, failed)
            if decision.action == "warn" and decision.message:
                content = content + f"\n\n[守护栏警告: {decision.message}]"
                decision_str, audit_reason = "warn", decision.message
        self._record(ctx.session_id, d.name, d.args, decision_str, audit_reason, failed, d.t0)
        return _tool_result(d.tool_call_id, content)

    # ── 组合入口 ─────────────────────────────────────────────────

    async def execute(self, tool_call: dict, ctx: TurnContext) -> dict:
        """单调用完整管线（decide → _run_body → finalize）。"""
        d = await self.decide(tool_call, ctx)
        if d.blocked is not None:
            return d.blocked
        ok, content = await self._run_body(d)
        return await self.finalize(d, ctx, ok, content)

    async def execute_batch(self, tool_calls: list[dict], ctx: TurnContext) -> list[dict]:
        """同批 tool_calls 的分区并行执行，结果按声明序返回。

        分区规则（对标 Claude Code 的「连续同类」模型）：
          - 决策段对整批**按声明序串行**：守护栏记账看得到前序结果，审批逐个弹出；
          - 连续的 `is_concurrency_safe=True` 调用划入同一并行区（asyncio.gather）；
          - 非安全调用、被拦截调用自成一串行屏障——模型声明的顺序天然表达依赖
            （[read, edit, read] 中第二个 read 被edit 隔开，不会与第一个并行）；
          - 未声明安全的工具一律串行（fail-closed，BaseTool 默认 False）；
          - 并行区执行完后按声明序 finalize，再进入下一区——跨区守护栏记账
            顺序与串行执行一致。
        """
        decisions = [await self.decide(tc, ctx) for tc in tool_calls]
        results: list[dict | None] = [None] * len(decisions)
        i = 0
        while i < len(decisions):
            d = decisions[i]
            if d.blocked is not None or not _is_safe(d.tool):
                results[i] = d.blocked if d.blocked is not None \
                    else await self._finish_one(d, ctx)
                i += 1
                continue
            j = i
            while (j < len(decisions)
                   and decisions[j].blocked is None
                   and _is_safe(decisions[j].tool)):
                j += 1
            segment = decisions[i:j]
            outs = await asyncio.gather(*(self._run_body(d) for d in segment))
            for k, (seg_d, (ok, content)) in enumerate(zip(segment, outs, strict=True)):
                results[i + k] = await self.finalize(seg_d, ctx, ok, content)
            i = j
        return results  # type: ignore[return-value]

    async def _finish_one(self, d: _Decision, ctx: TurnContext) -> dict:
        ok, content = await self._run_body(d)
        return await self.finalize(d, ctx, ok, content)

    def _record(
        self, session_id: str | None, name: str, args: dict,
        decision: str, reason: str, failed: bool, t0: float,
    ) -> None:
        if self._audit is None:
            return
        # 审计失败不能影响工具本身的返回；求值与写入一起纳入抑制范围
        with contextlib.suppress(Exception):
            args_blob = (
                redact_args_for_audit(args)
                if cfg.TOOL_REDACT_ENABLED
                else json.dumps(args, ensure_ascii=False, default=str)[:cfg.TOOL_AUDIT_ARGS_MAX_CHARS]
            )
            self._audit.record(
                session_id=session_id,
                tool=name,
                args=args_blob,
                decision=decision,
                reason=reason,
                failed=failed,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )


def _is_safe(tool: BaseTool | None) -> bool:
    """未声明 is_concurrency_safe=True 的工具一律视为不可并行（fail-closed）。"""
    return tool is not None and bool(getattr(tool, "is_concurrency_safe", False))


def _tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
