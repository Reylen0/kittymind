import fnmatch
import os
import re

from pydantic import BaseModel, Field

from baseagent.tools.base import BaseTool

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", "dist", "build", ".pytest_cache", ".tox",
}
_BINARY_EXTS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".whl", ".egg",
    ".db", ".sqlite", ".lock",
}
_MAX_RESULTS = 100
_MAX_FILE_SIZE = 1_000_000


class GrepToolParam(BaseModel):
    pattern: str = Field(description="搜索的正则表达式或普通字符串")
    path: str = Field(default=".", description="搜索目录或单个文件路径，默认当前工作目录")
    file_pattern: str = Field(default="*", description="文件名过滤 glob 模式，如 *.py")
    context_lines: int = Field(default=2, description="匹配行前后各显示的上下文行数")
    case_sensitive: str = Field(default="true", description="是否区分大小写，true 或 false")


class GrepTool(BaseTool):
    """文件内容搜索工具"""

    name: str = "grep"
    description: str = (
        "在文件内容中搜索正则表达式，返回匹配行及上下文。"
        "支持跨多个文件搜索，可通过 file_pattern 过滤文件类型。"
    )
    param_class = GrepToolParam

    def execute(self, parameters: GrepToolParam) -> str:
        flags = 0 if parameters.case_sensitive.lower() == "true" else re.IGNORECASE
        try:
            regex = re.compile(parameters.pattern, flags)
        except re.error as e:
            return f"错误: 无效的正则表达式 — {e}"

        target = os.path.abspath(parameters.path)
        if os.path.isfile(target):
            files, base_dir = [target], os.path.dirname(target)
        elif os.path.isdir(target):
            base_dir = target
            files = self._collect_files(target, parameters.file_pattern)
        else:
            return f"错误: 路径不存在 — {target}"

        results = []
        for filepath in files:
            if len(results) >= _MAX_RESULTS:
                results.append(f"... 结果超过 {_MAX_RESULTS} 条，已截断")
                break
            results.extend(self._search_file(filepath, regex, base_dir, parameters.context_lines))

        if not results:
            return f"未找到匹配 '{parameters.pattern}' 的内容"
        total = sum(1 for r in results if not r.startswith("..."))
        return "\n\n".join(results) + f"\n\n共 {total} 处匹配"

    def _collect_files(self, root: str, file_pattern: str) -> list[str]:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for fn in sorted(filenames):
                if fnmatch.fnmatch(fn, file_pattern):
                    if os.path.splitext(fn)[1].lower() not in _BINARY_EXTS:
                        files.append(os.path.join(dirpath, fn))
        return files

    def _search_file(self, filepath: str, regex, base_dir: str, ctx: int) -> list[str]:
        if os.path.getsize(filepath) > _MAX_FILE_SIZE:
            return []
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            return []

        rel = os.path.relpath(filepath, base_dir).replace("\\", "/")
        blocks: list[str] = []
        seen: set[int] = set()
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            start, end = max(0, i - ctx), min(len(lines), i + ctx + 1)
            block_lines = []
            for j in range(start, end):
                if j in seen and j != i:
                    continue
                seen.add(j)
                marker = ">" if j == i else " "
                block_lines.append(f"  {marker} {j+1:4d}: {lines[j].rstrip()}")
            blocks.append(f"{rel}:{i+1}:\n" + "\n".join(block_lines))
        return blocks
