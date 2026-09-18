"""工具体异步卸载（`ToolExecutor.aexecute` 内部 `asyncio.to_thread`）的回归线。

背景：审批等待与工具体执行曾经共用同一次 `run_in_executor` 调用，挤在一个专用
线程池里（`_get_tool_pool`，`thread_name_prefix="km-tool"`）——这是因为旧设计里
"等审批"和"跑工具"被捆在同一个同步 `ToolExecutor.execute()` 里，只能一起丢线程。

现在两者拆开了：闸门 3（等审批）在 `check_permission` 里直接 `await`，零线程；
只有工具体本身（`tool.run`，真阻塞）用 `asyncio.to_thread` 丢线程，走的是默认
线程池，不再需要专用池——审批不占线程之后，"审批集中占满默认池连带冻结其它
会话"这个旧动机本身就不存在了。

本文件直接测 `ToolExecutor.aexecute`（而不是经 KittyAgent 整条循环），锁定两件
仍然成立的契约：
1. 工具体确实在**非事件循环线程**里跑，不阻塞事件循环；
2. contextvars 跨线程传播——`asyncio.to_thread` 会复制 contextvars，权限桥的
   `aask()` 靠 ContextVar 定位归属连接；工具体本身若要读 contextvar
   （比如 `bash_cwd`），同样靠这个传播。
"""

import asyncio
import contextvars
import threading

from pydantic import BaseModel

from kittymind.tools.base import BaseTool, ToolResult
from kittymind.tools.executor import ToolExecutor, TurnContext
from kittymind.tools.registry import ToolRegistry

_MARKER: contextvars.ContextVar[str | None] = contextvars.ContextVar("km_test_marker", default=None)


class _EmptyParams(BaseModel):
    pass


class _RecordingTool(BaseTool):
    """记录执行线程 + 执行时刻的 contextvar 值。"""

    name = "noop"
    description = "记录执行线程与 contextvar"
    param_class = _EmptyParams

    def __init__(self):
        super().__init__()
        self.thread_names: list[str] = []
        self.ctx_values: list[str | None] = []

    def execute(self, parameters: _EmptyParams) -> ToolResult:
        self.thread_names.append(threading.current_thread().name)
        self.ctx_values.append(_MARKER.get())
        return ToolResult(ok=True, content="ok")


def make_executor() -> tuple[ToolExecutor, _RecordingTool]:
    tool = _RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry), tool


async def _call(executor: ToolExecutor, call_id: str = "c1"):
    return await executor.execute(
        {"id": call_id, "function": {"name": "noop", "arguments": "{}"}},
        TurnContext(),
    )


async def test_tool_body_runs_off_the_event_loop_thread():
    """工具体（`tool.run` 内部）不在事件循环线程上跑。"""
    executor, tool = make_executor()
    main_thread_name = threading.current_thread().name

    result = await _call(executor)

    assert result["content"] == "ok"
    assert tool.thread_names, "工具至少被执行一次"
    for name in tool.thread_names:
        assert name != main_thread_name, f"工具体占用了事件循环线程: {name}"


async def test_contextvars_cross_into_tool_thread():
    """ContextVar 必须传播进工具体线程——权限桥 aask() 定位归属连接靠的就是它。

    若有人把 `asyncio.to_thread` 换成不复制 contextvars 的裸线程调用，此测试会红。
    """
    executor, tool = make_executor()
    _MARKER.set("conn-42")

    await _call(executor)

    assert tool.ctx_values == ["conn-42"]


async def test_concurrent_calls_progress_independently():
    """4 次并发工具调用全部完成（无需专用池，asyncio.to_thread 天然支持并发）。"""
    executor, tool = make_executor()

    results = await asyncio.gather(*[_call(executor, call_id=f"c{i}") for i in range(4)])

    assert [r["content"] for r in results] == ["ok"] * 4
    assert len(tool.thread_names) == 4
