import os

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ._paths import resolve_path
from ...config import cfg

# SKIP_DIRS 不在 _OVERRIDABLE 里，是常量而非可覆盖配置，故模块级捕获无害。
_SKIP_DIRS  = cfg.SKIP_DIRS


class LsToolParam(BaseModel):
    path: str = Field(default=".", description="要列出的目录路径，默认当前工作目录")
    depth: int = Field(
        default_factory=lambda: cfg.LS_DEPTH, description="目录树展开深度，默认 2",
    )
    show_hidden: str = Field(default="false", description="是否显示隐藏文件，true 或 false")


class LsTool(BaseTool):
    name: str = "ls"
    description: str = (
        "以树形结构列出目录内容，显示文件大小。"
        "自动跳过 .git、node_modules、__pycache__ 等目录。"
    )
    param_class = LsToolParam
    is_concurrency_safe = True  # 只读，可同批并行

    def execute(self, parameters: LsToolParam) -> ToolResult:
        root = resolve_path(parameters.path)
        if not os.path.exists(root): return ToolResult(False, f"错误: 路径不存在 — {root}")
        if os.path.isfile(root): return ToolResult(True, f"{root}  ({_fmt(os.path.getsize(root))})")
        show_hidden = parameters.show_hidden.lower() == "true"
        lines = [root + "/"]
        counter = [0]
        self._walk(root, root, parameters.depth, 0, show_hidden, lines, counter)
        max_entries = cfg.LS_MAX_ENTRIES
        if counter[0] >= max_entries:
            lines.append(f"  ... (已截断至 {max_entries} 项)")
        return ToolResult(True, "\n".join(lines))

    def _walk(self, root, current, max_depth, depth, show_hidden, lines, counter):
        max_entries = cfg.LS_MAX_ENTRIES
        if depth >= max_depth or counter[0] >= max_entries: return
        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            lines.append("  " * (depth + 1) + "[权限不足]")
            return
        indent = "  " * (depth + 1)
        for d in [e for e in entries if os.path.isdir(os.path.join(current, e))]:
            if counter[0] >= max_entries or d in _SKIP_DIRS: continue
            if not show_hidden and d.startswith("."): continue
            lines.append(f"{indent}{d}/")
            counter[0] += 1
            self._walk(root, os.path.join(current, d), max_depth, depth + 1, show_hidden, lines, counter)
        for fn in [e for e in entries if os.path.isfile(os.path.join(current, e))]:
            if counter[0] >= max_entries: break
            if not show_hidden and fn.startswith("."): continue
            lines.append(f"{indent}{fn}  ({_fmt(os.path.getsize(os.path.join(current, fn)))})")
            counter[0] += 1


def _fmt(n: int) -> str:
    if n < 1024: return f"{n}B"
    if n < 1024 ** 2: return f"{n/1024:.1f}KB"
    return f"{n/1024**2:.1f}MB"
