import json

from .registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, tool_call: dict) -> dict:
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
