"""工具执行独立线程池（kitty_agent._get_tool_pool）的回归线。

背景：此前异步路径的工具执行走 `asyncio.to_thread`（默认线程池，min(32, cpu+4)）。
每次权限审批会阻塞一个 worker 线程长达 PERMISSION_ASK_TIMEOUT 秒，多会话集中弹
审批会占满默认池，连带冻结压缩摘要 / 记忆召回 / 后台提取。现在工具执行跑在
独立池（thread_name_prefix="km-tool"），占满只影响后续工具调用。

两个必须锁死的契约：
1. 工具确实跑在独立池（而非默认池）——旧代码上第一条测试会红（旧线程名是
   "asyncio_{n}_{pid}"），这是「真能拦住旧 bug」的回滚探针；
2. contextvars 跨线程传播——`loop.run_in_executor` 不会复制 contextvars
   （`asyncio.to_thread` 会），权限桥的 ask() 靠 ContextVar 定位归属连接。
   实现必须用 `copy_context().run` 显式带上；谁"简化"掉它，第二条会红。
"""

import asyncio
import contextvars
import threading

import pytest

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.config import cfg
from kittymind.core.llm_response import StreamEvent


class _FakeLLM:
    """一轮：先发工具调用，再发纯文本收尾。"""

    def __init__(self):
        self.calls = 0

    def _next(self):
        self.calls += 1
        if self.calls == 1:
            return StreamEvent(
                type="tool_calls_done",
                tool_calls=[{
                    "id": "c1",
                    "function": {"name": "noop", "arguments": "{}"},
                }],
            )
        return StreamEvent(type="text_delta", delta="done")

    async def async_stream_with_tools(self, messages=None, tools=None, **kwargs):
        event = self._next()
        yield event
        # tool_calls_done 是流的最后一个事件（与真实适配器一致），本次生成到此为止；
        # 下一轮循环重新进来才会拿到收尾的 text_delta。


class _FakeExecutor:
    """记录执行线程名 + 执行时刻的 contextvar 值。"""

    def __init__(self):
        self.thread_names: list[str] = []
        self.ctx_values: list[str | None] = []

    def execute(self, tool_call=None, ctx=None):
        self.thread_names.append(threading.current_thread().name)
        self.ctx_values.append(_MARKER.get())
        return {"role": "tool", "tool_call_id": tool_call["id"], "content": "ok"}


_MARKER: contextvars.ContextVar[str | None] = contextvars.ContextVar("km_test_marker", default=None)


def make_agent():
    llm = _FakeLLM()
    agent = KittyAgent(
        name="t", llm=llm, system_prompt="sp", tools=[],
        callbacks=[], aux_llm=None, interactive=False,
    )
    executor = _FakeExecutor()
    agent.tool_executor = executor
    return agent, executor


async def run_turn(agent):
    out = []
    async for chunk in agent.async_stream_run(session_id=None, input_text="hi"):
        out.append(chunk)
    return "".join(out)


@pytest.mark.asyncio
async def test_async_tools_run_in_dedicated_pool():
    """工具必须跑在 km-tool 独立池，而不是 asyncio 默认池。

    旧代码（asyncio.to_thread）上线程名是 "asyncio_{n}_{pid}"，此测试在旧树上会红。
    """
    agent, executor = make_agent()
    assert agent._tool_pool is None  # 懒创建：同步路径 / 未执行工具前不建池

    assert await run_turn(agent) == "done"

    assert executor.thread_names, "工具至少被执行一次"
    for name in executor.thread_names:
        assert name.startswith("km-tool"), f"工具跑在了非专用线程: {name}"
        assert not name.startswith("asyncio"), f"工具占用了 asyncio 默认池: {name}"


@pytest.mark.asyncio
async def test_contextvars_cross_into_tool_thread():
    """ContextVar 必须传播进工具线程——权限桥 ask() 靠它定位归属连接。

    若有人把 copy_context().run "简化"成裸 run_in_executor，此测试会红，
    多连接审批会退化成「单连接猜测 / fail-closed 拒绝」。
    """
    agent, executor = make_agent()
    _MARKER.set("conn-42")

    assert await run_turn(agent) == "done"

    assert executor.ctx_values == ["conn-42"]


@pytest.mark.asyncio
async def test_pool_size_follows_config_at_creation_time():
    """池尺寸在创建时刻取 cfg.TOOL_POOL_MAX_WORKERS（调用时求值，非 import 期钉死）。"""
    agent, _ = make_agent()
    original = cfg.TOOL_POOL_MAX_WORKERS
    cfg.TOOL_POOL_MAX_WORKERS = 3
    try:
        assert await run_turn(agent) == "done"
        assert agent._tool_pool is not None
        assert agent._tool_pool._max_workers == 3
    finally:
        cfg.TOOL_POOL_MAX_WORKERS = original


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_starve_default_pool():
    """4 个会话并发跑工具：全部完成，且没有任何一个工具跑到默认池。

    集中弹审批的真实场景是"多数 worker 线程被等待占用"，这里用并发工具执行
    验证池的调度正确（单会话内工具串行，跨会话并行）。
    """
    agents = [make_agent()[0] for _ in range(4)]
    executors = [a.tool_executor for a in agents]

    results = await asyncio.gather(*[run_turn(a) for a in agents])

    assert results == ["done"] * 4
    for ex in executors:
        assert all(n.startswith("km-tool") for n in ex.thread_names)
