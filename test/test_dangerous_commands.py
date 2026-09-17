"""危险命令黑名单（P1-4 回归，无需 LLM）。

**背景**：仓库里曾有两份互不一致的黑名单——`permission._BASH_HARD_DENY`（8 条明文子串）
与 `bash_tool._DANGEROUS_PATTERNS`（13 条正则）。问题不在"两份"，在于**两份都只在 bash
工具生效**：`verify` 的 `type=command` 同样把模型给的字符串交给 `subprocess(shell=True)`，
却一份都不走，于是成了 bash 的无防护镜像——`mkfs` / `shutdown` / `dd` 在这里一路放行。

本文件锁定四件事：

  1. **并集语义**：旧两份能拦的命令，现在一条都没漏；
  2. **判定一致**：bash 与 verify 逐条给出相同结论（连理由都相同）；
  3. **单一事实源**：`bash_tool` 不再自带第二份表，两处调用同一个函数；
  4. **不误伤**：普通命令照常放行（反向校验，防止靠"什么都拦"刷存在感）。

最后一条**刻意记录已知绕过**——这是护栏不是安全边界，理由见
`kittymind/tools/builtin/_dangerous.py` 的模块文档。
"""

import re

import pytest

from kittymind.tools import permission as permission_module
from kittymind.tools.builtin import _dangerous, bash_tool
from kittymind.tools.builtin._dangerous import (
    DANGEROUS_COMMANDS,
    find_dangerous,
    is_dangerous,
)
from kittymind.tools.builtin.bash_tool import BashTool, BashToolParam
from kittymind.tools.permission import check_permission

# 旧 `permission._BASH_HARD_DENY`：8 条明文子串（原样抄录，用于证明零丢失）
_OLD_PERMISSION_DENY = [
    "rm -rf /",
    "rm -rf \\",
    "sudo rm",
    ":(){ :|:",
    "rd /s /q c:\\",
    "rd /s /q c:/",
    "format c:",
    "del /f /s /q c:\\",
]

# 旧 `bash_tool._DANGEROUS_PATTERNS`：13 条正则各自的最小触发命令
_OLD_BASH_TOOL_DENY = [
    "rm -rf build",  # rm\s+-[^\s]*r
    "rm /",  # rm\s+/
    ":(){ :|:& };:",  # :\(\)\{
    "mkfs.ext4 /dev/sda1",  # mkfs
    "dd if=/dev/zero of=disk.img",  # dd\s+
    "cat x > /dev/sda",  # >\s*/dev/sd
    "chmod -R 777 .",  # chmod\s+-R\s+777
    "sudo rm x",  # sudo\s+rm
    "shutdown -h now",  # shutdown
    "reboot",  # reboot
    "format d:",  # format\s+[a-zA-Z]:
    "del /f x",  # del\s+/[sqf]
    "rd /s backup",  # rd\s+/s
]

_ALL_OLD_ENTRIES = sorted(set(_OLD_PERMISSION_DENY) | set(_OLD_BASH_TOOL_DENY))


class _Recorder:
    """审批回调探针。test/ 下没有 conftest，各文件自带（与 test_permission 同形）。"""

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
def ask() -> _Recorder:
    return _Recorder(allow=True)


# ── 1. 并集语义：零覆盖丢失 ───────────────────────────────────────

@pytest.mark.parametrize("command", _ALL_OLD_ENTRIES)
def test_merged_table_covers_every_old_entry(command):
    """旧两份黑名单能拦的命令，合并后一条都不能漏。"""
    assert find_dangerous(command) is not None, f"覆盖丢失: {command!r}"


# ── 2. 核心 bug：verify 曾是 bash 的无防护镜像 ────────────────────

@pytest.mark.parametrize("command", _ALL_OLD_ENTRIES)
def test_bash_and_verify_deny_identically(command):
    """bash 与 verify 必须逐条一致——这正是 P1-4 的 bug 所在。

    旧代码上：`mkfs` / `shutdown` / `dd` / `reboot` 等只在 bash 侧被
    `bash_tool` 拦截，verify 侧完全放行（返回 None = 允许执行）。
    """
    bash_deny = check_permission("bash", {"command": command}, None)
    verify_deny = check_permission("verify", {"type": "command", "command": command}, None)

    assert bash_deny is not None, f"bash 未拦下 {command!r}"
    assert verify_deny == bash_deny, f"verify 与 bash 判定不一致: {command!r}"


