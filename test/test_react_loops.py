"""ReAct 循环（`async_stream_run`）的特征测试（无需真实 LLM）。

`kitty_agent.py` 曾经有 `run()`（同步，子 Agent 专用）与 `async_stream_run()`
（异步，生产路径）两套并行实现，各自重复一遍「迭代 → 压缩 → 调 LLM → 执行
工具 → 判断 max_iterations」。两者已合并：现在只有 `async_stream_run()`
一份实现，子 Agent 与根 Agent 都走它——区别只是两个参数：

  - `root`：`True`（默认，根 Agent）会重新设置 cwd / 重置委派预算 / 覆盖根
    session 标签；`False`（子 Agent）原样沿用父级已建立的作用域。
  - `session_id`：子 Agent 传 `None`，天然跳过落库（`_commit_turn` 要求非空
    session_id）。

**唯一容易踩坑的一点**：记忆召回 / 后台记忆提取**不看 `session_id`**，只看
`self.memory`（这个 Agent 实例是否配了 `MemoryStore`）。子 Agent 之所以不会
触发父级的记忆召回，不是因为它传了 `session_id=None` 或 `root=False`，而是
因为 `task_tool.py` 构造子 Agent 时压根没把 `memory=` 传进去。本文件专门
钉住这一点（见 `test_recall_still_fires_without_session_id_when_memory_wired`），
免得日后有人"整理"构造参数时手滑传了 `memory=self._memory`，就在无意间让
子 Agent 拿到了父级的记忆。

另一点非显而易见的行为：agent 的全部分派统一走 `EventBus`（无 callbacks 机制，
已于 2026-09-18 移除）。工具起止事件 `agent.tool_call` / `agent.tool_result`
的进程内消费者是 `task_tool` 的工具计数器——它订阅的是子 Agent 自己的私有
总线，不是共享总线（并行子 Agent 场景下按 session_id 过滤会串计数）。

不碰真实文件系统 / 网络：工具执行器整个换成记录桩，LLM 用脚本替身。
"""

import asyncio

import pytest

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.core.exceptions import AgentException, LLMException
from kittymind.core.llm_response import StreamEvent
from kittymind.events.types import AGENT_TOOL_CALL, AGENT_TOOL_RESULT


# ── 替身 ─────────────────────────────────────────────────────────

