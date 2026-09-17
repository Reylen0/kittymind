"""三条 ReAct 路径的特征测试（无需真实 LLM）。

`kitty_agent.py` 里 `run()`（子 Agent）/ `stream_run()`（同步 CLI）/ `async_stream_run()`
（生产路径）各实现了一遍「迭代 → 压缩 → 调 LLM → 执行工具 → 判断 max_iterations」，
审查报告 §3.2 指出三者**已经行为漂移**，并建议合并——但合并的前提是**先把现状钉住**，
否则改完无从判断"行为到底变没变"。

本文件就是那张网。三件事：

1. 钉住 `run()` / `stream_run()` 的**可观察行为**——这两条此前**零覆盖**，
   而子 Agent 走的正是 `run()`。
2. 钉住三条路径**共有**的不变量（异常类型与文案、回调时序、喂给 LLM 的消息序列）。
3. **显式记录已知漂移**，共两类四条：
   - 集成层三条：记忆召回 / 落库 / 后台提取只在 async 路径发生；
   - 消息累积一条：`run()` 的 `last_messages` 结尾多一条**最终 assistant 消息**（它没被喂给
     LLM），两条流式路径都不含。分界线是「非流式 vs 流式」，不是「同步 vs 异步」。
   合并时若有人"顺手"把任一漂移抹平，对应测试会红——逼他先做决策
   （子 Agent 该不该拿到父级记忆召回？该不该落库？`last_messages` 该不该含最终答复？），
   而不是默认改行为。

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

    同时实现同步 / 流式 / 异步流式三种接口，因此同一个替身能驱动全部三条路径——
    这也让"三条路径喂给 LLM 的消息序列是否一致"变成可断言的事实。

    `seen` 存的是**调用当时的深一层浅拷贝**：循环之后还会往 messages 上 append，
    不拷贝就会让历史快照跟着变，断言失去意义。
    """

    model = "scripted"

    def __init__(self, steps: list[tuple[str, list[dict]]]):
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

    def stream_with_tools(self, messages, tools=None, **kwargs):
        yield from self._events(*(self._take(messages)))

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


def make_agent(steps, *, max_iterations=30, memory=None):
    """构造一个只连替身的 agent。

    `aux_llm=None` 是显式哨兵（"强制回退主模型"），避免默认 `_AUX_AUTO` 在
    LLM_AUX_MODEL_ID 有值时真去构造一个小模型客户端——测试必须与机器环境无关。
    """
    llm = _ScriptedLLM(steps)
    rec = _Recorder()
    agent = KittyAgent(
        name="t", llm=llm, system_prompt="sp", tools=[],
        callbacks=[rec], max_iterations=max_iterations,
        memory=memory, aux_llm=None, interactive=False,
    )
    agent.tool_executor = _RecordingExecutor()
    return agent, llm, rec


def run_sync(agent, session_id="s1", text="hi") -> str:
    return agent.run(session_id, text)


def run_stream(agent, session_id="s1", text="hi") -> list[str]:
    return list(agent.stream_run(session_id, text))


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


# ── 2. stream_run()：同步 CLI（此前零覆盖）───────────────────────

def test_stream_run_yields_deltas_in_order():
    agent, _, _ = make_agent([("hello", [])])

    out = run_stream(agent)

    assert out == ["hello"]
    # 流式路径不把最终 assistant 消息 append 进 messages（见 §4「消息累积」漂移）
    assert [m["role"] for m in agent.last_messages] == ["system", "user"]


def test_stream_run_executes_tool_then_continues():
    agent, llm, rec = make_agent([("use tool", [_tool_call("noop")]), ("done", [])])

    out = run_stream(agent)

    assert out == ["use tool", "done"]
    assert agent.tool_executor.calls == [_tool_call("noop")]
    assert llm.seen[1][-1]["content"] == "ran:noop"
    assert rec.events.count("on_tool_start") == 1
    # 有工具调用时 assistant 消息照常入列；缺席的只有最后的「纯文本那一轮」
    assert [m["role"] for m in agent.last_messages] == ["system", "user", "assistant", "tool"]


def test_stream_run_raises_on_max_iterations():
    agent, _, _ = make_agent([("loop", [_tool_call()]), ("loop", [_tool_call()])],
                             max_iterations=2)

    with pytest.raises(AgentException):
        run_stream(agent)


def test_stream_run_emits_llm_end_with_none():
    """`stream_run` 的 on_llm_end 传 None（流式路径拿不到完整 response 对象）。

    与 `run()` 传 LLMResponse 不同——这是既有差异，此处记录以免被"统一"掉时无人察觉。
    """
    seen = []

    class _Cb:
        def on_llm_end(self, response):
            seen.append(response)

    agent, _, _ = make_agent([("hi", [])])
    agent.callbacks = [_Cb()]

    run_stream(agent)

    assert seen == [None]


# ── 3. 三条路径共有不变量 ────────────────────────────────────────

def test_sync_paths_feed_the_same_message_shape():
    """同一脚本在 run / stream_run 上产生的 LLM 输入序列应当一致。"""
    steps = [("use tool", [_tool_call("noop")]), ("done", [])]
    agent_a, llm_a, _ = make_agent(steps)
    agent_b, llm_b, _ = make_agent(steps)

    run_sync(agent_a)
    run_stream(agent_b)

    assert llm_a.seen == llm_b.seen


