"""权限闸门单元测试（无需 LLM）。

新覆盖：git（写历史 / 破坏性附加参数 / 仓库越界）与 clipboard（读隐私外发 / 写覆盖）。
同时锁定既有语义不被本次改动破坏：ask_fn=None 时软规则静默放行、硬拒绝始终生效、
工作区外路径读写仍触发审批。
"""

import json

import pytest

from kittymind.tools.builtin.bash_tool import bash_cwd
from kittymind.tools.builtin.clipboard_tool import ClipboardTool
from kittymind.tools.builtin.git_tool import GitTool
from kittymind.tools.builtin.verify_tool import VerifyTool
from kittymind.tools.executor import ToolExecutor, TurnContext
from kittymind.tools.permission import (
    _GIT_DANGEROUS_FLAGS,
    _has_flag,
    check_permission,
)
from kittymind.tools.registry import ToolRegistry


class Recorder:
    """记录审批请求；allow 决定放行或拒绝。"""

    def __init__(self, allow: bool = True):
        self.allow = allow
        self.calls: list[tuple[str, dict, str]] = []

    def __call__(self, tool_name: str, args: dict, reason: str) -> bool:
        self.calls.append((tool_name, args, reason))
        return self.allow

    @property
    def asked(self) -> bool:
        return bool(self.calls)

    @property
    def last_reason(self) -> str:
        return self.calls[-1][2]


@pytest.fixture
def ask() -> Recorder:
    return Recorder(allow=True)


@pytest.fixture
def workdir(tmp_path):
    """把 bash_cwd 固定到 tmp_path，保证路径类规则可预期。"""
    token = bash_cwd.set(str(tmp_path))
    yield tmp_path
    bash_cwd.reset(token)


# ── git ──────────────────────────────────────────────────────────

def test_git_commit_asks(ask):
    assert check_permission("git", {"action": "commit", "message": "feat: x"}, ask) is None
    assert ask.asked and "commit" in ask.last_reason


def test_git_commit_denied():
    deny = Recorder(allow=False)
    result = check_permission("git", {"action": "commit", "message": "x"}, deny)
    assert result == "用户拒绝执行"


def test_git_commit_without_message_not_asked(ask):
    """message 为空时 git 工具退化为 status，不应弹审批。"""
    assert check_permission("git", {"action": "commit", "message": "   "}, ask) is None
    assert not ask.asked


@pytest.mark.parametrize("action", ["status", "diff", "log", "branch", "show"])
def test_git_readonly_not_asked(ask, action):
    assert check_permission("git", {"action": action}, ask) is None
    assert not ask.asked


def test_git_add_not_asked(ask):
    """add 只改索引、可 unstage，刻意不拦截（决策记录，非遗漏）。"""
    assert check_permission("git", {"action": "add", "files": "a.py"}, ask) is None
    assert not ask.asked


@pytest.mark.parametrize("extra", [
    "--amend", "--force", "--hard", "-f", "-D",
    "push --force-with-lease", "commit --no-verify",
])
def test_git_dangerous_extra_asks(ask, extra):
    assert check_permission("git", {"action": "diff", "extra": extra}, ask) is None
    assert ask.asked, f"extra={extra!r} 未触发审批"


def test_git_extra_long_option_not_misjudged(ask):
    """'--file=x' 不应被短选项 '-f' 子串命中（_has_flag 按词匹配）。"""
    assert check_permission("git", {"action": "diff", "extra": "--file=x"}, ask) is None
    assert not ask.asked


def test_git_workdir_outside_asks(workdir, ask):
    outside = str(workdir.parent)
    assert check_permission("git", {"action": "status", "workdir": outside}, ask) is None
    assert ask.asked and "工作目录" in ask.last_reason


def test_git_workdir_inside_not_asked(workdir, ask):
    assert check_permission("git", {"action": "status", "workdir": str(workdir)}, ask) is None
    assert not ask.asked


# ── clipboard ────────────────────────────────────────────────────

def test_clipboard_write_asks(ask):
    assert check_permission("clipboard", {"action": "write", "content": "hi"}, ask) is None
    assert ask.asked and "覆盖" in ask.last_reason


def test_clipboard_read_asks(ask):
    assert check_permission("clipboard", {"action": "read"}, ask) is None
    assert ask.asked and "敏感" in ask.last_reason


@pytest.mark.parametrize("action", ["", "erase", None])
def test_clipboard_unknown_action_not_asked(ask, action):
    """非法 action 交给工具自己报错，权限层不做多余拦截。"""
    assert check_permission("clipboard", {"action": action}, ask) is None
    assert not ask.asked


# ── 既有语义回归 ─────────────────────────────────────────────────

