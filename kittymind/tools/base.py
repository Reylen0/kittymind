from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果：ok 由工具自己显式声明，不再靠下游猜文本。"""
    ok: bool
    content: str


class BaseTool(ABC):
    # 子类必须以类属性（或经 __init__）提供 name / description / param_class，
    # 因此基类占位值是 None——标注必须如实写 Optional，别写 str（RUF013 曾报过）。
    name: str | None = None
    description: str | None = None
    param_class: type[BaseModel] | None = None
    # True 表示该工具没有同步 execute() 实现，必须走 aexecute()（目前只有 task 工具）
    is_async: ClassVar[bool] = False

    def __init__(self, name=None, description=None, param_class=None):
        if name is not None: self.name = name
        if description is not None: self.description = description
        if param_class is not None: self.param_class = param_class

    def run(self, parameters: dict[str, Any]) -> ToolResult:
        return self.execute(self.param_class(**parameters))

    @abstractmethod
    def execute(self, parameters: BaseModel) -> ToolResult:
        pass

    async def arun(self, parameters: dict[str, Any]) -> ToolResult:
        return await self.aexecute(self.param_class(**parameters))

    async def aexecute(self, parameters: BaseModel) -> ToolResult:
        raise NotImplementedError(f"{type(self).__name__} 未实现 aexecute")

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

