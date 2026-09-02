import glob as _glob
import os

from pydantic import BaseModel, Field

from baseagent.tools.base import BaseTool

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", "dist", "build", ".pytest_cache", ".tox",
}


class GlobToolParam(BaseModel):
    pattern: str = Field(description="Glob 模式，如 **/*.py、src/**/*.ts")
    path: str = Field(default=".", description="搜索根目录，默认当前工作目录")


class GlobTool(BaseTool):
    """文件名模式搜索工具"""

    name: str = "glob"
    description: str = (
        "按 glob 模式递归搜索文件，返回相对路径列表。"
        "支持 ** 跨目录通配。自动跳过 .git、node_modules 等目录。"
    )
    param_class = GlobToolParam

    def execute(self, parameters: GlobToolParam) -> str:
        root = os.path.abspath(parameters.path)
        if not os.path.isdir(root):
            return f"错误: 目录不存在 — {root}"

        try:
            raw = _glob.glob(os.path.join(root, parameters.pattern), recursive=True)
        except Exception as e:
            return f"错误: glob 搜索失败 — {e}"

        matches = []
        for m in sorted(raw):
            if not os.path.isfile(m):
                continue
            rel = os.path.relpath(m, root).replace("\\", "/")
            if any(p in _SKIP_DIRS for p in rel.split("/")):
                continue
            matches.append(rel)

        if not matches:
            return f"未找到匹配 '{parameters.pattern}' 的文件（搜索于 {root}）"
        return "\n".join(matches) + f"\n\n共 {len(matches)} 个文件"