def test_soft_rules_skipped_when_no_ask_fn(workdir):
    """CLI / 无 ask_fn 路径：软规则静默放行（保持原设计，避免 worker 线程卡 input）。"""
    assert check_permission("git", {"action": "commit", "message": "x"}, None) is None
    assert check_permission("clipboard", {"action": "write"}, None) is None
    assert check_permission("clipboard", {"action": "read"}, None) is None


def test_hard_deny_still_applies_without_ask_fn():
    assert check_permission("bash", {"command": "rm -rf /"}, None) is not None


def test_file_write_outside_still_asks(workdir, ask):
    outside = str(workdir.parent / "x.txt")
    assert check_permission("file_write", {"path": outside}, ask) is None
    assert ask.asked and "工作目录之外" in ask.last_reason


def test_file_write_inside_not_asked(workdir, ask):
    assert check_permission("file_write", {"path": "a.txt"}, ask) is None
    assert not ask.asked


def test_unrelated_tool_ignored(ask):
    assert check_permission("get_current_time", {}, ask) is None
    assert not ask.asked


# ── 执行器集成：规则确实接在工具调用链上 ─────────────────────────

def _run_tool(tool, args: dict, ask_fn):
    """经 ToolExecutor 走一次完整闸门（args 走 JSON 序列化，模拟真实 tool_call）。"""
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, ask_fn=ask_fn)
    return executor.execute(
        {"id": "c1", "function": {"name": tool.name, "arguments": json.dumps(args)}},
        TurnContext(session_id="s1"),
    )


def test_executor_blocks_git_commit_when_denied():
    deny = Recorder(allow=False)
    out = _run_tool(GitTool(), {"action": "commit", "message": "x"}, deny)
    assert out["role"] == "tool"
    assert "Permission denied" in out["content"]
    assert "commit" in deny.last_reason


def test_executor_blocks_clipboard_write_when_denied():
    deny = Recorder(allow=False)
    out = _run_tool(ClipboardTool(), {"action": "write", "content": "hi"}, deny)
    assert "Permission denied" in out["content"]
    assert "覆盖" in deny.last_reason


def test_executor_allows_git_status(workdir, ask):
    out = _run_tool(GitTool(), {"action": "status", "workdir": str(workdir)}, ask)
    assert not ask.asked, "只读 git 操作不应触发审批"
    assert "Permission denied" not in out["content"]


# ── verify：与 bash 共用硬拒绝，且执行命令必须过审批（P0 回归） ───

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "sudo rm -rf /usr",
    "format c:",
    "del /f /s /q c:\\",
])
def test_verify_shares_hard_deny_with_bash(command):
    """verify(type=command) 同样执行模型给的任意命令，硬拒绝不能漏。"""
    bash_deny = check_permission("bash", {"command": command}, None)
    verify_deny = check_permission("verify", {"type": "command", "command": command}, None)
    assert bash_deny is not None, f"bash 未拦下 {command!r}"
    assert verify_deny is not None, f"verify 未拦下 {command!r}（等同 bash 的无防护镜像）"


def test_verify_command_asks(ask):
    assert check_permission("verify", {"type": "command", "command": "pytest -q"}, ask) is None
    assert ask.asked and "任意命令" in ask.last_reason


def test_verify_probe_not_asked(ask):
    """probe 只做连通性探测，不该弹审批。"""
    assert check_permission("verify", {"type": "probe", "target": "127.0.0.1:8000"}, ask) is None
    assert not ask.asked


def test_verify_empty_command_not_asked(ask):
    assert check_permission("verify", {"type": "command", "command": "  "}, ask) is None
    assert not ask.asked


def test_verify_delete_command_uses_specific_reason(ask):
    """删除类命令应命中更具体的理由，而不是兜底那条。"""
    assert check_permission("verify", {"type": "command", "command": "rm -rf build"}, ask) is None
    assert ask.asked and "删除" in ask.last_reason


def test_executor_blocks_verify_when_denied():
    deny = Recorder(allow=False)
    out = _run_tool(VerifyTool(), {"type": "command", "command": "echo hi"}, deny)
    assert "Permission denied" in out["content"]
    assert "任意命令" in deny.last_reason


# ── _has_flag 精度 ───────────────────────────────────────────────

def test_has_flag_word_boundary():
    assert _has_flag("-f", _GIT_DANGEROUS_FLAGS) is True
    assert _has_flag("--file=x", _GIT_DANGEROUS_FLAGS) is False
    assert _has_flag("--formatted", _GIT_DANGEROUS_FLAGS) is False
    assert _has_flag("push --force-with-lease", _GIT_DANGEROUS_FLAGS) is True
    assert _has_flag("diff --stat", _GIT_DANGEROUS_FLAGS) is False
    assert _has_flag("", _GIT_DANGEROUS_FLAGS) is False
