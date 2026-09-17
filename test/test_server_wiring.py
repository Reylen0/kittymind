"""server 装配回归测试（无需 LLM / 不触碰用户数据目录）。

守住一条 P0 回归线：server/app.py 的 build_agent 必须装配完整内置工具集。
曾出现只注册 7 个工具（缺 file_edit/glob/grep/ls/git/screenshot/clipboard），
导致 Electron 生产路径缺一半能力，而 CLI（chat_async.py）反而是全量。
"""

from unittest.mock import MagicMock

import server.app as app_mod


BUILTIN_TOOL_NAMES = {
    "get_current_time", "ls", "glob", "grep",
    "file_read", "file_write", "file_edit",
    "git", "bash", "screenshot", "clipboard",
    "write_memory", "verify",
}


def _build_isolated_agent(monkeypatch):
    """隔离外部依赖（LLM / 会话库 / 工作区 / 记忆）后装配 agent。

    必须在事件循环内调用——build_agent 会取 running loop 传给 TaskTool。
    """
    monkeypatch.setattr(app_mod, "BaseAgentLLM", lambda *a, **k: MagicMock(model="fake"))
    monkeypatch.setattr(app_mod, "SessionManager", lambda *a, **k: MagicMock())
    monkeypatch.setattr(app_mod, "WorkspaceManager", lambda *a, **k: MagicMock())
    monkeypatch.setattr(app_mod, "MemoryStore", lambda *a, **k: MagicMock())
    return app_mod.build_agent(MagicMock())


async def test_build_agent_registers_all_builtin_tools(monkeypatch):
    agent = _build_isolated_agent(monkeypatch)
    names = {t.name for t in agent.tools}
    missing = BUILTIN_TOOL_NAMES - names
    assert not missing, f"server 端缺失工具：{sorted(missing)}"
    assert names == BUILTIN_TOOL_NAMES | {"task"}


async def test_task_tool_sub_tools_are_complete(monkeypatch):
    """子 Agent 的工具集同样不能缺——否则委派出去的子任务做不了文件编辑/搜索。"""
    agent = _build_isolated_agent(monkeypatch)
    task_tool = next(t for t in agent.tools if t.name == "task")
    sub_names = {t.name for t in task_tool._sub_tools}
    missing = BUILTIN_TOOL_NAMES - sub_names
    assert not missing, f"子 Agent 缺失工具：{sorted(missing)}"


async def test_tool_schemas_match_registry(monkeypatch):
    """下发给模型的 schema 数量应与注册工具数一致（防止漏注册或 schema 生成失败）。"""
    agent = _build_isolated_agent(monkeypatch)
    assert len(agent.tool_registry.get_schemas()) == len(agent.tools)
