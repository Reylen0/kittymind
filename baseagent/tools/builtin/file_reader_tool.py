import os

from pydantic import BaseModel, Field

from ..base import BaseTool

_MAX_BYTES = 100_000  # 单次最多读取 100 KB


class FileReaderParam(BaseModel):
    path: str = Field(description="文件的绝对路径或相对路径")
    encoding: str = Field(default="utf-8", description="文件编码，默认 utf-8")


class FileReaderTool(BaseTool):
    """本地文件读取工具"""

    name: str = "file_reader"
    description: str = "读取本地文件的文本内容，适合 Agent 按需获取文件数据"
    param_class = FileReaderParam

    def execute(self, parameters: FileReaderParam) -> str:
        path = os.path.abspath(parameters.path)
        if not os.path.exists(path):
            return f"错误: 文件不存在 — {path}"
        if not os.path.isfile(path):
            return f"错误: 路径不是文件 — {path}"

        size = os.path.getsize(path)
        try:
            with open(path, "r", encoding=parameters.encoding, errors="replace") as f:
                content = f.read(_MAX_BYTES)
        except Exception as e:
            return f"错误: 读取失败 — {e}"

        truncated = size > _MAX_BYTES
        header = f"[文件: {path}  大小: {size} 字节{'  (已截断至前 100 KB)' if truncated else ''}]\n"
        return header + content
