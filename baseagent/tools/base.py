from abc import ABC, abstractmethod
from typing import Dict
from typing import Any

from pydantic import BaseModel

class BaseTool(ABC):
    """工具基类"""

    name: str = None
    description: str = None
    param_class: BaseModel = None

    def __init__(self, name=None, description=None, param_class=None):
        if name is not None:
            self.name = name
        if description is not None:
            self.description = description
        if param_class is not None:
            self.param_class = param_class

    def run(self, parameters: dict[str, Any]) -> Any:
        param_class = self.param_class(**parameters)
        return self.execute(param_class)

    @abstractmethod
    def execute(self, parameters: BaseModel) -> Any:
        """执行工具
        Args:
            parameters: 工具参数字典
            content: 可选的上下文信息
        """
        pass

    def to_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling schema 格式

        用于 FunctionCallAgent，使工具能够被 OpenAI 原生 function calling 使用

        Returns:
            符合 OpenAI function calling 标准的 schema
        """
        schema = self.param_class.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema
            }
        }