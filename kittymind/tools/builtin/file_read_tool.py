import os

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ._fmt import human_size
from ._paths import resolve_path
from ...config import cfg


class FileReadToolParam(BaseModel):
    path: str = Field(description="要读取的文件路径")
    start_line: int = Field(default=1, description="起始行号（从 1 开始）")
    end_line: int = Field(default=0, description="结束行号（含），0 表示读到末尾")
    encoding: str = Field(default="utf-8", description="文件编码，默认 utf-8")


class FileReadTool(BaseTool):
    name: str = "file_read"
    description: str = (
        "读取文件内容并显示行号，支持指定行范围。"
        "单次返回行数有上限；大文件请用 start_line/end_line 分段读取。"
    )
    param_class = FileReadToolParam

    def execute(self, parameters: FileReadToolParam) -> ToolResult:
        path = resolve_path(parameters.path)
        if not os.path.exists(path): return ToolResult(False, f"错误: 文件不存在 — {path}")
        if not os.path.isfile(path): return ToolResult(False, f"错误: 路径不是文件 — {path}")
        size = os.path.getsize(path)
        if size > cfg.FILE_READ_MAX_BYTES:
            return ToolResult(
                False,
                f"错误: 文件过大 ({human_size(size)} > "
                f"{human_size(cfg.FILE_READ_MAX_BYTES)})，请用 start_line/end_line 分段读取",
            )
        try:
            with open(path, encoding=parameters.encoding, errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            return ToolResult(False, f"错误: 读取失败 — {e}")
        total = len(all_lines)
        start = max(1, parameters.start_line)
        end = total if parameters.end_line <= 0 else min(parameters.end_line, total)
        if start > total: return ToolResult(False, f"错误: start_line={start} 超出文件总行数 {total}")
        selected = all_lines[start - 1: end]
        max_lines = cfg.FILE_READ_MAX_LINES
        truncated = len(selected) > max_lines
        if truncated: selected = selected[:max_lines]
        width = len(str(end))
        numbered = "".join(f"{start+i:{width}d}  {line}" for i, line in enumerate(selected))
        header = f"[{path}  {total} 行  {size} 字节"
        if start != 1 or end != total: header += f"  显示 {start}-{start+len(selected)-1} 行"
        if truncated: header += f"  (已截断至 {max_lines} 行)"
        return ToolResult(True, header + "]\n" + numbered)
