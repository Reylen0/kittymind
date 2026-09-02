from abc import ABC, abstractmethod

from .llm_response import LLMResponse, StreamEvent


class BaseLLMAdapter(ABC):
    """LLM适配器基类"""

    def __init__(self, model: str, api_key: str, base_url: str, timeout: int = 60):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._client = None

    @abstractmethod
    def _create_client(self):
        """创建客户端"""
        pass

    @abstractmethod
    def invoke(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse:
        """调用大语言模型 非流式响应"""
        pass

    @abstractmethod
    def invoke_stream(self, messages: list[dict[str, str]], **kwargs) -> iter:
        """调用大语言模型 流式响应（仅文字）"""
        pass

    @abstractmethod
    def stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        """流式调用，同时处理文字内容和工具调用。

        yield StreamEvent：
          - type='text_delta'      文字片段
          - type='tool_calls_done' 完整工具调用列表（流结束时，有工具调用则 yield）
        """
        pass

class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI兼容接口适配器（默认）

    支持：
    - OpenAI官方API
    - 所有OpenAI兼容接口（DeepSeek、Qwen、Kimi、智谱等）
    - Thinking Models（o1、deepseek-reasoner等）
    """
    def _create_client(self):
        """创建OpenAI客户端"""
        from openai import OpenAI

        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )

    def invoke(self, messages: list[dict[str, str]], tools: list[dict] = None, **kwargs) -> LLMResponse:
        """调用大语言模型 非流式响应"""
        if not self._client:
            self._client = self._create_client()

        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            **kwargs
        )
        message = response.choices[0].message
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })
        return LLMResponse(content=message.content, tool_calls=tool_calls)

    def invoke_stream(self, messages: list[dict[str, str]], **kwargs) -> iter:
        """调用大语言模型 流式响应（仅文字）"""
        if not self._client:
            self._client = self._create_client()

        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs
        )

        for chunk in response:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            if content:
                yield content

    def stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        """流式调用，同时处理文字内容和工具调用。

        yield StreamEvent：
          text_delta      — 文字片段
          tool_calls_done — 完整工具调用列表（仅当有工具调用时）
        """
        if not self._client:
            self._client = self._create_client()

        create_kwargs: dict = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            create_kwargs["tools"] = tools
        create_kwargs.update(kwargs)

        tool_calls_buf: dict[int, dict] = {}

        response = self._client.chat.completions.create(**create_kwargs)
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                yield StreamEvent(type='text_delta', delta=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buf:
                        tool_calls_buf[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.id:
                        tool_calls_buf[idx]["id"] += tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_buf[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_buf[idx]["function"]["arguments"] += tc.function.arguments

        if tool_calls_buf:
            # 确保每个工具调用的 arguments 至少是合法 JSON（LLM 可能对无参工具返回空字符串）
            for tc in tool_calls_buf.values():
                if not tc["function"]["arguments"]:
                    tc["function"]["arguments"] = "{}"
            yield StreamEvent(type='tool_calls_done', tool_calls=list(tool_calls_buf.values()))


def create_adapter(model: str, api_key: str, base_url: str, timeout: int) -> BaseLLMAdapter:
    # 暂时只支持 OpenAI 兼容接口，后续可按 base_url 路由到其他适配器
    return OpenAIAdapter(model, api_key, base_url, timeout)
