"""BaseAgent统一LLM接口"""

import os
from typing import Optional, Iterator

from .llm_adapters import create_adapter
from .llm_response import LLMResponse, StreamEvent
from .exceptions import BaseAgentException, LLMException


class BaseAgentLLM:
    """统一 LLM 客户端，支持 OpenAI 及所有兼容接口。"""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: Optional[float] = None,
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
            raise BaseAgentException("必须提供模型名称（model 或 LLM_MODEL_ID）")
        if not self.api_key:
            raise BaseAgentException("必须提供 API 密钥（api_key 或 LLM_API_KEY）")
        if not self.base_url:
            raise BaseAgentException("必须提供服务地址（base_url 或 LLM_BASE_URL）")

        self._client = create_adapter(self.model, self.api_key, self.base_url, self.timeout)

    def _base_kwargs(self, kwargs: dict) -> dict:
        call_kwargs: dict = {}
        # 只有明确设置了 temperature 才发送（新版 Claude 模型已废弃该参数）
        t = kwargs.pop("temperature", self.temperature)
        if t is not None:
            call_kwargs["temperature"] = t
        if self.max_tokens:
            call_kwargs["max_tokens"] = kwargs.pop("max_tokens", self.max_tokens)
        call_kwargs.update(kwargs)
        return call_kwargs

    def invoke(self, messages: list[dict], **kwargs) -> LLMResponse:
        call_kwargs = self._base_kwargs(kwargs)
        try:
            return self._client.invoke(messages=messages, **call_kwargs)
        except Exception as e:
            raise LLMException(f"LLM调用失败: {e}")

    def stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        call_kwargs = self._base_kwargs(kwargs)
        try:
            yield from self._client.stream_with_tools(messages=messages, tools=tools, **call_kwargs)
        except Exception as e:
            raise LLMException(f"LLM流式调用失败: {e}")
