"""Agent 抽象基类"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from ..core.message import Message
from ..core.llm import BaseAgentLLM

if TYPE_CHECKING:
    from ..callbacks.base import BaseCallBack


class Agent(ABC):
    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        callbacks: Optional[list["BaseCallBack"]] = None,
    ):
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt
        self.description = description
        self.callbacks = callbacks or []
        self._history: dict[str, list[Message]] = {}

    @abstractmethod
    def run(self, session_id: str | None, input_text: str, **kwargs) -> str:
        pass

    def add_message(self, session_id: str, message: Message):
        if session_id not in self._history:
            self._history[session_id] = []
        self._history[session_id].append(message)

    def clear_history(self, session_id: str):
        if session_id in self._history:
            self._history[session_id].clear()

    def replace_history(self, session_id: str, messages: list[dict]):
        """用压缩后的 dict 列表替换 _history，使下轮 _build_messages 用压缩版。"""
        self._history[session_id] = [
            Message(
                content=m.get("content"),
                role=m["role"],
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in messages
        ]

    def get_history(self, session_id: str) -> list[Message]:
        return self._history.get(session_id, [])

    def get_context(self, session_id: str) -> list[dict]:
        return [msg.to_dict() for msg in self.get_history(session_id)]

    def _emit(self, event: str, *args, **kwargs):
        for cb in self.callbacks:
            method = getattr(cb, event, None)
            if method:
                method(*args, **kwargs)

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"
