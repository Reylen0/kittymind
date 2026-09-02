"""剪贴板读写工具。使用 pyperclip 库。"""

from pydantic import BaseModel, Field

from baseagent.tools.base import BaseTool


class ClipboardToolParam(BaseModel):
    action: str = Field(description="操作类型：read（读取剪贴板）或 write（写入剪贴板）")
    content: str = Field(default="", description="写入剪贴板的文本内容（action=write 时必填）")


class ClipboardTool(BaseTool):
    """剪贴板读写工具"""

    name: str = "clipboard"
    description: str = (
        "读取或写入系统剪贴板。"
        "action=read：返回当前剪贴板文本内容；"
        "action=write：将指定文本写入剪贴板。"
    )
    param_class = ClipboardToolParam

    def execute(self, parameters: ClipboardToolParam) -> str:
        try:
            import pyperclip
        except ImportError:
            return "错误: 请先安装 pyperclip 库：uv add pyperclip"

        action = parameters.action.lower().strip()

        if action == "read":
            try:
                text = pyperclip.paste()
                if not text:
                    return "(剪贴板为空)"
                return text
            except Exception as e:
                return f"错误: 读取剪贴板失败 — {e}"

        elif action == "write":
            if not parameters.content:
                return "错误: write 操作需要提供 content 参数"
            try:
                pyperclip.copy(parameters.content)
                return f"已复制到剪贴板（{len(parameters.content)} 个字符）"
            except Exception as e:
                return f"错误: 写入剪贴板失败 — {e}"

        else:
            return f"错误: 不支持的操作 '{action}'，可用: read、write"
