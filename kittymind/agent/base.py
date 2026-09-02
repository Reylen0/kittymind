"""Agent 抽象基类"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from ..core.message import Message
from ..core.llm import BaseAgentLLM
from ..memory.base import BaseMemory

if TYPE_CHECKING:
    from ..callbacks.base import BaseCallBack


class Agent(ABC):
    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        memory: Optional["BaseMemory"] = None,
        description: Optional[str] = None,
        callbacks: Optional[list["BaseCallBack"]] = None,
    ):
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt
        self.memory = memory
        self.description = description
        self.callbacks = callbacks or []
        self._history: dict[str, list[Message]] = {}

    @abstractmethod
    def run(self, session_id: str | None, input_text: str, **kwargs) -> str:
        pass

    def add_message(self, session_id: str, message: Message):
        if self.memory:
            self.memory.save(session_id, message)
        else:
            if session_id not in self._history:
                self._history[session_id] = []
            self._history[session_id].append(message)

    def clear_history(self, session_id: str):
        if self.memory:
            self.memory.clear(session_id)
        else:
            if session_id in self._history:
                self._history[session_id].clear()

    def get_history(self, session_id: str) -> list[Message]:
        if self.memory:
            return self.memory.get_history(session_id)
        return self._history.get(session_id, [])

    def get_context(self, session_id: str) -> list[dict]:
        if self.memory:
            return self.memory.get_context(session_id)
        history = self.get_history(session_id)
        return [msg.to_dict() for msg in history]

    def _emit(self, event: str, *args, **kwargs):
        for cb in self.callbacks:
            method = getattr(cb, event, None)
            if method:
                method(*args, **kwargs)

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"
