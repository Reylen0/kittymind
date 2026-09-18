"""Agent 基类"""
from ..core.llm import BaseAgentLLM


class Agent:
    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: str | None = None,
        description: str | None = None,
    ):
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt
        self.description = description

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"
