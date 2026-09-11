import json

from ..config import cfg
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
            content = str(result)
            if len(content) > cfg.BASH_MAX_OUTPUT:
                content = content[:cfg.BASH_MAX_OUTPUT] + f"\n…[输出过长，已截断至 {cfg.BASH_MAX_OUTPUT} 字符]"
            return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        except Exception as e:
            return {"role": "tool", "tool_call_id": tool_call_id, "content": str(e)}
