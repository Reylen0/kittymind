"""后台任务引用回归（P1，无需真实 LLM）。

asyncio 对运行中的 task 只持**弱引用**：`asyncio.create_task()` 的返回值不保存，
任务可能在执行途中被 GC 掉，异常也会被静默吞掉（`asyncio.EventLoop` 只在任务
结束后才回收引用）。记忆提取是 fire-and-forget 的后台任务，最容易被吞。

这里验证：任务在运行期间被 agent 强引用持有，完成后从集合里释放。
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kittymind.agent import kitty_agent as agent_mod
from kittymind.agent.kitty_agent import KittyAgent


class _FakeLLM:
    """只回一段文本、不调工具的最小 LLM 替身。"""

    model = "fake"

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        yield SimpleNamespace(
            type="text_delta", delta="ok", tool_calls=None, usage=None
        )


async def _noop_recall(self, messages, input_text, session_id):
    return None


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(KittyAgent, "_inject_memory_recall", _noop_recall)
    return KittyAgent(name="t", llm=_FakeLLM(), memory=MagicMock(), interactive=False)


async def _collect(agent):
    chunks = []
    async for chunk in agent.async_stream_run(session_id=None, input_text="hi"):
        chunks.append(chunk)
    return "".join(chunks)


async def test_memory_extraction_task_is_strongly_referenced(agent, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_extract(messages, llm, store):
        calls.append(messages)
        started.set()
        release.wait(5)
        return True

    monkeypatch.setattr(agent_mod, "extract_memories", fake_extract)

    assert await _collect(agent) == "ok"

    for _ in range(100):                     # 等后台线程真正跑起来
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set(), "记忆提取任务未启动（可能被 GC 吞掉了）"
    assert len(agent._bg_tasks) == 1, "运行中的后台任务必须被强引用持有"

    release.set()
    for _ in range(100):                     # 等任务结束并触发 done 回调
        if not agent._bg_tasks:
            break
        await asyncio.sleep(0.02)
    assert agent._bg_tasks == set(), "任务完成后应从引用集合中移除"
    assert calls, "记忆提取未执行"