@pytest.mark.parametrize("command", ["mkfs.ext4 /dev/sda1", "shutdown -h now", "reboot", "dd if=x of=y"])
def test_verify_denies_bash_tool_only_patterns(command):
    """单独点名：这几条旧代码里只有 bash_tool 那份正则认得，verify 全放行。"""
    assert check_permission("verify", {"type": "command", "command": command}, None) is not None


# ── 3. 单一事实源 ─────────────────────────────────────────────────

def test_no_second_blacklist_remains():
    """两份旧表都必须消失，否则"漂移"会重新长出来。"""
    assert not hasattr(permission_module, "_BASH_HARD_DENY"), "permission 仍带旧表"
    assert not hasattr(bash_tool, "_DANGEROUS_PATTERNS"), "bash_tool 仍带旧表"
    assert not hasattr(bash_tool, "_DANGEROUS_RE"), "bash_tool 仍带旧正则"


def test_both_call_sites_share_the_same_function():
    """两处调用同一个函数对象，而不是各持一份等价副本。"""
    assert bash_tool.find_dangerous is _dangerous.find_dangerous
    assert permission_module.find_dangerous is _dangerous.find_dangerous


def test_table_is_well_formed():
    patterns = [p for p, _ in DANGEROUS_COMMANDS]
    assert len(set(patterns)) == len(patterns), "存在重复正则"
    assert all(reason.strip() for _, reason in DANGEROUS_COMMANDS), "拒绝理由不能为空"
    for pattern in patterns:
        re.compile(pattern)  # 正则可编译


def test_fast_path_agrees_with_reason_path():
    """`is_dangerous`（联合正则快路径）与 `find_dangerous`（逐条取理由）必须一致。"""
    for command in _ALL_OLD_ENTRIES + ["pytest -q", "ls -la", "git status --short", ""]:
        assert is_dangerous(command) == (find_dangerous(command) is not None), command


def test_reason_is_specific_per_pattern():
    """理由要具体到能直接展示给用户，而不是笼统的"危险命令"。"""
    assert "fork bomb" in find_dangerous(":(){ :|:& };:")
    assert "格式化文件系统" in find_dangerous("mkfs.ext4 /dev/sda1")
    assert "关机" in find_dangerous("shutdown -h now")
    assert "递归删除" in find_dangerous("rm -rf build")


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_is_not_dangerous(value):
    assert find_dangerous(value) is None
    assert is_dangerous(value) is False


# ── 4. 反向校验：不误伤 ───────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "pytest -q",
    "ls -la",
    "git status --short",
    "npm run build",
    "pnpm install",
    "rm build/old.txt",  # 非递归删除 → 该走审批，不是硬拒绝
    "mv a.txt b.txt",
    "python -m http.server 8000",
])
def test_safe_commands_are_not_hard_denied(command):
    """普通命令必须放行——门禁不能靠误伤来"看起来有效"。"""
    assert find_dangerous(command) is None, f"误伤: {command!r}"


def test_non_recursive_delete_goes_to_approval_not_hard_deny(ask):
    """非递归删在该审批的地方审批：硬拒绝只收"明显误触"，不收常规操作。"""
    assert check_permission("bash", {"command": "rm build/old.txt"}, ask) is None
    assert ask.asked and "删除" in ask.last_reason


# ── 工具层自查（execute() 可直接调用，绕过 ToolExecutor）───────────

@pytest.mark.parametrize("command", ["mkfs.ext4 /dev/sda1", "shutdown -h now", "rm -rf build"])
def test_bash_tool_still_self_checks_on_direct_execute(command):
    """`BashTool.execute()` 可被直接调用而不过 ToolExecutor，工具层必须自查。"""
    result = BashTool().execute(BashToolParam(command=command))
    assert not result.ok
    assert "危险命令" in result.content


# ── 5. 刻意记录已知绕过：这是护栏，不是安全边界 ────────────────────

@pytest.mark.parametrize("command", [
    "r''m -rf /",              # 引号拼接绕开 "rm" 字面量
    "rm${IFS}-rf${IFS}/",      # $IFS 代替空格
    "find . -delete",          # 不用 rm 也能删
    "truncate -s 0 notes.txt", # 不用 rm 也能毁文件
])
def test_known_bypasses_are_not_caught(command):
    """**刻意**锁定这些绕过：字符串匹配拦不住它们，模块文档也是这么写的。

    把"缺口"写成断言有两个作用：
      - 阻止读者误以为"危险命令已经被完全挡住了"；
      - 一旦有人真把某条堵上，这条会变红，逼他同步更新 `_dangerous.py`
        文档里的「已知绕过」清单，而不是让文档悄悄失真。
    """
    assert find_dangerous(command) is None, f"{command!r} 已被拦下 —— 请更新模块文档的绕过清单"
