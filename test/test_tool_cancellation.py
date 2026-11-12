"""工具取消的物理传导特征测试。

核心主张：
  - `bash` 工具改走 `asyncio.create_subprocess_exec/_shell`（原生协作式取消）之后，
    `task.cancel()` 必须能真正杀掉底层子进程，而不是"不再等待、子进程在后台裸跑"；
  - 命令本身超时（而非被取消）时同样要杀掉子进程，不是让它继续跑到自然结束；
  - `task` 子 Agent 委派的挂钟超时（`SUBAGENT_WALL_TIMEOUT`）要让父 Agent 在子任务
    卡死时拿到一个失败结果返回，而不是无限期挂起。
"""

import asyncio
import sys

import pytest

from kittymind.config import cfg
from kittymind.tools.builtin.bash_tool import BashTool, BashToolParam, bash_cwd
from kittymind.tools.builtin.task_tool import TaskInput, TaskTool


# ── bash：取消必须真的杀掉子进程 ────────────────────────────────────

async def test_bash_tool_cancellation_kills_the_real_subprocess(tmp_path):
    marker = tmp_path / "marker.txt"
    script = tmp_path / "loop.py"
    script.write_text(
        "import sys, time\n"
        "p = sys.argv[1]\n"
        "for _ in range(200):\n"
        "    with open(p, 'a') as f:\n"
        "        f.write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{marker}"'

    token = bash_cwd.set(str(tmp_path))
    try:
        task = asyncio.create_task(
            BashTool().aexecute(BashToolParam(command=command, timeout=30))
        )
        for _ in range(100):
            if marker.exists() and marker.stat().st_size > 0:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("子进程未能在预期时间内启动写入")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        size_at_cancel = marker.stat().st_size
        await asyncio.sleep(0.5)  # 若子进程还活着，这段时间内还会再写入几次
        assert marker.stat().st_size == size_at_cancel, (
            "task.cancel() 之后子进程应已被 proc.kill() 杀死，不应继续写入"
        )
    finally:
        bash_cwd.reset(token)


async def test_bash_tool_timeout_also_kills_the_subprocess(tmp_path):
    """超时（非取消）同样是一条"不能让子进程裸跑"的路径，单独覆盖。"""
    marker = tmp_path / "marker.txt"
    script = tmp_path / "loop.py"
    script.write_text(
        "import sys, time\n"
        "p = sys.argv[1]\n"
        "for _ in range(200):\n"
        "    with open(p, 'a') as f:\n"
        "        f.write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{marker}"'

    token = bash_cwd.set(str(tmp_path))
    try:
        result = await BashTool().aexecute(BashToolParam(command=command, timeout=1))
    finally:
        bash_cwd.reset(token)

    assert not result.ok
    assert "超时" in result.content

    size_at_return = marker.stat().st_size
    await asyncio.sleep(0.5)
    assert marker.stat().st_size == size_at_return, "超时后子进程应已被杀死"


async def test_bash_tool_normal_command_still_works(tmp_path):
    token = bash_cwd.set(str(tmp_path))
    try:
        result = await BashTool().aexecute(BashToolParam(command="echo hello", timeout=10))
    finally:
        bash_cwd.reset(token)
    assert result.ok
    assert "hello" in result.content


# ── task：子 Agent 挂钟超时 ─────────────────────────────────────────

class _HangingLLM:
    """子 Agent 第一次调用 LLM 时就卡死——模拟"某次调用卡住"而非步数爆表。"""

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        await asyncio.sleep(3600)
        yield  # pragma: no cover - 不会跑到这里


async def test_task_tool_wall_timeout_fails_fast_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr(cfg, "SUBAGENT_WALL_TIMEOUT", 0.2)
    tool = TaskTool(llm=_HangingLLM(), sub_tools=[])
    result = await tool.aexecute(TaskInput(prompt="随便一个会卡住的任务"))
    assert not result.ok
    assert "超时" in result.content
