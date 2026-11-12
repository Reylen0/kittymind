"""崩溃检测标记的特征测试（ARCHITECTURE.md Phase 34 ②）。

核心主张：`turn_in_progress` 在轮次开始（用户消息提交后）置位，正常结束/
显式取消都在 `run_conversation` 的 `finally` 里清除。

**一个容易踩的坑，也是本文件要钉住的边界**：Python 的 `finally` 在任何能被
解释器正常处理的异常路径上都会执行——包括 LLM 调用失败这类"正常的错误"
（已经有 `agent.error` 事件明确报告给用户，不是"进程神秘消失"）。所以**不能**
用"让 LLM 抛异常"来模拟真正的进程崩溃并断言标记留下来：异常会被正常捕获、
正常解包栈帧、正常跑到 `finally`，标记一样会被清掉——这是正确行为，不是 bug
（真正的进程被杀死是 Python 完全来不及跑任何代码，`finally` 根本没有机会
执行，这一点不可能在同一个解释器内用抛异常来复现）。

所以这里测的是真正能被断言的不变式：
  1. 标记在用户消息提交之后、轮次完成之前就已经落库为 True
     （用一个会在工具执行期间回读状态的执行器替身，验证"此刻读库"看到的
     就是 True——这正是进程此刻被杀会留下的状态）；
  2. 正常收尾会清掉它；
  3. 显式模拟"上一个进程死前就是这个状态"（直接调 mark_turn_started 而不
     真的跑一轮），下一轮正常对话照常清掉它，不会永久卡死；
  4. 子 Agent（session_id=None）完全不碰这个标记。
"""

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.core.llm_response import StreamEvent
from kittymind.session.manager import SessionManager


def _tool_call(name: str = "noop", call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    model = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        text, tool_calls = self.steps.pop(0)
        if text:
            yield StreamEvent(type="text_delta", delta=text)
        yield StreamEvent(type="tool_calls_done", tool_calls=tool_calls)


class _SpyingExecutor:
    """不执行真实工具，只按协议回灌结果；每次被调用时顺带回读会话状态。

    回读发生在"轮次仍在进行中"的那一刻——如果进程此刻真的被杀死，数据库里
    看到的就是这次回读看到的状态，这正是测试要验证的东西。
    """

    def __init__(self, session_manager, session_id):
        self._sm = session_manager
        self._sid = session_id
        self.seen_states: list[dict] = []

    async def execute(self, tool_call, session_id=None, guardrail_state=None):
        self.seen_states.append(self._sm.get_session_state(self._sid))
        name = tool_call["function"]["name"]
        return {"role": "tool", "tool_call_id": tool_call.get("id"), "content": f"ran:{name}"}

    async def execute_batch(self, tool_calls, session_id=None, guardrail_state=None):
        return [await self.execute(tc, session_id, guardrail_state) for tc in tool_calls]


def _make_agent(tmp_path, steps):
    sm = SessionManager(db_path=tmp_path / "s.db")
    agent = KittyAgent(
        name="t", llm=_ScriptedLLM(steps), system_prompt="sp", tools=[],
        session_manager=sm, aux_llm=None, interactive=False,
    )
    return agent, sm


async def test_flag_is_true_while_the_turn_is_still_running(tmp_path):
    """工具执行期间回读状态：此刻库里的 turn_in_progress 必须已经是 True——
    这正是"如果进程这一刻被杀死"会留下的状态。
    """
    agent, sm = _make_agent(tmp_path, steps=[
        ("", [_tool_call("a")]),
        ("", [_tool_call("b")]),
        ("done", []),
    ])
    spy = _SpyingExecutor(sm, "s1")
    agent.tool_executor = spy

    result = await agent.run("s1", "跑两步工具后结束")

    assert result.text == "done"
    assert len(spy.seen_states) == 2
    assert all(s["turn_in_progress"] is True for s in spy.seen_states)
    assert all(s["turn_started_at"] for s in spy.seen_states)


async def test_normal_completion_clears_the_flag(tmp_path):
    """正常收尾：`finally` 清掉标记，下次启动不会误报"异常中断"。"""
    agent, sm = _make_agent(tmp_path, steps=[("", [_tool_call("noop")]), ("done", [])])
    agent.tool_executor = _SpyingExecutor(sm, "s1")

    result = await agent.run("s1", "跑个工具后结束")
    assert result.text == "done"

    state = sm.get_session_state("s1")
    assert state["turn_in_progress"] is False
    assert state["turn_started_at"] is None


async def test_next_turn_overwrites_a_stale_flag_left_by_a_dead_process(tmp_path):
    """模拟"上一个进程死前这个会话就卡在 turn_in_progress=True"：直接写状态，
    不真的跑一轮（跑一轮必然会清掉它，没法模拟真崩溃留下的痕迹）。下一轮
    正常对话要能照常清掉它，不会被旧标记卡死。
    """
    agent, sm = _make_agent(tmp_path, steps=[("done", [])])
    sm.create_session(title="t", session_id="s1")
    sm.mark_turn_started("s1")
    assert sm.get_session_state("s1")["turn_in_progress"] is True  # 模拟的"崩溃残留"

    agent.tool_executor = _SpyingExecutor(sm, "s1")
    result = await agent.run("s1", "新的一轮，正常说完")

    assert result.text == "done"
    assert sm.get_session_state("s1")["turn_in_progress"] is False


async def test_sub_agent_turns_do_not_touch_the_flag(tmp_path):
    """子 Agent（session_id=None）完全不碰这个标记——它只对真实会话有意义。"""
    agent, _sm = _make_agent(tmp_path, steps=[("done", [])])
    agent.tool_executor = _SpyingExecutor(agent.session_manager, "unused")

    out = "".join([
        c async for c in agent.run_conversation(session_id=None, input_text="hi", root=False)
    ])

    assert out == "done"  # 跑通即可：没有 session_id 时 mark_turn_* 全程不触发
