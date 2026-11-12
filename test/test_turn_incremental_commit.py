"""轮内增量提交的特征测试（ARCHITECTURE.md Phase 34 ①）。

核心主张：用户消息、每步 assistant+tool_result 批次、以及压缩结果都在
产出的当下就落库，不等整轮 ReAct 循环跑完——进程在循环中途"崩溃"（这里
用 LLM 调用抛异常模拟同一个后果：循环再也无法推进到 `_commit_turn`），
已经发生的内容依然能在 `sessions.db` 里查到。

与 `test_react_loops.py` 的分工：那边用假 session manager 验证 ReAct 循环
本身的控制流（事件顺序、迭代上限等），这里专门验证"落库时机"这一件事，
所以用真实 SQLite（tmp_path），直接查库断言持久化结果。
"""

import pytest

from kittymind.agent.kitty_agent import (
    ContextCompressor, KittyAgent, TurnContext, _fresh_tracker,
)
from kittymind.core.exceptions import LLMException
from kittymind.core.llm_response import StreamEvent
from kittymind.session.manager import SessionManager


def _tool_call(name: str = "noop", call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _CrashingLLM:
    """按脚本返回若干步正常结果，脚本耗尽后抛异常——模拟"进程在这一步崩溃"。

    真实崩溃是进程被杀，这里用"LLM 调用失败"模拟同一个后果：ReAct 循环
    再也无法推进到收尾的 `_commit_turn`，只能靠此前的增量提交兜底。
    """

    model = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        if not self.steps:
            raise RuntimeError("模拟进程崩溃：LLM 调用再也不会成功")
        text, tool_calls = self.steps.pop(0)
        if text:
            yield StreamEvent(type="text_delta", delta=text)
        yield StreamEvent(type="tool_calls_done", tool_calls=tool_calls)


class _RecordingExecutor:
    """不执行真实工具，只按协议回灌结果（同 test_react_loops.py 的替身）。"""

    async def execute(self, tool_call, session_id=None, guardrail_state=None):
        name = tool_call["function"]["name"]
        return {"role": "tool", "tool_call_id": tool_call.get("id"), "content": f"ran:{name}"}

    async def execute_batch(self, tool_calls, session_id=None, guardrail_state=None):
        return [await self.execute(tc, session_id, guardrail_state) for tc in tool_calls]


def _make_agent(tmp_path, steps):
    agent = KittyAgent(
        name="t", llm=_CrashingLLM(steps), system_prompt="sp", tools=[],
        session_manager=SessionManager(db_path=tmp_path / "s.db"),
        aux_llm=None, interactive=False,
    )
    agent.tool_executor = _RecordingExecutor()
    return agent


async def _run(agent, session_id, text):
    async for _ in agent.run_conversation(session_id=session_id, input_text=text):
        pass


# ── 崩溃场景：已发生的内容不会凭空消失 ─────────────────────────────

async def test_user_message_survives_a_crash_before_any_llm_response(tmp_path):
    """LLM 第一次调用就"崩溃"：用户消息仍然已经落库（①的最低要求）。"""
    agent = _make_agent(tmp_path, steps=[])  # 脚本为空 → 第一次调用即抛异常

    with pytest.raises(LLMException):
        await _run(agent, "s1", "你好")

    history = agent.session_manager.load_history("s1")
    assert [m["role"] for m in history] == ["user"]
    assert history[0]["content"] == "你好"


async def test_completed_steps_survive_a_crash_in_a_later_step(tmp_path):
    """第 1 步工具调用正常跑完并落库；第 2 步"崩溃"——第 1 步的内容不会跟着丢。"""
    agent = _make_agent(tmp_path, steps=[("", [_tool_call("noop")])])

    with pytest.raises(LLMException):
        await _run(agent, "s1", "跑个工具")

    history = agent.session_manager.load_history("s1")
    assert [m["role"] for m in history] == ["user", "assistant", "tool"]
    assert history[1]["tool_calls"][0]["function"]["name"] == "noop"
    assert history[2]["content"] == "ran:noop"


async def test_two_completed_steps_both_survive_a_crash_in_a_third_step(tmp_path):
    """多步场景：前两步都已落库，第 3 步崩溃，前两步内容齐全不丢不重。"""
    agent = _make_agent(tmp_path, steps=[
        ("", [_tool_call("a", "c1")]),
        ("", [_tool_call("b", "c2")]),
    ])

    with pytest.raises(LLMException):
        await _run(agent, "s1", "跑两个工具")

    history = agent.session_manager.load_history("s1")
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant", "tool"]
    assert history[2]["content"] == "ran:a"
    assert history[4]["content"] == "ran:b"


# ── 正常收尾：_commit_turn 只补最后一条，不重复落库前面的步骤 ────────

async def test_commit_turn_only_adds_the_trailing_final_message(tmp_path):
    """正常收尾时，前面的步骤已经逐步落库过；`_commit_turn` 只补最后一条。"""
    agent = _make_agent(tmp_path, steps=[("", [_tool_call("noop")]), ("done", [])])

    result = await agent.run("s1", "跑个工具后结束")
    assert result.text == "done"

    history = agent.session_manager.load_history("s1")
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[-1]["content"] == "done"


async def test_simple_turn_without_tool_calls_commits_user_and_final_message(tmp_path):
    """没有工具调用的最简单一轮：用户消息 + 最终回答各落一条，不多不少。"""
    agent = _make_agent(tmp_path, steps=[("done", [])])

    result = await agent.run("s1", "你好")
    assert result.text == "done"

    history = agent.session_manager.load_history("s1")
    assert [m["role"] for m in history] == ["user", "assistant"]


# ── 压缩：发生的当下就落库，不等整轮结束（直接单测 `_commit_compaction`）──

async def test_compaction_is_committed_immediately_and_updates_loaded_seqs(tmp_path):
    """压缩一旦发生：旧行标 compacted、摘要行拿到真实 seq、`compressed_once`
    立即持久化——都不依赖轮次是否跑到结尾。

    直接构造 `TurnContext` 调用 `_commit_compaction`，不依赖真的触发
    `ContextCompressor.compress()`（那需要人为顶高 token 占用比，属于
    压缩器自身的边界测试范畴，`test_context_compressor.py` 已经覆盖）。
    """
    agent = _make_agent(tmp_path, steps=[])
    sm = agent.session_manager
    seqs = sm.append_turn("s1", [
        {"role": "user", "content": "早前的提问"},
        {"role": "assistant", "content": "早前的回答"},
    ])

    messages = [
        {"role": "system", "content": "sp"},
        {"role": "user", "content": "早前的提问", "_seq": seqs[0]},
        # 第二条被摘要掉：换成一条带 _SUMMARY_MARKER、无 _seq 的合成消息
        {"role": "assistant", "content": "【历史摘要】...", "_compressed_summary": True},
    ]
    turn = TurnContext(
        messages=messages, loaded_seqs=list(seqs),
        tools_schema=None, tracker=_fresh_tracker(),
        compressor=ContextCompressor(agent.aux_model),
        session_id="s1", effective_workspace_id=None,
        turn_messages=[messages[1]],
    )

    agent._commit_compaction(turn, "s1")

    full = sm.load_full_history("s1")
    compacted = [m for m in full if m["compacted"]]
    assert len(compacted) == 1
    assert compacted[0]["content"] == "早前的回答"

    active = [m for m in full if m["active"]]
    assert [m["role"] for m in active] == ["user", "assistant"]
    assert "历史摘要" in active[1]["content"]

    assert sm.get_session_state("s1")["compressed_once"] is True
    # loaded_seqs 已经把被压缩掉的旧 seq 换成了摘要行的新 seq
    assert seqs[1] not in turn.loaded_seqs
    assert active[1]["seq"] in turn.loaded_seqs


async def test_commit_compaction_is_a_noop_without_anything_to_compact(tmp_path):
    """没有旧行需要标记时（compacted_seqs 为空）直接跳过，不瞎调用底层写入。"""
    agent = _make_agent(tmp_path, steps=[])
    sm = agent.session_manager
    seqs = sm.append_turn("s1", [{"role": "user", "content": "hi"}])

    messages = [
        {"role": "system", "content": "sp"},
        {"role": "user", "content": "hi", "_seq": seqs[0]},
    ]
    turn = TurnContext(
        messages=messages, loaded_seqs=list(seqs),
        tools_schema=None, tracker=_fresh_tracker(),
        compressor=ContextCompressor(agent.aux_model),
        session_id="s1", effective_workspace_id=None,
        turn_messages=[messages[1]],
    )

    agent._commit_compaction(turn, "s1")  # 不抛即通过

    assert sm.get_session_state("s1").get("compressed_once") is not True


# ── ok 字段也走增量提交，不只是等轮次收尾才补 ───────────────────────

async def test_tool_ok_flag_is_persisted_by_the_incremental_commit(tmp_path):
    """工具失败（ok=False）时，这个标记要跟着同一步的增量提交落库——前端据此
    判定成败（见 kittymind.encoding 的姊妹修复：ARCHITECTURE.md 同批 bug）。
    """
    class _FailingExecutor:
        async def execute(self, tool_call, session_id=None, guardrail_state=None):
            return {"role": "tool", "tool_call_id": tool_call.get("id"),
                    "content": "boom", "ok": False}

        async def execute_batch(self, tool_calls, session_id=None, guardrail_state=None):
            return [await self.execute(tc, session_id, guardrail_state) for tc in tool_calls]

    agent = _make_agent(tmp_path, steps=[("", [_tool_call("bash")]), ("done", [])])
    agent.tool_executor = _FailingExecutor()

    result = await agent.run("s1", "跑个会失败的命令")
    assert result.text == "done"

    display = agent.session_manager.get_session("s1")["messages"]
    tool_row = next(m for m in display if m["role"] == "tool")
    assert tool_row["ok"] is False
