"""Agent基类"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from .message import Message
from .llm import BaseAgentLLM
from ..memory.base import BaseMemory

if TYPE_CHECKING:
    from baseagent.callbacks.base import BaseCallBack

class Agent(ABC):
    """Agent基类"""
    
    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        memory: Optional["BaseMemory"] = None,
        description: Optional[str] = None,
        callbacks: Optional[list["BaseCallBack"]] = None
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
        """运行Agent"""
        pass
    
    def add_message(self, session_id: str, message: Message):
        """添加消息到历史记录"""
        if self.memory:
            self.memory.save(session_id, message)
        else:
            if session_id not in self._history:
                self._history[session_id] = []
            self._history[session_id].append(message)

    def clear_history(self, session_id: str):
        """清空历史记录"""
        if self.memory:
            self.memory.clear(session_id)
        else:
            if session_id in self._history:
                self._history[session_id].clear()
    
    def get_history(self, session_id: str) -> list[Message]:
        """获取历史记录"""
        if self.memory:
            return self.memory.get_history(session_id)
        return self._history.get(session_id, [])

    def get_context(self, session_id: str) -> list[dict[str, str]]:
        """获取上下文"""
        if self.memory:
            return self.memory.get_context(session_id)
        history = self.get_history(session_id)
        if not history:
            return []
        return [msg.to_dict() for msg in history]

    def _emit(self, event: str, *args, **kwargs):
        for cb in self.callbacks:
            method = getattr(cb, event, None)
            if method:
                method(*args, **kwargs)
    
    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model}, description={self.description})"
