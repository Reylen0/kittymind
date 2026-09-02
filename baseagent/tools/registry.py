from typing import Any

from ..core.exceptions import ToolException

from .base import BaseTool


class ToolRegistry:
    """工具注册表"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        """注册工具"""
        if tool.name in self._tools:
            raise ToolException(f"工具{tool.name}已注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        """根据名字返回工具"""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolException(f"工具{name}不存在")
    
    def get_schemas(self) -> list[dict[str, Any]]:
        """返回已注册的工具的schema列表"""
        return [tool.to_schema() for tool in self._tools.values()]
