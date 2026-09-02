"""BaseAgent统一LLM接口 - 基于OpenAI原生API"""

import os
from typing import Optional, Iterator

from .llm_adapters import create_adapter
from .llm_response import LLMResponse, StreamEvent

from .exceptions import BaseAgentException, LLMException

class BaseAgentLLM:
    """
    BaseAgent统一LLM客户端

    设计理念：
    - 统一配置：只需 LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL、LLM_TIMEOUT

    支持的接口：
    - OpenAI及所有兼容接口（DeepSeek、Qwen、Kimi、智谱、Ollama等）
    - Anthropic Claude
    - Google Gemini
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        **kwargs
    ):
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.api_key = api_key or os.getenv("LLM_API_KEY")
        self.base_url = base_url or os.getenv("LLM_BASE_URL")
        self.timeout = timeout or int(os.getenv("LLM_TIMEOUT", "60"))

        self.temperature = temperature
        self.max_tokens = max_tokens
        self.kwargs = kwargs

        if not self.model:
            raise BaseAgentException("必须提供模型名称（model参数或LLM_MODEL_ID环境变量）")
        if not self.api_key:
            raise BaseAgentException("必须提供API密钥（api_key参数或LLM_API_KEY环境变量）")
        if not self.base_url:
            raise BaseAgentException("必须提供服务地址（base_url参数或LLM_BASE_URL环境变量）")

        self._client = create_adapter(self.model, self.api_key, self.base_url, self.timeout)

    def _base_kwargs(self, kwargs: dict) -> dict:
        """提取通用调用参数"""
        call_kwargs = {"temperature": kwargs.pop("temperature", self.temperature)}
        if self.max_tokens:
            call_kwargs["max_tokens"] = kwargs.pop("max_tokens", self.max_tokens)
        call_kwargs.update(kwargs)
        return call_kwargs

    def think(self, messages: list[dict[str, str]], **kwargs) -> Iterator[str]:
        """流式调用（仅文字），适合最终回答场景。"""
        call_kwargs = self._base_kwargs(kwargs)
        try:
            yield from self._client.invoke_stream(messages=messages, **call_kwargs)
        except Exception as e:
            raise LLMException(f"LLM调用失败: {str(e)}")

    def invoke(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse:
        """非流式调用，返回完整响应。"""
        call_kwargs = self._base_kwargs(kwargs)
        try:
            return self._client.invoke(messages=messages, **call_kwargs)
        except Exception as e:
            raise LLMException(f"LLM调用失败: {str(e)}")

    def stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        """全程流式调用，同时处理文字和工具调用。

        yield StreamEvent：
          text_delta      — 文字片段（含工具调用步骤里 LLM 输出的文字）
          tool_calls_done — 流结束时的完整工具调用列表（有工具调用才 yield）
        """
        call_kwargs = self._base_kwargs(kwargs)
        try:
            yield from self._client.stream_with_tools(
                messages=messages, tools=tools, **call_kwargs
            )
        except Exception as e:
            raise LLMException(f"LLM流式调用失败: {str(e)}")
