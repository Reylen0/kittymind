"""子 Agent 委派预算：追踪委派深度与总数，防止递归爆炸。

使用方式（根 Agent）:
    budget_token  = set_root_budget(cfg.SUBAGENT_MAX_DEPTH, cfg.SUBAGENT_MAX_TOTAL)
    session_token = set_root_session(session_id)
    try:
        ...
    finally:
        reset_root_budget(budget_token)
        reset_root_session(session_token)

使用方式（TaskTool.execute）:
    budget = current_budget()
    if not can_delegate(budget):
        return "委派已达上限..."
    try:
        with child_scope(budget) as active:
            can_recurse = active.depth < active.max_depth
            ...
    except DelegationLimit as e:
        return str(e)
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from collections.abc import Generator


class DelegationLimit(Exception):
    """委派深度或总数已达上限。"""


@dataclass
class DelegationBudget:
    depth: int = 0
    spawned: int = 0
    max_depth: int = 3
    max_total: int = 8
    _lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, repr=False
    )


_budget: ContextVar[DelegationBudget | None] = ContextVar(
    "_delegation_budget", default=None
)
_root_session_id: ContextVar[str | None] = ContextVar(
    "_root_session_id", default=None
)
_root_usage_recorder: ContextVar[object | None] = ContextVar(
    "_root_usage_recorder", default=None
)


def current_budget() -> DelegationBudget | None:
    return _budget.get()


def set_root_budget(max_depth: int, max_total: int):
    """创建根预算并注入 ContextVar，返回 token 供 finally 中 reset。"""
    budget = DelegationBudget(max_depth=max_depth, max_total=max_total)
    return _budget.set(budget)


def reset_root_budget(token) -> None:
    _budget.reset(token)


def set_root_session(session_id: str | None):
    """注入根 session_id 供子事件打标签，返回 token 供 finally 中 reset。"""
    return _root_session_id.set(session_id)


def reset_root_session(token) -> None:
    _root_session_id.reset(token)


def current_root_session() -> str | None:
    """当前上下文归属的根 session_id（与 current_budget 对称的只读访问）。

    子 Agent 复用父级作用域，所以拿到的始终是父级真实会话。审批事件靠它给
    payload 打 session_id，前端才能把弹窗归属到正确的会话——缺这个字段时，
    多会话并行会把 A 的审批弹在 B 的界面上。
    """
    return _root_session_id.get()


def set_root_usage_recorder(recorder) -> None:
    """注入根 Agent 当前 turn 的用量聚合器，供子 Agent（task_tool）回传合并。"""
    _root_usage_recorder.set(recorder)


def reset_root_usage_recorder(token) -> None:
    _root_usage_recorder.reset(token)


def current_root_usage_recorder():
    """当前上下文里根 Agent 的用量聚合器（子 Agent 用它把 token 回传父级）。"""
    return _root_usage_recorder.get()


def can_delegate(budget: DelegationBudget | None) -> bool:
    """None = CLI 无预算，允许一次委派作为兜底。"""
    if budget is None:
        return True
    with budget._lock:
        return budget.depth < budget.max_depth and budget.spawned < budget.max_total


@contextmanager
def child_scope(
    budget: DelegationBudget | None,
) -> Generator[DelegationBudget, None, None]:
    """进入子 Agent 作用域：depth+1, spawned+1；退出时只 depth-1（spawned 单调累加）。

    若 budget 为 None（CLI 场景），临时创建并注入 ContextVar，退出后还原。
    这样同步嵌套（orchestrator 再委派）也能正确读到深度上下文。
    """
    budget_token = None
    if budget is None:
        budget = DelegationBudget()
        budget_token = _budget.set(budget)

    try:
        with budget._lock:
            if budget.depth >= budget.max_depth or budget.spawned >= budget.max_total:
                raise DelegationLimit(
                    f"委派已达上限（深度 {budget.max_depth} / 总数 {budget.max_total}），"
                    "请直接完成任务或将任务拆得更小。"
                )
            budget.depth += 1
            budget.spawned += 1
        try:
            yield budget
        finally:
            with budget._lock:
                budget.depth -= 1  # spawned 不回退，是全树只增不减的计数
    finally:
        if budget_token is not None:
            _budget.reset(budget_token)
