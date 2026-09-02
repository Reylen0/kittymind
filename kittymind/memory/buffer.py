from ..core.message import Message
from .base import BaseMemory


class BufferMemory(BaseMemory):
    """滑动窗口会话历史"""

    def __init__(self, window_size: int):
        self.window_size = window_size
        self.history: dict[str, list[Message]] = {}

    def save(self, session_id: str, message: Message) -> None:
        if session_id not in self.history:
            self.history[session_id] = []
        self.history[session_id].append(message)
        if len(self.history[session_id]) > self.window_size:
            self.history[session_id] = self.history[session_id][-self.window_size:]

    def get_history(self, session_id: str) -> list[Message]:
        return self.history.get(session_id, [])

    def get_context(self, session_id: str) -> list[dict]:
        return [msg.to_dict() for msg in self.get_history(session_id)]

    def clear(self, session_id: str) -> None:
        if session_id in self.history:
            self.history[session_id].clear()
