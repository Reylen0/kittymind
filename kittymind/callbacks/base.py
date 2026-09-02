from typing import Any

from ..core.llm_response import LLMResponse
from ..core.message import Message


class BaseCallBack:
    def on_agent_start(self, name: str, input_text: str): pass
    def on_agent_end(self, name: str, final_text: str): pass
    def on_agent_error(self, name: str, error: Exception): pass
    def on_llm_start(self, messages: list[Message]): pass
    def on_llm_end(self, response: LLMResponse): pass
    def on_llm_error(self, error: Exception): pass
    def on_tool_start(self, name: str, args: Any): pass
    def on_tool_end(self, name: str, result: dict): pass
    def on_tool_error(self, name: str, error: Exception): pass