async def test_all_three_paths_feed_the_same_llm_inputs():
    """三条路径喂给 LLM 的消息序列**逐字节一致**——这是合并最硬的对齐目标。

    差异全在"喂完之后怎么处理"（回调/事件、落库、last_messages 尾部），
    不在"喂什么"。合并时若这条红了，说明改动了模型可见的提示，风险等级最高。
    """
    steps = [("use tool", [_tool_call("noop")]), ("done", [])]
    agent_a, llm_a, _ = make_agent(steps)
    agent_b, llm_b, _ = make_agent(steps)
    agent_c, llm_c, _ = make_agent(steps)

    run_sync(agent_a)
    run_stream(agent_b)
    await run_async(agent_c)

    assert llm_a.seen == llm_c.seen
    assert llm_b.seen == llm_c.seen


def test_all_paths_share_the_max_iterations_error_text():
    """三条路径的兜底异常必须同样是 `超过最大迭代次数 N`（合并时的对齐目标）。"""
    steps = [("loop", [_tool_call()]), ("loop", [_tool_call()])]
    agent_a, _, _ = make_agent(steps, max_iterations=2)
    agent_b, _, _ = make_agent(steps, max_iterations=2)

    with pytest.raises(AgentException) as a:
        run_sync(agent_a)
    with pytest.raises(AgentException) as b:
        run_stream(agent_b)

    assert str(a.value) == str(b.value)


def test_sync_last_messages_carry_the_final_assistant_reply():
    """**漂移第 4 条（前半）**：`run()` 会把最终 assistant 消息 append 进 `last_messages`。

    虽然这条消息**从未喂给 LLM**（下一轮循环已经 break 了），但它确实是本轮的产出，
    所以 `task_tool` 拿它估 token 是合理的。前半条在这里，后半条见文件末的流式对照。
    """
    agent, llm, _ = make_agent([("final answer", [])])

    run_sync(agent)

    assert agent.last_messages == llm.seen[-1] + [
        {"role": "assistant", "content": "final answer", "tool_calls": None}
    ]


# ── 4. 已知漂移：合并前必须先做决策，不能默认改行为 ────────────────

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


def test_sync_paths_skip_recall_and_commit(integration_spies):
    """**刻意记录漂移（报告 §3.2）**：`run()` 与 `stream_run()` 都不注入召回、不落库。

    刻意传 `memory=`：让"能力就摆在手边"而不是"压根没配"，否则这条测试只证明了
    `memory is None`，证明不了路径本身不做这件事。

    子 Agent 走的正是 `run()`，所以它拿不到父级的记忆召回，也不往会话表写。
    合并三条路径时若"顺手"改成一致，本测试会红——先回答两件事：
      1. 子 Agent 该不该拿到父级的记忆召回？（作用域污染 vs 上下文质量）
      2. 子 Agent 该不该落库？（它通常没有独立 session，session_id=None）
    在此之前，保持现状是**已知且被记录**的行为，而不是疏漏。
    """
    agent_a, _, _ = make_agent([("done", [])], memory=MagicMock())
    agent_b, _, _ = make_agent([("done", [])], memory=MagicMock())

    run_sync(agent_a)
    run_stream(agent_b)

    assert integration_spies == {"recall": 0, "commit": 0, "extract": 0}


async def test_async_path_does_recall_and_commit(integration_spies):
    """漂移的另一半：async 生产路径三者全都做（对照组，防止"统一"成更弱的一侧）。"""
    agent, _, _ = make_agent([("done", [])], memory=MagicMock())

    assert await run_async(agent) == "done"
    await _drain_bg_tasks(agent)

    assert integration_spies == {"recall": 1, "commit": 1, "extract": 1}


async def test_async_path_skips_commit_without_session(integration_spies):
    """无 session_id 时 async 也不落库（子 Agent 若改走 async 的前提条件之一）。"""
    agent, _, _ = make_agent([("done", [])])

    await run_async(agent, session_id=None)

    assert integration_spies["commit"] == 0


async def test_streaming_last_messages_equal_what_was_fed_to_the_llm():
    """**漂移第 4 条（后半）**：两条流式路径都不 append 最终 assistant 消息。

    于是它们的 `last_messages` 严格等于"最后一次喂给 LLM 的消息"，而 `run()` 多一条
    没人喂过的最终答复（见 §4 前半）。合并时这里必须**先定语义**再动手，两种都自洽：
      - 取 `run()` 语义（整轮含答复）——`task_tool` 的 token 估算要的就是这个；
      - 取流式语义（严格=已喂给 LLM 的）——落库路径靠 `turn_messages` 另行维护。
    顺带记一笔：本属性在 async 路径上此前**压根没被赋值**（恒为 `[]`），
    是本轮改动才让它非空的，所以这条漂移此前不可见。
    """
    agent_a, llm_a, _ = make_agent([("done", [])])
    agent_b, llm_b, _ = make_agent([("done", [])])

    run_stream(agent_a)
    assert await run_async(agent_b) == "done"

    for agent, llm in ((agent_a, llm_a), (agent_b, llm_b)):
        assert agent.last_messages == llm.seen[-1]
        assert [m["role"] for m in agent.last_messages] == ["system", "user"]
