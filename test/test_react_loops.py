"""两条 ReAct 路径的特征测试（无需真实 LLM）。

`kitty_agent.py` 里 `run()`（子 Agent）/ `async_stream_run()`（生产路径）各自
实现一遍「迭代 → 压缩 → 调 LLM → 执行工具 → 判断 max_iterations」。曾经还有
第三条 `stream_run()`（同步流式 CLI），已随其唯一调用方 `chat.py` 一并删除——
那条链路（`stream_run` → `_step_stream` → `BaseAgentLLM.stream_with_tools` →
适配器层同步 `stream_with_tools`）只为这一个 demo 脚本存在，`chat_async.py`
的异步 CLI demo 功能更全，足以覆盖手动测试需求。

本文件钉住两件事：

1. `run()` 的**可观察行为**——子 Agent 走的正是这条路径，此前零覆盖。
2. 两条路径**共有**的不变量（异常类型与文案、回调时序、喂给 LLM 的消息序列），
   以及**显式记录的已知漂移**，共两类五条：
   - 集成层三条：记忆召回 / 落库 / 后台提取只在 async 路径发生；
   - 消息与答复语义两条：`run()` 的 `last_messages` 结尾多一条**最终 assistant 消息**
     （它没被喂给 LLM），`async_stream_run()` 不含（分界线是「非流式 vs 流式」）；
     以及"空答复"的处理不对称——非流式把 `content=None` 视作未能收尾（抛迭代上限），
     流式只可能累积出 `""`（判为正常收尾）；工具调用轮的空正文同理（`""` vs `None`）。
   若有人改动时"顺手"把某条漂移抹平，对应测试会红——逼他先做决策
   （子 Agent 该不该拿到父级记忆召回？该不该落库？`last_messages` 该不该含最终答复？
   空答复算收尾还是算失败？），而不是默认改行为。

不碰真实文件系统 / 网络：工具执行器整个换成记录桩，LLM 用脚本替身。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.core.exceptions import AgentException, LLMException
from kittymind.core.llm_response import LLMResponse


# ── 替身 ─────────────────────────────────────────────────────────

def _tool_call(name: str = "noop", call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """按脚本逐步返回 `(text, tool_calls)`；记录每次调用收到的消息快照。

    同时实现同步 / 异步流式两种接口，因此同一个替身能驱动两条路径——
    这也让"两条路径喂给 LLM 的消息序列是否一致"变成可断言的事实。

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
    def _events(text: str, tool_calls: list[dict]):
        if text:
            yield SimpleNamespace(type="text_delta", delta=text, tool_calls=None, usage=None)
        yield SimpleNamespace(type="tool_calls_done", delta="", tool_calls=tool_calls, usage=None)

    def invoke(self, messages, tools=None, **kwargs) -> LLMResponse:
        text, tool_calls = self._take(messages)
        return LLMResponse(content=text, tool_calls=tool_calls)

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        for event in self._events(*(self._take(messages))):
            yield event


class _RecordingExecutor:
    """替换 ToolExecutor：不执行任何真实工具，只记录调用并按协议返回工具结果。"""

    def __init__(self):
        self.calls: list[dict] = []

    def execute(self, tool_call, ctx):
        self.calls.append(tool_call)
        name = tool_call["function"]["name"]
        return {"role": "tool", "tool_call_id": tool_call.get("id"), "content": f"ran:{name}"}


class _Recorder:
    """回调探针：按名字记录 `_emit` 的调用顺序（`_emit` 用 getattr 分派，故此法可行）。"""

    def __init__(self):
        self.events: list[str] = []

    def __getattr__(self, name):
        if name.startswith("on_"):
            def record(*args, **kwargs):
                self.events.append(name)
            return record
        raise AttributeError(name)


