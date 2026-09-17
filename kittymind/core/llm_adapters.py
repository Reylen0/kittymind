import json
from abc import ABC, abstractmethod

from .llm_response import LLMResponse, StreamEvent
from ..config import cfg


class BaseLLMAdapter(ABC):
    def __init__(self, model: str, api_key: str, base_url: str, timeout: int = 60):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._client = None
        self._async_client = None

    @abstractmethod
    def _create_client(self): pass

    def _create_async_client(self):
        raise NotImplementedError

    @abstractmethod
    def invoke(self, messages: list[dict], **kwargs) -> LLMResponse: pass

    @abstractmethod
    def invoke_stream(self, messages: list[dict], **kwargs) -> iter: pass

    @abstractmethod
    def stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs): pass

    async def async_stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        raise NotImplementedError


class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI 兼容接口适配器（DeepSeek/Qwen/Kimi/智谱/Ollama 等）"""

    def _create_client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

    def _create_async_client(self):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

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

    async def async_stream_with_tools(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ):
        if not self._async_client:
            self._async_client = self._create_async_client()

        create_kwargs = {
            "model": self.model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            create_kwargs["tools"] = tools
        create_kwargs.update(kwargs)

        tool_calls_buf: dict[int, dict] = {}
        usage_snapshot: dict | None = None

        response = await self._async_client.chat.completions.create(**create_kwargs)
        async for chunk in response:
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


class AnthropicAdapter(BaseLLMAdapter):
    """Anthropic API 适配器（claude-* 模型）。

    base_url 指向 Anthropic 兼容端点（直连 https://api.anthropic.com 或兼容代理）。
    内部将 OpenAI 格式消息/工具模式转换为 Anthropic 格式；响应转回内部 OpenAI 格式。
    """

    def _create_client(self):
        import anthropic
        return anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url or None,
            timeout=float(self.timeout),
        )

    def _create_async_client(self):
        import anthropic
        return anthropic.AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url or None,
            timeout=float(self.timeout),
        )

    # ── 提示词缓存断点 ────────────────────────────────────────────
    #
    # Anthropic 缓存需显式在 content block 上打 cache_control，不像 OpenAI
    # 兼容端点那样按前缀自动缓存。断点越靠后，覆盖的可复用前缀越长：
    #   tools（工具定义几乎不变）→ system（含记忆召回段，会话内冻结）→
    #   messages 最后一条（标准的多轮对话增量缓存写法：本轮标记的前缀，
    #   下一轮连同新增内容一起复用）。三处共 3 个断点，未超过 API 上限 4。

    _CACHE_CONTROL = {"type": "ephemeral"}

    @classmethod
    def _mark_cache_breakpoint(cls, block: dict) -> dict:
        return {**block, "cache_control": cls._CACHE_CONTROL}

    @classmethod
    def _add_cache_breakpoint(cls, messages: list[dict]) -> list[dict]:
        """在最后一条消息末尾追加缓存断点，使其之前的完整历史可被复用。"""
        if not messages:
            return messages
        content = messages[-1]["content"]
        if isinstance(content, str):
            if not content:
                return messages
            content = [{"type": "text", "text": content}]
        elif not content:
            return messages
        else:
            content = list(content)
        content[-1] = cls._mark_cache_breakpoint(content[-1])
        messages[-1] = {**messages[-1], "content": content}
        return messages

    # ── 格式转换：消息 ─────────────────────────────────────────────

    @staticmethod
    def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
        """OpenAI 格式消息 → Anthropic 格式（跳过 system，合并连续 tool 结果）。"""
        result: list[dict] = []
        for msg in messages:
            role = msg["role"]
            if role == "system":
                continue

            if role == "tool":
                # tool 消息 → user 消息中的 tool_result 块
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content") or "",
                }
                # 连续多条 tool 结果合并到同一 user 消息（Anthropic 要求角色交替）
                if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                    result[-1]["content"].append(block)
                else:
                    result.append({"role": "user", "content": [block]})

            elif role == "assistant" and msg.get("tool_calls"):
                # assistant 带工具调用 → content 列表（text + tool_use）
                content: list = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    args = tc["function"].get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": args,
                    })
                result.append({"role": "assistant", "content": content})

            else:
                result.append({"role": role, "content": msg.get("content") or ""})

        return result

    # ── 格式转换：工具 ─────────────────────────────────────────────

    @staticmethod
    def _to_anthropic_tools(tools: list[dict] | None) -> list[dict] | None:
        """OpenAI function schema → Anthropic tool schema。"""
        if not tools:
            return None
        result = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                f = t["function"]
                result.append({
                    "name": f["name"],
                    "description": f.get("description", ""),
                    "input_schema": f.get("parameters", {}),
                })
            else:
                result.append(t)
        if result:
            result[-1] = AnthropicAdapter._mark_cache_breakpoint(result[-1])
        return result or None

    # ── 构建请求参数 ────────────────────────────────────────────────

    def _make_params(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        kwargs: dict,
    ) -> dict:
        kwargs.pop("temperature", None)  # Anthropic Claude 不接受 temperature 参数
        params: dict = {
            "model": self.model,
            "messages": self._add_cache_breakpoint(self._to_anthropic_messages(messages)),
            "max_tokens": kwargs.pop("max_tokens", cfg.LLM_MAX_TOKENS),
        }
        params.update(kwargs)
        # 合并全部 system 消息（按原顺序）：内部会用独立 system 消息承载记忆召回段
        system_parts = [
            m.get("content") for m in messages
            if m.get("role") == "system" and m.get("content")
        ]
        if system_parts:
            params["system"] = [
                self._mark_cache_breakpoint({"type": "text", "text": "\n\n".join(system_parts)})
            ]
        ant_tools = self._to_anthropic_tools(tools)
        if ant_tools:
            params["tools"] = ant_tools
        return params

    # ── 响应转换：tool_use 块 → 内部格式 ──────────────────────────

    @staticmethod
    def _parse_content_blocks(content_blocks) -> tuple[str | None, list[dict]]:
        """从 Anthropic content 块中提取文本和工具调用（内部 OpenAI 格式）。"""
        text = None
        tool_calls = []
        for block in content_blocks:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
        return text, tool_calls

    # ── 公共接口 ───────────────────────────────────────────────────

    def invoke(self, messages: list[dict], tools: list[dict] = None, **kwargs) -> LLMResponse:
        if not self._client:
            self._client = self._create_client()
        params = self._make_params(messages, tools, kwargs)
        resp = self._client.messages.create(**params)
        text, tool_calls = self._parse_content_blocks(resp.content)
        return LLMResponse(content=text, tool_calls=tool_calls)

    def invoke_stream(self, messages: list[dict], **kwargs) -> iter:
        if not self._client:
            self._client = self._create_client()
        params = self._make_params(messages, None, kwargs)
        for event in self._client.messages.create(**params, stream=True):
            if event.type == "content_block_delta" and event.delta.type == "text_delta":
                yield event.delta.text

    def stream_with_tools(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        if not self._client:
            self._client = self._create_client()
        params = self._make_params(messages, tools, kwargs)

        tool_calls_buf: dict[int, dict] = {}
        input_tokens = 0
        output_tokens = 0

        for event in self._client.messages.create(**params, stream=True):
            etype = event.type
            if etype == "message_start":
                input_tokens = event.message.usage.input_tokens
            elif etype == "content_block_start":
                cb = event.content_block
                if cb.type == "tool_use":
                    tool_calls_buf[event.index] = {
                        "id": cb.id, "name": cb.name, "input_parts": [],
                    }
            elif etype == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    yield StreamEvent(type="text_delta", delta=delta.text)
                elif delta.type == "input_json_delta" and event.index in tool_calls_buf:
                    tool_calls_buf[event.index]["input_parts"].append(delta.partial_json)
            elif etype == "message_delta":
                output_tokens = event.usage.output_tokens

        if tool_calls_buf:
            yield StreamEvent(type="tool_calls_done", tool_calls=[
                {
                    "id": buf["id"], "type": "function",
                    "function": {
                        "name": buf["name"],
                        "arguments": "".join(buf["input_parts"]) or "{}",
                    },
                }
                for _, buf in sorted(tool_calls_buf.items())
            ])

        yield StreamEvent(type="usage", usage={
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        })

    async def async_stream_with_tools(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ):
        if not self._async_client:
            self._async_client = self._create_async_client()
        params = self._make_params(messages, tools, kwargs)

        tool_calls_buf: dict[int, dict] = {}
        input_tokens = 0
        output_tokens = 0

        async for event in await self._async_client.messages.create(**params, stream=True):
            etype = event.type
            if etype == "message_start":
                input_tokens = event.message.usage.input_tokens
            elif etype == "content_block_start":
                cb = event.content_block
                if cb.type == "tool_use":
                    tool_calls_buf[event.index] = {
                        "id": cb.id, "name": cb.name, "input_parts": [],
                    }
            elif etype == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    yield StreamEvent(type="text_delta", delta=delta.text)
                elif delta.type == "input_json_delta" and event.index in tool_calls_buf:
                    tool_calls_buf[event.index]["input_parts"].append(delta.partial_json)
            elif etype == "message_delta":
                output_tokens = event.usage.output_tokens

        if tool_calls_buf:
            yield StreamEvent(type="tool_calls_done", tool_calls=[
                {
                    "id": buf["id"], "type": "function",
                    "function": {
                        "name": buf["name"],
                        "arguments": "".join(buf["input_parts"]) or "{}",
                    },
                }
                for _, buf in sorted(tool_calls_buf.items())
            ])

        yield StreamEvent(type="usage", usage={
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        })


def create_adapter(model: str, api_key: str, base_url: str, timeout: int) -> BaseLLMAdapter:
    """根据模型名称自动选择适配器：claude-* / anthropic.* → AnthropicAdapter，其余 → OpenAIAdapter。"""
    if model.startswith(("claude-", "anthropic.")):
        return AnthropicAdapter(model, api_key, base_url, timeout)
    return OpenAIAdapter(model, api_key, base_url, timeout)
