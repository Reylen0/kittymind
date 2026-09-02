"""权限管线 —— 工具执行前的三道闸门。

闸门 1：硬拒绝列表   命中 → 直接拒绝
闸门 2：规则匹配     命中 → 进入闸门 3
闸门 3：用户审批     由 ask_fn 决定允许或拒绝（默认 CLI input，GUI 场景可注入替换）

三道都未命中 → 放行。
"""

import json
import os
from pathlib import Path
from typing import Callable, Optional

from baseagent.tools.executor import ToolExecutor
from baseagent.tools.registry import ToolRegistry

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
_RULES: list[tuple[set, object, str]] = [
    (
        {"file_write", "file_edit"},
        lambda args, wd: not _inside_workdir(args.get("path", "."), wd),
        "写入路径在工作目录之外",
    ),
    (
        {"file_read"},
        lambda args, wd: not _inside_workdir(args.get("path", "."), wd),
        "读取路径在工作目录之外",
    ),
    (
        {"bash"},
        lambda args, _: _has_any(args.get("command", ""), ["rm ", "del ", "rmdir ", "Remove-Item"]),
        "命令包含删除操作",
    ),
    (
        {"bash"},
        lambda args, _: _has_any(args.get("command", ""), [
            "> /etc/", "> /usr/", "> /bin/", "> /boot/",
            "> C:\\Windows\\", "> C:\\System32",
        ]),
        "命令向系统路径写入",
    ),
    (
        {"bash"},
        lambda args, _: "chmod 777" in args.get("command", ""),
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


# ─────────────────────────────────────────────────────────────
# PermissionToolExecutor
# ─────────────────────────────────────────────────────────────

# 默认 ask_fn：CLI 交互（适合终端使用）
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


class PermissionToolExecutor(ToolExecutor):
    """在工具执行前插入三道权限闸门。

    ask_fn 签名：(tool_name: str, args: dict, reason: str) -> bool
    默认使用 CLI input；桌面 GUI 场景注入一个基于 WebSocket 的异步版本。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        workdir: str = ".",
        ask_fn: Optional[Callable[[str, dict, str], bool]] = None,
    ):
        super().__init__(registry)
        self.workdir = os.path.abspath(workdir)
        self._ask_fn = ask_fn or _cli_ask
        self._last_call_key: Optional[str] = None

    def begin_turn(self) -> None:
        """每轮对话开始前重置重复调用检测。"""
        self._last_call_key = None

    def execute(self, tool_call: dict) -> dict:
        tool_call_id = tool_call.get("id", "")
        function = tool_call.get("function", {})
        name = function.get("name", "")
        try:
            args = json.loads(function.get("arguments", "{}") or "{}")
        except Exception:
            args = {}

        # ── 闸门 0：重复调用检测 ────────────────────────────────
        call_key = f"{name}:{json.dumps(args, sort_keys=True)}"
        if call_key == self._last_call_key:
            return self._denied(
                tool_call_id,
                f"重复调用拦截：工具 '{name}' 以完全相同的参数被连续调用两次。",
            )
        self._last_call_key = call_key

        # ── 闸门 1：硬拒绝 ──────────────────────────────────────
        if name == "bash":
            cmd = args.get("command", "").lower()
            for pattern, reason in _BASH_HARD_DENY:
                if pattern.lower() in cmd:
                    return self._denied(tool_call_id, f"硬拒绝: {reason}")

        # ── 闸门 2 + 3：规则 → 用户审批 ─────────────────────────
        for tool_names, check_fn, reason in _RULES:
            if name not in tool_names:
                continue
            try:
                triggered = check_fn(args, self.workdir)
            except Exception:
                triggered = False
            if not triggered:
                continue
            if not self._ask_fn(name, args, reason):
                return self._denied(tool_call_id, "用户拒绝执行")
            break

        return super().execute(tool_call)

    @staticmethod
    def _denied(tool_call_id: str, reason: str) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": f"Permission denied: {reason}"}
