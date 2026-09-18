import glob as _glob
import os

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ._paths import is_inside, resolve_path
from ...config import cfg

_SKIP_DIRS = cfg.SKIP_DIRS


class GlobToolParam(BaseModel):
    pattern: str = Field(description="Glob 模式，如 **/*.py、src/**/*.ts")
    path: str = Field(default=".", description="搜索根目录，默认当前工作目录")


class GlobTool(BaseTool):
    name: str = "glob"
    description: str = (
        "按 glob 模式递归搜索文件，返回相对路径列表。"
        "支持 ** 跨目录通配。自动跳过 .git、node_modules 等目录。"
    )
    param_class = GlobToolParam
    is_concurrency_safe = True  # 只读，可同批并行

    def execute(self, parameters: GlobToolParam) -> ToolResult:
        root = resolve_path(parameters.path)
        if not os.path.isdir(root): return ToolResult(False, f"错误: 目录不存在 — {root}")
        # 掐掉根锚点：os.path.join(root, "/etc/*") 会直接丢弃 root，前导分隔符等于
        # "从盘符根开始找"，必须去掉。
        pattern = (parameters.pattern or "*").strip().lstrip("/\\") or "*"
        try:
            raw = _glob.glob(os.path.join(root, pattern), recursive=True)
        except Exception as e:
            return ToolResult(False, f"错误: glob 搜索失败 — {e}")
        matches = []
        for m in sorted(raw):
            if not os.path.isfile(m): continue
            # 兜底：pattern 里带 ../ 时结果会落到 root 之外，一律丢弃
            if not is_inside(m, root): continue
            rel = os.path.relpath(m, root).replace("\\", "/")
            if any(p in _SKIP_DIRS for p in rel.split("/")): continue
            matches.append(rel)
        if not matches:
            return ToolResult(True, f"未找到匹配 '{parameters.pattern}' 的文件（搜索于 {root}）")
        return ToolResult(True, "\n".join(matches) + f"\n\n共 {len(matches)} 个文件")
