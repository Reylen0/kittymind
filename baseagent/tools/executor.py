from .registry import ToolRegistry
import json

class ToolExecutor:
    """工具执行器"""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, tool_call: dict) -> dict:
        """根据llm返回tool_call调用工具"""
        try:
            tool_call_id = tool_call.get("id")
            function = tool_call.get("function")
            name = function.get("name")
            args = json.loads(function.get("arguments") or "{}")
            tool = self.registry.get(name=name)
            result = tool.run(args)
            return {"role": "tool", "tool_call_id": tool_call_id, "content": str(result)}
        except Exception as e:
            return {"role": "tool", "tool_call_id": tool_call_id, "content": str(e)}