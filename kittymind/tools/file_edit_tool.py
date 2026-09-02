import os

from pydantic import BaseModel, Field

from baseagent.tools.base import BaseTool

_MAX_FILE_SIZE = 2_000_000


class FileEditToolParam(BaseModel):
    path: str = Field(description="要编辑的文件路径")
    old_string: str = Field(description="要替换的原始字符串，必须在文件中精确匹配且唯一")
    new_string: str = Field(description="替换后的新字符串，可以为空字符串（表示删除）")
    encoding: str = Field(default="utf-8", description="文件编码，默认 utf-8")


class FileEditTool(BaseTool):
    """精确字符串替换工具"""

    name: str = "file_edit"
    description: str = (
        "在文件中将 old_string 精确替换为 new_string。"
        "old_string 必须在文件中唯一出现一次。"
        "适合局部修改；需重写整个文件时使用 file_write。"
    )
    param_class = FileEditToolParam

    def execute(self, parameters: FileEditToolParam) -> str:
        path = os.path.abspath(parameters.path)
        if not os.path.exists(path):
            return f"错误: 文件不存在 — {path}"
        if os.path.getsize(path) > _MAX_FILE_SIZE:
            return "错误: 文件超过 2MB，请使用 file_write 整体替换"
        try:
            with open(path, "r", encoding=parameters.encoding, errors="replace") as f:
                content = f.read()
        except Exception as e:
            return f"错误: 读取失败 — {e}"

        count = content.count(parameters.old_string)
        if count == 0:
            return (
                f"错误: 未找到 old_string（文件 {os.path.basename(path)}）\n"
                f"  首行: {repr(parameters.old_string.split(chr(10))[0][:60])}"
            )
        if count > 1:
            return f"错误: old_string 在文件中出现了 {count} 次，请添加更多上下文使其唯一"

        new_content = content.replace(parameters.old_string, parameters.new_string, 1)
        try:
            with open(path, "w", encoding=parameters.encoding, errors="replace") as f:
                f.write(new_content)
        except Exception as e:
            return f"错误: 写入失败 — {e}"

        delta = parameters.new_string.count("\n") - parameters.old_string.count("\n")
        sign = f"+{delta}" if delta >= 0 else str(delta)
        return f"已编辑: {path}  ({sign} 行)"