class _FakeSessionManager:
    """只给出 `_build_messages` / `_new_turn` 需要的最小接口（不碰 SQLite）。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def load_messages(self, session_id):
        return self._rows

    def get_session_state(self, session_id):
        return {}


def make_agent(steps, *, max_iterations=30, memory=None, session_manager=None):
    """构造一个只连替身的 agent。

    `aux_llm=None` 是显式哨兵（"强制回退主模型"），避免默认 `_AUX_AUTO` 在
    LLM_AUX_MODEL_ID 有值时真去构造一个小模型客户端——测试必须与机器环境无关。
    """
    llm = _ScriptedLLM(steps)
    rec = _Recorder()
    agent = KittyAgent(
        name="t", llm=llm, system_prompt="sp", tools=[],
        callbacks=[rec], max_iterations=max_iterations,
        memory=memory, session_manager=session_manager,
        aux_llm=None, interactive=False,
    )
    agent.tool_executor = _RecordingExecutor()
    return agent, llm, rec


def run_sync(agent, session_id="s1", text="hi") -> str:
    return agent.run(session_id, text)


async def run_async(agent, session_id="s1", text="hi") -> str:
    return "".join([c async for c in agent.async_stream_run(session_id=session_id, input_text=text)])


# ── 1. run()：子 Agent 路径（此前零覆盖）────────────────────────

def test_run_returns_text_and_exposes_messages():
    agent, llm, _ = make_agent([("done", [])])

    assert run_sync(agent) == "done"

    assert [m["role"] for m in agent.last_messages] == ["system", "user", "assistant"]
    assert agent.last_messages[-1]["content"] == "done"
    assert llm.seen[0][0]["role"] == "system"  # 首条始终是主 system


def test_run_callback_order():
    """起止时序：start → llm_start → llm_end → (tool_start → tool_end) → agent_end。"""
    agent, _, rec = make_agent([("thinking", [_tool_call()]), ("final", [])])

    assert run_sync(agent) == "final"

    assert rec.events == [
        "on_agent_start", "on_llm_start", "on_llm_end",
        "on_tool_start", "on_tool_end",
        "on_llm_start", "on_llm_end",
        "on_agent_end",
    ]


def test_run_executes_tool_then_continues():
    """工具结果必须以 role=tool 回灌，且第二轮 LLM 能看到它。"""
    agent, llm, _ = make_agent([("use tool", [_tool_call("noop")]), ("done", [])])

    assert run_sync(agent) == "done"

    assert agent.tool_executor.calls == [_tool_call("noop")]
    second_call = llm.seen[1]
    assert second_call[-1]["role"] == "tool"
    assert second_call[-1]["content"] == "ran:noop"


def test_run_executes_every_tool_call_in_one_step():
    """一步内请求多个工具时，逐个执行、逐个回灌（不是只取第一个）。"""
    calls = [_tool_call("a", "c1"), _tool_call("b", "c2")]
    agent, _, rec = make_agent([("go", calls), ("done", [])])

    assert run_sync(agent) == "done"

    assert agent.tool_executor.calls == calls
    assert [m["role"] for m in agent.last_messages] == [
        "system", "user", "assistant", "tool", "tool", "assistant",
    ]
    assert [m["tool_call_id"] for m in agent.last_messages if m["role"] == "tool"] == ["c1", "c2"]
    assert rec.events.count("on_tool_start") == 2
    assert rec.events.count("on_tool_end") == 2


def test_run_raises_agent_exception_when_iterations_exhausted():
    agent, _, rec = make_agent([("loop", [_tool_call()]), ("loop", [_tool_call()])],
                               max_iterations=2)

    with pytest.raises(AgentException) as exc:
        run_sync(agent)

    assert "超过最大迭代次数 2" in str(exc.value)
    assert rec.events[-1] == "on_agent_error"


def test_run_wraps_llm_error_and_keeps_cause():
    class _Boom(_ScriptedLLM):
        def invoke(self, messages, tools=None, **kwargs):
            raise ValueError("上游炸了")

    agent, _, rec = make_agent([("unused", [])])
    agent.llm = _Boom([("unused", [])])

    with pytest.raises(LLMException) as exc:
        run_sync(agent)

    assert isinstance(exc.value.__cause__, ValueError)  # 异常链不能丢（B904 批次的成果）
    assert rec.events == ["on_agent_start", "on_llm_start", "on_llm_error", "on_agent_error"]


# ── 2. 两条路径共有不变量 ────────────────────────────────────────

async def test_both_paths_feed_the_same_llm_inputs():
    """`run()` 与 `async_stream_run()` 喂给 LLM 的消息序列**逐字节一致**。

    差异全在"喂完之后怎么处理"（回调/事件、落库、last_messages 尾部），
    不在"喂什么"。这里若变红，说明改动了模型可见的提示，风险等级最高。
    """
    steps = [("use tool", [_tool_call("noop")]), ("done", [])]
    agent_a, llm_a, _ = make_agent(steps)
    agent_b, llm_b, _ = make_agent(steps)

    run_sync(agent_a)
    await run_async(agent_b)

    assert llm_a.seen == llm_b.seen


async def test_both_paths_share_the_max_iterations_error_text():
    """两条路径的兜底异常必须同样是 `超过最大迭代次数 N`（同一个 `_ensure_final_text`）。"""
    steps = [("loop", [_tool_call()]), ("loop", [_tool_call()])]
    agent_a, _, _ = make_agent(steps, max_iterations=2)
    agent_b, _, _ = make_agent(steps, max_iterations=2)

    with pytest.raises(AgentException) as a:
        run_sync(agent_a)
    with pytest.raises(AgentException) as b:
        await run_async(agent_b)

    assert str(a.value) == str(b.value)


def test_history_seq_marks_are_stripped_before_the_llm_call():
    """历史消息上的 `_seq` 只用于落库对账，必须在调 LLM 前剥掉（`_strip_internal`）。

    同时确认剥离是"只对喂给 LLM 的那份做拷贝"，本轮回灌的 `messages` 仍带 `_seq`。
    """
    rows = [
        {"seq": 1, "role": "user", "content": "早前的提问"},
        {"seq": 2, "role": "assistant", "content": "早前的回答"},
    ]
    agent, llm, _ = make_agent([("done", [])], session_manager=_FakeSessionManager(rows))

    assert run_sync(agent) == "done"

    assert all("_seq" not in m for m in llm.seen[0]), "内部标记不得泄漏给 LLM API"
    assert [m["role"] for m in llm.seen[0]] == ["system", "user", "assistant", "user"]
    assert agent.last_messages[1]["_seq"] == 1, "本轮回灌的 messages 仍保留 _seq 供落库对账"


# ── 3. 已知漂移：改动前必须先做决策，不能默认改行为 ────────────────

def test_run_last_messages_carry_the_final_assistant_reply():
    """**漂移第 4 条（前半）**：`run()` 会把最终 assistant 消息 append 进 `last_messages`。

    虽然这条消息**从未喂给 LLM**（下一轮循环已经 break 了），但它确实是本轮的产出，
    所以 `task_tool` 拿它估 token 是合理的。后半条见 `test_async_last_messages_*`。
    """
    agent, llm, _ = make_agent([("final answer", [])])

    run_sync(agent)

    assert agent.last_messages == llm.seen[-1] + [
        {"role": "assistant", "content": "final answer", "tool_calls": None}
    ]


@pytest.fixture
def integration_spies(monkeypatch):
    """把三个集成层入口换成计数器，观察各路径是否触达。"""
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


def test_run_skips_recall_and_commit(integration_spies):
    """**刻意记录漂移（第 1-3 条）**：`run()` 不注入召回、不落库、不起后台提取。

    刻意传 `memory=`：让"能力就摆在手边"而不是"压根没配"，否则这条测试只证明了
    `memory is None`，证明不了路径本身不做这件事。

    子 Agent 走的正是 `run()`，所以它拿不到父级的记忆召回，也不往会话表写。
    改动这条路径时若"顺手"改成一致，本测试会红——先回答两件事：
      1. 子 Agent 该不该拿到父级的记忆召回？（作用域污染 vs 上下文质量）
      2. 子 Agent 该不该落库？（它通常没有独立 session，session_id=None）
    在此之前，保持现状是**已知且被记录**的行为，而不是疏漏。
    """
    agent, _, _ = make_agent([("done", [])], memory=MagicMock())

    run_sync(agent)

    assert integration_spies == {"recall": 0, "commit": 0, "extract": 0}


async def test_async_path_does_recall_and_commit(integration_spies):
    """漂移的另一半：async 生产路径三者全都做（对照组，防止改动"统一"成更弱的一侧）。"""
    agent, _, _ = make_agent([("done", [])], memory=MagicMock())

    assert await run_async(agent) == "done"
    await _drain_bg_tasks(agent)

    assert integration_spies == {"recall": 1, "commit": 1, "extract": 1}


async def test_async_path_skips_commit_without_session(integration_spies):
    """无 session_id 时 async 也不落库（子 Agent 若改走 async 的前提条件之一）。"""
    agent, _, _ = make_agent([("done", [])])

    await run_async(agent, session_id=None)

    assert integration_spies["commit"] == 0


async def test_async_last_messages_equal_what_was_fed_to_the_llm():
    """**漂移第 4 条（后半）**：`async_stream_run()` 不 append 最终 assistant 消息。

    于是它的 `last_messages` 严格等于"最后一次喂给 LLM 的消息"，而 `run()` 多一条
    没人喂过的最终答复（见前半）。改动这里必须**先定语义**再动手，两种都自洽：
      - 取 `run()` 语义（整轮含答复）——`task_tool` 的 token 估算要的就是这个；
      - 取流式语义（严格=已喂给 LLM 的）——落库路径靠 `turn_messages` 另行维护。
    顺带记一笔：本属性在 async 路径上曾经压根没被赋值（恒为 `[]`），后来才修上。
    """
    agent, llm, _ = make_agent([("done", [])])

    assert await run_async(agent) == "done"

    assert agent.last_messages == llm.seen[-1]
    assert [m["role"] for m in agent.last_messages] == ["system", "user"]


def test_run_keeps_an_empty_string_reply_as_an_answer():
    """`content=""` 是一段（空的）答复 → 正常收尾、返回空串。"""
    agent, _, _ = make_agent([("", [])])

    assert run_sync(agent) == ""


def test_run_treats_none_reply_as_exhaustion():
    """**漂移第 5 条（前半）**：`content=None` 且无工具调用 → 按"本轮没能收尾"处理。

    注意与上一条的区别：`""` 是答复，`None` 是"没拿到答复"。非流式才有 None 这一态
    （`LLMResponse.content` 可为 None），于是它被判为迭代耗尽、抛 `AgentException`
    （文案是"超过最大迭代次数"，严格说并不准确——这里只是沿用既有行为并把它钉住）。
    """
    agent, _, rec = make_agent([(None, [])])

    with pytest.raises(AgentException, match="超过最大迭代次数"):
        run_sync(agent)

    assert rec.events[-1] == "on_agent_error"


async def test_async_path_turns_a_blank_reply_into_empty_string():
    """**漂移第 5 条（后半）**：流式路径没有 None 态，空答复只会累积成 `""`。

    同一条脚本（`content=None`，无工具调用）在 `run()` 上抛异常，在 `async_stream_run()`
    上正常返回空串——这是"非流式 vs 流式"的分界。改动时必须先定语义（空答复算收尾还是
    算失败），再动手。
    """
    agent, _, _ = make_agent([(None, [])], max_iterations=1)

    assert await run_async(agent) == ""

    assert [m["role"] for m in agent.last_messages] == ["system", "user"]


async def test_tool_only_round_normalizes_empty_content_differently():
    """**漂移第 5 条（工具调用轮子情形）**：assistant 正文的归一化两条路径写法不同。

    工具调用轮通常没有正文（模型只吐 tool_calls），而这行消息会被原样写进后续请求，
    所以 `""` 与 `None` 的差别是真实可见的：`run()` 保留 `""`，`async_stream_run()`
    归一成 `None`（OpenAI 协议允许 content=null）。改动时别顺手统一。
    """
    steps = [("", [_tool_call("noop")]), ("done", [])]
    agent_a, _, _ = make_agent(steps)
    agent_b, _, _ = make_agent(steps)

    run_sync(agent_a)
    await run_async(agent_b)

    expected_calls = [_tool_call("noop")]
    assert agent_a.last_messages[2] == {
        "role": "assistant", "content": "", "tool_calls": expected_calls,
    }
    assert agent_b.last_messages[2] == {
        "role": "assistant", "content": None, "tool_calls": expected_calls,
    }
