from typing import Any

from ..core.llm_response import LLMResponse
from ..core.message import Message


class BaseCallBack:
    """agent执行流程回调基类"""

    def on_agent_start(self, name: str, input_text: str):
        """agent调用开始时"""
        pass

    def on_agent_end(self, name: str, final_text: str):
        """agent调用结束时"""
        pass

    def on_agent_error(self, name: str, error: Exception):
        """agent调用报错时"""
        pass

    def on_llm_start(self, messages: list[Message]):
        """llm调用开始时"""
        pass

    def on_llm_end(self, response: LLMResponse):
        """llm调用结束时"""
        pass

    def on_llm_error(self, error: Exception):
        """llm调用报错时"""
        pass

    def on_tool_start(self, name: str, args: Any):
        """工具调用开始时"""
        pass

    def on_tool_end(self, name: str, result: dict):
        """工具调用结束时"""
        pass

    def on_tool_error(self, name: str, error: Exception):
        """工具调用报错时"""
        pass