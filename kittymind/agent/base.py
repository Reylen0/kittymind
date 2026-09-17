"""Agent 基类"""
from typing import TYPE_CHECKING

from ..core.llm import BaseAgentLLM

if TYPE_CHECKING:
    from ..callbacks.base import BaseCallBack


class Agent:
    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: str | None = None,
        description: str | None = None,
        callbacks: list["BaseCallBack"] | None = None,
    ):
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt
        self.description = description
        self.callbacks = callbacks or []

    def _emit(self, event: str, *args, **kwargs):
        for cb in self.callbacks:
            method = getattr(cb, event, None)
            if method:
                method(*args, **kwargs)

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"
