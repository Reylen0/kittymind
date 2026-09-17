from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果：ok 由工具自己显式声明，不再靠下游猜文本。"""
    ok: bool
    content: str


class BaseTool(ABC):
    name: str = None
    description: str = None
    param_class: BaseModel = None

    def __init__(self, name=None, description=None, param_class=None):
        if name is not None: self.name = name
        if description is not None: self.description = description
        if param_class is not None: self.param_class = param_class

    def run(self, parameters: dict[str, Any]) -> ToolResult:
        return self.execute(self.param_class(**parameters))

    @abstractmethod
    def execute(self, parameters: BaseModel) -> ToolResult:
        pass

    def to_schema(self) -> dict[str, Any]:
        schema = self.param_class.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            }
        }
