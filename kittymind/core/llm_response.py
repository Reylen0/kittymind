from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StreamEvent:
    """stream_with_tools() 产生的流式事件。

    type:
      'text_delta'      — 文字片段
      'tool_calls_done' — 流结束时的完整工具调用列表
      'usage'           — 本次调用真实 token 用量（OpenAI usage 字段）
    """
    type: str
    delta: str = ''
    tool_calls: list = field(default_factory=list)
    usage: Optional[dict] = None


class LLMResponse:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[list[dict]] = None):
        self.content = content
        self.tool_calls = tool_calls or []

    def is_tool_call(self) -> bool:
        return len(self.tool_calls) > 0

    def __str__(self) -> str:
        return f"LLMResponse(content={self.content}, tool_calls={self.tool_calls})"
