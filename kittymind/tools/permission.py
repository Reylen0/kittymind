"""权限检查纯函数模块。

check_permission(name, args, ask_fn) -> str | None
  返回 None 表示放行；返回拒绝原因字符串表示阻止。

闸门顺序：
  1. 硬拒绝（始终生效，含 ask_fn=None 的 CLI / 子 Agent 路径）
  2. 规则匹配（路径越界、删除操作、系统路径写入、chmod 777）
  3. 用户审批（ask_fn=None 时跳过，直接放行；避免在 worker 线程误触发终端 input）
"""

import os
from pathlib import Path
from typing import Callable, Optional

from .builtin.bash_tool import bash_cwd


# ── 闸门 1：硬拒绝（bash 专用） ────────────────────────────────
_BASH_HARD_DENY: list[tuple[str, str]] = [
    ("rm -rf /",          "递归删除根目录"),
    ("rm -rf \\",         "递归删除根目录"),
    ("sudo rm",           "以 sudo 执行删除"),
    (":(){ :|:",          "fork bomb"),
    ("rd /s /q c:\\",     "递归删除 C 盘"),
    ("rd /s /q c:/",      "递归删除 C 盘"),
    ("format c:",         "格式化 C 盘"),
    ("del /f /s /q c:\\", "递归删除 C 盘文件"),
]


# ── 闸门 2：软规则 ─────────────────────────────────────────────
def _check_path_outside(args: dict) -> bool:
    cwd = bash_cwd.get()
    raw = args.get("path", ".")
    resolved = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(cwd, raw))
    return not _inside_workdir(resolved, cwd)


_RULES: list[tuple[set, object, str]] = [
    (
        {"file_write", "file_edit"},
        lambda args: _check_path_outside(args),
        "写入路径在工作目录之外",
    ),
    (
        {"file_read"},
        lambda args: _check_path_outside(args),
        "读取路径在工作目录之外",
    ),
    (
        {"bash"},
        lambda args: _has_any(args.get("command", ""), ["rm ", "del ", "rmdir ", "Remove-Item"]),
        "命令包含删除操作",
    ),
    (
        {"bash"},
        lambda args: _has_any(args.get("command", ""), [
            "> /etc/", "> /usr/", "> /bin/", "> /boot/",
            "> C:\\Windows\\", "> C:\\System32",
        ]),
        "命令向系统路径写入",
    ),
    (
        {"bash"},
        lambda args: "chmod 777" in args.get("command", ""),
        "命令将权限设置为 777",
    ),
]


def _inside_workdir(path: str, workdir: str) -> bool:
    try:
        Path(os.path.abspath(path)).resolve().relative_to(
            Path(os.path.abspath(workdir)).resolve()
        )
        return True
    except ValueError:
        return False


def _has_any(text: str, patterns: list[str]) -> bool:
    t = text.lower()
    return any(p.lower() in t for p in patterns)


def _short(val: object, limit: int = 80) -> str:
    s = str(val)
    return s[:limit] + "…" if len(s) > limit else s


# ── 默认 ask_fn：CLI 交互 ──────────────────────────────────────
def _cli_ask(tool_name: str, args: dict, reason: str) -> bool:
    display = {k: (_short(v, 100) if k != "content" else f"<{len(str(v))} chars>") for k, v in args.items()}
    print(f"\n[WARN] {reason}")
    print(f"    tool : {tool_name}")
    for k, v in display.items():
        print(f"    {k}: {v}")
    try:
        return input("    允许执行？[y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ── 公共接口 ─────────────────────────────────────────────────

def check_permission(
    name: str,
    args: dict,
    ask_fn: Optional[Callable[[str, dict, str], bool]] = None,
) -> Optional[str]:
    """检查工具调用权限。返回 None 表示放行；返回字符串表示拒绝原因。

    闸门1（硬拒绝）始终生效，含 ask_fn=None 的路径。
    闸门2/3 仅当名称匹配规则时触发；ask_fn=None 时跳过用户审批直接放行。
    """
    # 闸门 1：硬拒绝
    if name == "bash":
        cmd = args.get("command", "").lower()
        for pattern, reason in _BASH_HARD_DENY:
            if pattern.lower() in cmd:
                return f"硬拒绝: {reason}"

    # 闸门 2 + 3：规则 → 用户审批
    for tool_names, check_fn, reason in _RULES:
        if name not in tool_names:
            continue
        try:
            triggered = check_fn(args)
        except Exception:
            triggered = False
        if not triggered:
            continue
        # ask_fn=None → 跳过审批直接放行（不阻断，但也不触发终端 input）
        if ask_fn is None:
            break
        if not ask_fn(name, args, reason):
            return "用户拒绝执行"
        break

    return None
