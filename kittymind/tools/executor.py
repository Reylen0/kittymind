"""工具执行器 — 守护栏前置 → 权限闸门 → 执行 → 脱敏 → 守护栏后置 → 审计。

单一 ToolExecutor 类涵盖全部场景：
  - ask_fn=None：纯路径（无 GUI 时的调用方），跳过用户审批（gate-2/3），保留硬拒绝（gate-1）
  - ask_fn 有值：完整三道权限闸门（gate-3 是 `await ask_fn(...)`，不占用任何线程）
  - guardrail 有值：启用失败循环/无进展检测
  - audit 有值：记录每次调用（脱敏后）到 SQLite + JSONL

工具体本身（`tool.run`）是同步阻塞调用，`execute()` 用 `asyncio.to_thread` 丢进
默认线程池；task 工具是例外（它本身要 `await` 子 Agent），走 `tool.arun`。
"""

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field

from ..config import cfg
from .audit import ToolAuditLog
from .guardrails import GuardrailController, GuardrailTurnState
from .permission import check_permission
from .redaction import redact, redact_args_for_audit
from .registry import ToolRegistry


@dataclass
class TurnContext:
    """一轮工具调用期间的运行态，随调用显式传递；轮结束即弹，不需要任何隔离机制。"""
    session_id: str | None = None
    guardrail_state: GuardrailTurnState = field(default_factory=GuardrailTurnState)


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

    async def execute(self, tool_call: dict, ctx: TurnContext) -> dict:
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
                return _tool_result(tool_call_id, decision.message)

        # ── 2. 权限闸门（硬拒绝始终生效；ask_fn=None 跳过用户审批）──
        reason = await check_permission(name, args, self._ask_fn)
        if reason is not None:
            self._record(ctx.session_id, name, args, "denied", reason, False, t0)
            return _tool_result(tool_call_id, f"Permission denied: {reason}")

        # ── 3. 执行（同步工具体丢进线程池；task 等异步工具直接 await）───
        try:
            tool = self.registry.get(name=name)
            if tool.is_async:
                tool_result = await tool.arun(args)
            else:
                tool_result = await asyncio.to_thread(tool.run, args)
            ok, content = tool_result.ok, str(tool_result.content)
            # 全局统一截断（对所有工具生效，故配置名是 TOOL_ 而非 BASH_）
            if len(content) > cfg.TOOL_MAX_OUTPUT:
                content = content[:cfg.TOOL_MAX_OUTPUT] + f"\n…[输出过长，已截断至 {cfg.TOOL_MAX_OUTPUT} 字符]"
        except Exception as e:
            content = str(e)
            ok = False

        # ── 4. 脱敏（进上下文/守护栏/审计之前统一替换敏感信息）──
        if cfg.TOOL_REDACT_ENABLED:
            content = redact(content)

        # ── 5. 守护栏后置：更新计数，warn 则追加指导文字 ──────
        failed = not ok
        decision_str, audit_reason = "allow", ""
        if self._guardrail is not None:
            decision = self._guardrail.after_call(ctx.guardrail_state, name, args, content, failed)
            if decision.action == "warn" and decision.message:
                content = content + f"\n\n[守护栏警告: {decision.message}]"
                decision_str, audit_reason = "warn", decision.message

        # ── 6. 审计 ──────────────────────────────────────────
        self._record(ctx.session_id, name, args, decision_str, audit_reason, failed, t0)
        return _tool_result(tool_call_id, content)

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


def _tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
