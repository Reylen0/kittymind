from typing import Optional, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel

MessageRole = Literal["user", "assistant", "system", "tool"]


class Message(BaseModel):
    """消息类，兼容 OpenAI API 格式（含工具调用）"""

    content: Optional[str] = None
    role: MessageRole
    timestamp: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None

    def __init__(self, content: Optional[str], role: MessageRole, **kwargs):
        super().__init__(
            content=content,
            role=role,
            timestamp=kwargs.get('timestamp', datetime.now()),
            metadata=kwargs.get('metadata', {}),
            tool_calls=kwargs.get('tool_calls'),
            tool_call_id=kwargs.get('tool_call_id'),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d

    def __str__(self) -> str:
        return f"[{self.role}] {self.content}"
