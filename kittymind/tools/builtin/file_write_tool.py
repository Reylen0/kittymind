import os

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ._fmt import human_size
from ._paths import resolve_path
from ...config import cfg


class FileWriteToolParam(BaseModel):
    path: str = Field(description="目标文件路径，不存在则创建")
    content: str = Field(description="写入文件的完整内容")
    encoding: str = Field(default="utf-8", description="文件编码，默认 utf-8")


class FileWriteTool(BaseTool):
    name: str = "file_write"
    description: str = (
        "创建或覆写一个文件，写入指定内容。父目录不存在时自动创建。"
        "如需局部修改已有文件，应使用 file_edit 工具。"
    )
    param_class = FileWriteToolParam

    def execute(self, parameters: FileWriteToolParam) -> ToolResult:
        path = resolve_path(parameters.path)
        max_bytes = cfg.FILE_WRITE_MAX_BYTES
        if len(parameters.content.encode(parameters.encoding, errors="replace")) > max_bytes:
            return ToolResult(False, f"错误: 内容超过 {human_size(max_bytes)} 限制，拒绝写入")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except Exception as e:
            return ToolResult(False, f"错误: 无法创建目录 — {e}")
        try:
            with open(path, "w", encoding=parameters.encoding, errors="replace") as f:
                f.write(parameters.content)
        except Exception as e:
            return ToolResult(False, f"错误: 写入失败 — {e}")
        lines = parameters.content.count("\n") + 1
        return ToolResult(True, f"已写入: {path}  ({lines} 行，{os.path.getsize(path)} 字节)")
