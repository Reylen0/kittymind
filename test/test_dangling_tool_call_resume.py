"""悬空 tool_call 幂等恢复 + `resume_conversation` 的特征测试（ARCHITECTURE.md Phase 34 ③）。

核心主张：续跑一个"进程恰好死在执行工具调用期间"的会话时——
  - 幂等只读工具（`guardrails.IDEMPOTENT_TOOLS`）直接重新执行一次，拿确定结果；
  - 变更类工具（`guardrails.MUTATING_TOOLS`）绝不自动重放，合成一条"状态未知"
    结果，把判断权交还模型；
  - 续跑不追加新用户消息，恢复结果落库后把循环正常交还给 LLM 继续。

与 `test_session.py::test_find_dangling_tool_calls_*` 的分工：那边纯测
"怎么判断悬空"（manager 层，不碰 LLM/工具执行），这里测"悬空判断之后怎么
处理"（agent 层，端到端跑一遍 `resume_conversation`）。
"""

from pydantic import BaseModel

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.core.llm_response import StreamEvent
from kittymind.session.manager import SessionManager
from kittymind.tools.base import BaseTool, ToolResult


class _EmptyParams(BaseModel):
    pass


class _FakeIdempotentTool(BaseTool):
    """名字对上 `guardrails.IDEMPOTENT_TOOLS`（如 file_read）即可触发"直接重跑"分支。"""

    name = "file_read"
    description = "fake idempotent tool"
    param_class = _EmptyParams
    is_concurrency_safe = True

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, parameters):
        self.calls += 1
        return ToolResult(True, "重新执行后的真实结果")


class _FakeMutatingTool(BaseTool):
    """名字对上 `guardrails.MUTATING_TOOLS`（如 bash）即可触发"绝不重放"分支。"""

    name = "bash"
    description = "fake mutating tool"
    param_class = _EmptyParams
    is_concurrency_safe = False

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, parameters):
        self.calls += 1
        return ToolResult(True, "这不该被调用到")


class _ScriptedLLM:
    model = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)

    async def async_stream_with_tools(self, messages, tools=None, **kwargs):
        text, tool_calls = self.steps.pop(0)
        if text:
            yield StreamEvent(type="text_delta", delta=text)
        yield StreamEvent(type="tool_calls_done", tool_calls=tool_calls)


def _tc(call_id: str, name: str = "bash") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _make_agent(tmp_path, steps, tools):
    sm = SessionManager(db_path=tmp_path / "s.db")
    agent = KittyAgent(
        name="t", llm=_ScriptedLLM(steps), system_prompt="sp", tools=tools,
        session_manager=sm, aux_llm=None, interactive=False,
    )
    return agent, sm


async def _resume(agent, session_id):
    return "".join([c async for c in agent.resume_conversation(session_id)])


async def test_resume_reexecutes_idempotent_dangling_call(tmp_path):
    tool = _FakeIdempotentTool()
    agent, sm = _make_agent(tmp_path, steps=[("done", [])], tools=[tool])
    sid = sm.create_session(title="t")
    sm.append_turn(sid, [
        {"role": "user", "content": "读个文件"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "file_read")]},
        # 没有对应的 tool 结果行——模拟进程死在执行这批工具调用期间
    ])

    out = await _resume(agent, sid)

    assert out == "done"
    assert tool.calls == 1  # 幂等工具确实被重新执行了一次

    display = sm.get_session(sid)["messages"]
    tool_row = next(m for m in display if m["role"] == "tool")
    assert tool_row["content"] == "重新执行后的真实结果"
    assert tool_row["ok"] is True


async def test_resume_does_not_replay_mutating_dangling_call(tmp_path):
    tool = _FakeMutatingTool()
    agent, sm = _make_agent(tmp_path, steps=[("done", [])], tools=[tool])
    sid = sm.create_session(title="t")
    sm.append_turn(sid, [
        {"role": "user", "content": "跑个命令"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "bash")]},
    ])

    out = await _resume(agent, sid)

    assert out == "done"
    assert tool.calls == 0  # 绝不自动重放

    display = sm.get_session(sid)["messages"]
    tool_row = next(m for m in display if m["role"] == "tool")
    assert "执行状态未知" in tool_row["content"]
    assert "只读工具自行核实" in tool_row["content"]
    assert tool_row["ok"] is False


async def test_resume_handles_mixed_idempotent_and_mutating_batch_in_order(tmp_path):
    """同一批里既有幂等又有变更类：各自按分类处理，结果顺序与声明序一致。"""
    idempotent, mutating = _FakeIdempotentTool(), _FakeMutatingTool()
    agent, sm = _make_agent(tmp_path, steps=[("done", [])], tools=[idempotent, mutating])
    sid = sm.create_session(title="t")
    sm.append_turn(sid, [
        {"role": "user", "content": "读文件再跑命令"},
        {"role": "assistant", "content": None,
         "tool_calls": [_tc("c1", "file_read"), _tc("c2", "bash")]},
    ])

    out = await _resume(agent, sid)

    assert out == "done"
    assert idempotent.calls == 1
    assert mutating.calls == 0

    display = sm.get_session(sid)["messages"]
    tool_rows = [m for m in display if m["role"] == "tool"]
    assert len(tool_rows) == 2
    # tool_call_id 不在展示视图里，但顺序必须跟声明序一致：c1(file_read) 先于 c2(bash)
    assert tool_rows[0]["content"] == "重新执行后的真实结果"
    assert tool_rows[0]["ok"] is True
    assert "执行状态未知" in tool_rows[1]["content"]
    assert tool_rows[1]["ok"] is False


async def test_resume_without_any_dangling_call_just_continues(tmp_path):
    """没有悬空调用时（如进程死在流式产出最终文本期间，那部分文本从未落库）：
    续跑等价于让 LLM 看着现有历史继续说话。
    """
    agent, sm = _make_agent(tmp_path, steps=[("接着说完", [])], tools=[])
    sid = sm.create_session(title="t")
    sm.append_turn(sid, [{"role": "user", "content": "你好"}])

    out = await _resume(agent, sid)

    assert out == "接着说完"
    display = sm.get_session(sid)["messages"]
    assert [m["role"] for m in display] == ["user", "assistant"]


async def test_resume_clears_turn_in_progress_on_completion(tmp_path):
    """续跑本身也是一次"轮次"：正常结束要清掉崩溃检测标记（Phase 34 ②）。"""
    tool = _FakeIdempotentTool()
    agent, sm = _make_agent(tmp_path, steps=[("done", [])], tools=[tool])
    sid = sm.create_session(title="t")
    sm.mark_turn_started(sid)  # 模拟"上一个进程死前就是这个状态"
    sm.append_turn(sid, [
        {"role": "user", "content": "读个文件"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "file_read")]},
    ])

    await _resume(agent, sid)

    assert sm.get_session_state(sid)["turn_in_progress"] is False
