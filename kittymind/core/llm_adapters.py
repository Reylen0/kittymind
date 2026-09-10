from abc import ABC, abstractmethod

from .llm_response import LLMResponse, StreamEvent


class BaseLLMAdapter(ABC):
    def __init__(self, model: str, api_key: str, base_url: str, timeout: int = 60):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._client = None

    @abstractmethod
    def _create_client(self): pass

    @abstractmethod
    def invoke(self, messages: list[dict], **kwargs) -> LLMResponse: pass

    @abstractmethod
    def invoke_stream(self, messages: list[dict], **kwargs) -> iter: pass

    @abstractmethod
    def stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs): pass


class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI 兼容接口适配器（DeepSeek/Qwen/Kimi/智谱/Ollama 等）"""

    def _create_client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

    def invoke(self, messages: list[dict], tools: list[dict] = None, **kwargs) -> LLMResponse:
        if not self._client:
            self._client = self._create_client()
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, **kwargs
        )
        msg = resp.choices[0].message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id, "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                })
        return LLMResponse(content=msg.content, tool_calls=tool_calls)

    def invoke_stream(self, messages: list[dict], **kwargs) -> iter:
        if not self._client:
            self._client = self._create_client()
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, stream=True, **kwargs
        )
        for chunk in resp:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            if content:
                yield content

    def stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        if not self._client:
            self._client = self._create_client()

        create_kwargs = {
            "model": self.model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            create_kwargs["tools"] = tools
        create_kwargs.update(kwargs)

        tool_calls_buf: dict[int, dict] = {}
        usage_snapshot: dict | None = None

        for chunk in self._client.chat.completions.create(**create_kwargs):
            # 末尾 usage-only chunk（无 choices）——记录但不跳过
            if not chunk.choices:
                if chunk.usage is not None:
                    usage_snapshot = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }
                continue

            delta = chunk.choices[0].delta

            if delta.content:
                yield StreamEvent(type='text_delta', delta=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buf:
                        tool_calls_buf[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc.id:
                        tool_calls_buf[idx]["id"] += tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_buf[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_buf[idx]["function"]["arguments"] += tc.function.arguments

        if tool_calls_buf:
            for tc in tool_calls_buf.values():
                if not tc["function"]["arguments"]:
                    tc["function"]["arguments"] = "{}"
            yield StreamEvent(type='tool_calls_done', tool_calls=list(tool_calls_buf.values()))

        if usage_snapshot:
            yield StreamEvent(type='usage', usage=usage_snapshot)


def create_adapter(model: str, api_key: str, base_url: str, timeout: int) -> BaseLLMAdapter:
    return OpenAIAdapter(model, api_key, base_url, timeout)
