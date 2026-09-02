from abc import ABC, abstractmethod

from ..core.message import Message


class BaseMemory(ABC):
    @abstractmethod
    def save(self, session_id: str, message: Message) -> None:
        pass

    @abstractmethod
    def get_history(self, session_id: str) -> list[Message]:
        pass

    @abstractmethod
    def get_context(self, session_id: str) -> list[dict]:
        pass

    @abstractmethod
    def clear(self, session_id: str) -> None:
        pass