def _tool_call(name: str = "noop", call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """按脚本逐步返回 `(text, tool_calls)`；记录每次调用收到的消息快照。

    `seen` 存的是**调用当时的深一层浅拷贝**：循环之后还会往 messages 上 append，
    不拷贝就会让历史快照跟着变，断言失去意义。
    """

    model = "scripted"

    def __init__(self, steps: list[tuple[str | None, list[dict]]]):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    def _take(self, messages):
        self.seen.append([dict(m) for m in messages])
        assert self.steps, "脚本步数用尽：循环比预期多跑了一轮"
        return self.steps.pop(0)

    @staticmethod
    def _events(text: str | None, tool_calls: list[dict]):
        if text:
            yield StreamEvent(type="text_delta", delta=text)
        yield StreamEvent(type="tool_calls_done", tool_calls=tool_calls)

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        for event in self._events(*(self._take(messages))):
            yield event


class _RecordingExecutor:
    """替换 ToolExecutor：不执行任何真实工具，只记录调用并按协议返回工具结果。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def execute(self, tool_call, ctx):
        self.calls.append(tool_call)
        name = tool_call["function"]["name"]
        return {"role": "tool", "tool_call_id": tool_call.get("id"), "content": f"ran:{name}"}

    async def execute_batch(self, tool_calls, ctx):
        """与真实 execute_batch 的契约对齐：结果按声明序返回。"""
        return [await self.execute(tc, ctx) for tc in tool_calls]


class _Recorder:
    """事件探针：订阅 agent 的 EventBus，按序记录事件类型。

    用通配符订阅（"*"）接住所有事件；断言时按需过滤。
    """

    def __init__(self):
        self.events: list[str] = []

    def attach(self, bus) -> None:
        async def _record(event_type, data):
            self.events.append(event_type)
        bus.subscribe("*", _record)


class _FakeSessionManager:
    """只给出 `_resolve_workspace` / `_build_messages` / `_new_turn` / `_commit_turn`
    需要的最小接口（不碰 SQLite；本文件的测试不落压缩分支，故 `session_exists`/
    `archive_and_compact` 均不需要实现）。
    """

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.committed: list[tuple] = []

    def get_session(self, session_id):
        return None  # 无存量 workspace_id 记录，回退默认工作目录

    def load_messages(self, session_id):
        return self._rows

    def get_session_state(self, session_id):
        return {}

    def append_turn(self, session_id, turn_messages, input_text, workspace_id=None):
        self.committed.append((session_id, turn_messages, input_text))

    def save_session_state(self, session_id, **state_fields):
        pass


def make_agent(steps, *, max_iterations=30, memory=None, session_manager=None):
    """构造一个只连替身的 agent。

    `aux_llm=None` 是显式哨兵（"强制回退主模型"），避免默认 `_AUX_AUTO` 在
    LLM_AUX_MODEL_ID 有值时真去构造一个小模型客户端——测试必须与机器环境无关。
    """
    llm = _ScriptedLLM(steps)
    rec = _Recorder()
    agent = KittyAgent(
        name="t", llm=llm, system_prompt="sp", tools=[],
        max_iterations=max_iterations,
        memory=memory, session_manager=session_manager,
        aux_llm=None, interactive=False,
    )
    rec.attach(agent.event_bus)
    agent.tool_executor = _RecordingExecutor()
    return agent, llm, rec


async def run_agent(agent, session_id="s1", text="hi", root=True) -> str:
    return "".join([
        c async for c in
        agent.async_stream_run(session_id=session_id, input_text=text, root=root)
    ])


# ── 1. 基本行为 ──────────────────────────────────────────────────

async def test_returns_text_and_exposes_messages():
    agent, llm, _ = make_agent([("done", [])])

    assert await run_agent(agent) == "done"

    assert [m["role"] for m in agent.last_messages] == ["system", "user", "assistant"]
    assert agent.last_messages[-1]["content"] == "done"
    assert llm.seen[0][0]["role"] == "system"  # 首条始终是主 system


async def test_tool_calls_emit_events_in_order():
    """工具起止事件经 EventBus 发出且按序——task_tool 的工具计数器就靠这个。"""
    agent, _, rec = make_agent([("", [_tool_call()]), ("final", [])])

    assert await run_agent(agent) == "final"

    tool_events = [e for e in rec.events if e.startswith("agent.tool")]
    assert tool_events == [AGENT_TOOL_CALL, AGENT_TOOL_RESULT]


async def test_executes_tool_then_continues():
    """工具结果必须以 role=tool 回灌，且第二轮 LLM 能看到它。

    工具轮的正文留空：非空文本会作为流式 delta 被 yield，混进拼接结果——那是
    另一件事（真实聊天场景里模型确实会先说几句再调工具），这里只测工具续接。
    """
    agent, llm, _ = make_agent([("", [_tool_call("noop")]), ("done", [])])

    assert await run_agent(agent) == "done"

    assert agent.tool_executor.calls == [_tool_call("noop")]
    second_call = llm.seen[1]
    assert second_call[-1]["role"] == "tool"
    assert second_call[-1]["content"] == "ran:noop"


async def test_executes_every_tool_call_in_one_step():
    """一步内请求多个工具时，逐个执行、逐个回灌（不是只取第一个）。"""
    calls = [_tool_call("a", "c1"), _tool_call("b", "c2")]
    agent, _, rec = make_agent([("", calls), ("done", [])])

    assert await run_agent(agent) == "done"

    assert agent.tool_executor.calls == calls
    assert [m["role"] for m in agent.last_messages] == [
        "system", "user", "assistant", "tool", "tool", "assistant",
    ]
    assert [m["tool_call_id"] for m in agent.last_messages if m["role"] == "tool"] == ["c1", "c2"]
    assert rec.events.count(AGENT_TOOL_CALL) == 2
    assert rec.events.count(AGENT_TOOL_RESULT) == 2


async def test_raises_agent_exception_when_iterations_exhausted():
    agent, _, _ = make_agent([("loop", [_tool_call()]), ("loop", [_tool_call()])],
                             max_iterations=2)

    with pytest.raises(AgentException) as exc:
        await run_agent(agent)

    assert "超过最大迭代次数 2" in str(exc.value)


async def test_wraps_llm_error_and_keeps_cause():
    class _Boom(_ScriptedLLM):
        async def async_stream_with_tools(self, messages, tools=None, **kwargs):
            raise ValueError("上游炸了")
            yield  # 不可达；仅用于让函数体含 yield，成为异步生成器

    agent, _, _ = make_agent([("unused", [])])
    agent.llm = _Boom([("unused", [])])

    with pytest.raises(LLMException) as exc:
        await run_agent(agent)

    assert isinstance(exc.value.__cause__, ValueError)  # 异常链不能丢


async def test_history_seq_marks_are_stripped_before_the_llm_call():
    """历史消息上的 `_seq` 只用于落库对账，必须在调 LLM 前剥掉（`_strip_internal`）。

    同时确认剥离是"只对喂给 LLM 的那份做拷贝"，本轮回灌的 `messages` 仍带 `_seq`。
    """
    rows = [
        {"seq": 1, "role": "user", "content": "早前的提问"},
        {"seq": 2, "role": "assistant", "content": "早前的回答"},
    ]
    agent, llm, _ = make_agent([("done", [])], session_manager=_FakeSessionManager(rows))

    assert await run_agent(agent) == "done"

    assert all("_seq" not in m for m in llm.seen[0]), "内部标记不得泄漏给 LLM API"
    assert [m["role"] for m in llm.seen[0]] == ["system", "user", "assistant", "user"]
    assert agent.last_messages[1]["_seq"] == 1, "本轮回灌的 messages 仍保留 _seq 供落库对账"


async def test_last_messages_carry_the_final_assistant_reply():
    """`last_messages` 结尾含本轮最终 assistant 答复（`task_tool` 拿它 estimate_tokens）。

    这条消息**从未喂给 LLM**（下一轮循环已经 break 了），但它确实是本轮的产出。
    """
    agent, llm, _ = make_agent([("final answer", [])])

    await run_agent(agent)

    assert agent.last_messages == llm.seen[-1] + [
        {"role": "assistant", "content": "final answer", "tool_calls": None}
    ]


async def test_empty_string_reply_is_a_normal_answer():
    """`content=""` 是一段（空的）答复 → 正常收尾、返回空串。"""
    agent, _, _ = make_agent([("", [])])

    assert await run_agent(agent) == ""


async def test_none_content_step_normalizes_to_empty_string():
    """脚本步 `(None, [])`：没有 text_delta 事件，累积出的正文自然是 `""`，正常收尾。

    没有工具调用、也没有文本，这一步就是"模型交了个空答复"，不是错误。
    """
    agent, _, _ = make_agent([(None, [])], max_iterations=1)

    assert await run_agent(agent) == ""
    assert [m["role"] for m in agent.last_messages] == ["system", "user", "assistant"]


async def test_tool_round_with_empty_text_normalizes_content_to_none():
    """工具调用轮没有正文时，assistant 消息的 content 归一成 `None`（OpenAI 协议允许 null）。

    这行消息会被原样写进下一轮请求，所以 `None` 而非 `""` 是真实可见的格式。
    """
    agent, _, _ = make_agent([("", [_tool_call("noop")]), ("done", [])])

    await run_agent(agent)

    assert agent.last_messages[2] == {
        "role": "assistant", "content": None, "tool_calls": [_tool_call("noop")],
    }


# ── 2. 集成层：只看「配了什么」，不看「走了哪条路径」 ────────────

@pytest.fixture
def integration_spies(monkeypatch):
    """把三个集成层入口换成计数器，观察是否触达。"""
    hits = {"recall": 0, "commit": 0, "extract": 0}

    async def spy_recall(self, messages, input_text, session_id):
        hits["recall"] += 1

    def spy_commit(self, *args, **kwargs):
        hits["commit"] += 1

    async def spy_extract(self, turn_messages):
        hits["extract"] += 1

    monkeypatch.setattr(KittyAgent, "_inject_memory_recall", spy_recall)
    monkeypatch.setattr(KittyAgent, "_commit_turn", spy_commit)
    monkeypatch.setattr(KittyAgent, "_extract_memories_bg", spy_extract)
    return hits


async def _drain_bg_tasks(agent) -> None:
    """`_extract_memories_bg` 是 create_task 起的后台任务，必须让出控制权才会跑。"""
    for _ in range(50):
        if not agent._bg_tasks:
            return
        await asyncio.sleep(0)


async def test_recall_and_commit_and_extract_happen_when_wired(integration_spies):
    """三者都配齐（memory + session_manager + 非空 session_id）时，三者都触发。"""
    from unittest.mock import MagicMock
    agent, _, _ = make_agent([("done", [])], memory=MagicMock())

    assert await run_agent(agent) == "done"
    await _drain_bg_tasks(agent)

    assert integration_spies == {"recall": 1, "commit": 1, "extract": 1}


async def test_commit_skipped_without_session_id(integration_spies):
    """`session_id=None` 时不落库——`_commit_turn` 要求真实会话才有意义。"""
    agent, _, _ = make_agent([("done", [])])

    await run_agent(agent, session_id=None)

    assert integration_spies["commit"] == 0


async def test_recall_still_fires_without_session_id_when_memory_wired(integration_spies):
    """**关键行为**：记忆召回只看 `self.memory`，不看 `session_id`。

    子 Agent 用 `session_id=None` 调用（模拟 task_tool 的调用形状），如果这里
    也配了 `memory=`，召回依然会触发——`session_id=None` 只影响落库，不影响召回。

    这正是为什么 `task_tool.py` 构造子 Agent 时必须**不传** `memory=`：子 Agent
    不该拿到父级记忆，靠的是"没配"，不是"session_id 是 None"。改动 task_tool
    的构造参数时，如果谁手滑把 `memory=self._memory` 加回去，子 Agent 会立刻
    拿到父级的记忆召回——这正是本测试要防的事。
    """
    from unittest.mock import MagicMock
    agent, _, _ = make_agent([("done", [])], memory=MagicMock())

    await run_agent(agent, session_id=None)

    assert integration_spies["recall"] == 1
    assert integration_spies["commit"] == 0
