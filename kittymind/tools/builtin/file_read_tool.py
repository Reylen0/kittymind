import os

from pydantic import BaseModel, Field

from ..base import BaseTool
from .bash_tool import bash_cwd
from ...config import cfg

_MAX_LINES = cfg.FILE_READ_MAX_LINES
_MAX_BYTES = cfg.FILE_READ_MAX_BYTES


class FileReadToolParam(BaseModel):
    path: str = Field(description="要读取的文件路径")
    start_line: int = Field(default=1, description="起始行号（从 1 开始）")
    end_line: int = Field(default=0, description="结束行号（含），0 表示读到末尾")
    encoding: str = Field(default="utf-8", description="文件编码，默认 utf-8")


class FileReadTool(BaseTool):
    name: str = "file_read"
    description: str = (
        "读取文件内容并显示行号，支持指定行范围。"
        "单次最多返回 2000 行；大文件请用 start_line/end_line 分段读取。"
    )
    param_class = FileReadToolParam

    def execute(self, parameters: FileReadToolParam) -> str:
        raw = parameters.path
        if os.path.isabs(raw):
            path = os.path.normpath(raw)
        else:
            path = os.path.normpath(os.path.join(bash_cwd.get(), raw))
        if not os.path.exists(path): return f"错误: 文件不存在 — {path}"
        if not os.path.isfile(path): return f"错误: 路径不是文件 — {path}"
        size = os.path.getsize(path)
        if size > _MAX_BYTES:
            return f"错误: 文件过大 ({size/1024:.0f}KB > 800KB)，请用 start_line/end_line 分段读取"
        try:
            with open(path, "r", encoding=parameters.encoding, errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            return f"错误: 读取失败 — {e}"
        total = len(all_lines)
        start = max(1, parameters.start_line)
        end = total if parameters.end_line <= 0 else min(parameters.end_line, total)
        if start > total: return f"错误: start_line={start} 超出文件总行数 {total}"
        selected = all_lines[start - 1: end]
        truncated = len(selected) > _MAX_LINES
        if truncated: selected = selected[:_MAX_LINES]
        width = len(str(end))
        numbered = "".join(f"{start+i:{width}d}  {line}" for i, line in enumerate(selected))
        header = f"[{path}  {total} 行  {size} 字节"
        if start != 1 or end != total: header += f"  显示 {start}-{start+len(selected)-1} 行"
        if truncated: header += f"  (已截断至 {_MAX_LINES} 行)"
        return header + "]\n" + numbered
