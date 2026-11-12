"""BaseAgent统一LLM接口"""

import asyncio
import logging
import os
import random
import time

import anthropic
import openai

from .llm_adapters import create_adapter
from .llm_response import LLMResponse
from .exceptions import BaseAgentException, LLMException
from ..config import cfg

logger = logging.getLogger(__name__)

# 瞬时错误：网络层 / 超时 / 429 / 5xx —— OpenAI、Anthropic 两个 SDK 的异常层级
# 设计几乎一致（同源于 httpx），都能精确对上这几类。400/401/403/404 等业务错误
# 故意不在其中，重试它们只会原样失败 N 次，白白拖长响应时间。
_TRANSIENT_ERROR_TYPES = (
    openai.APIConnectionError, openai.APITimeoutError,
    openai.RateLimitError, openai.InternalServerError,
    anthropic.APIConnectionError, anthropic.APITimeoutError,
    anthropic.RateLimitError, anthropic.InternalServerError,
)


def _is_transient(e: Exception) -> bool:
    return isinstance(e, _TRANSIENT_ERROR_TYPES)


def _retry_delay(attempt: int) -> float:
    """指数退避 + 抖动：第 n 次重试（attempt 从 0 计）等待 base*2**n ~ base*2**(n+1)。"""
    base = cfg.LLM_RETRY_BASE_DELAY
    return base * (2 ** attempt) + random.uniform(0, base)


class BaseAgentLLM:
    """统一 LLM 客户端，支持 OpenAI 及所有兼容接口。"""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
        **kwargs
    ):
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.api_key = api_key or os.getenv("LLM_API_KEY")
        self.base_url = base_url or os.getenv("LLM_BASE_URL")
        self.timeout = timeout or int(os.getenv("LLM_TIMEOUT", "60"))
        # 默认值在调用时求值；用 is None 判空以保留显式传入的 0.0
        self.temperature = cfg.LLM_TEMPERATURE if temperature is None else temperature
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
        """合并调用级参数与默认值；**不修改调用方传入的 kwargs**（此前用 pop 原地改）。"""
        rest = dict(kwargs)
        call_kwargs = {"temperature": rest.pop("temperature", self.temperature)}
        if self.max_tokens:
            call_kwargs["max_tokens"] = rest.pop("max_tokens", self.max_tokens)
        call_kwargs.update(rest)
        return call_kwargs

    def invoke(self, messages: list[dict], **kwargs) -> LLMResponse:
        call_kwargs = self._base_kwargs(kwargs)
        attempt = 0
        while True:
            try:
                return self._client.invoke(messages=messages, **call_kwargs)
            except Exception as e:
                if _is_transient(e) and attempt < cfg.LLM_RETRY_MAX_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    attempt += 1
                    logger.warning(
                        "LLM 调用遇到瞬时错误，%.1fs 后重试（第 %d/%d 次）：%s",
                        delay, attempt, cfg.LLM_RETRY_MAX_ATTEMPTS, e,
                    )
                    time.sleep(delay)
                    continue
                raise LLMException(f"LLM调用失败: {e}") from e

    async def async_stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ):
        call_kwargs = self._base_kwargs(kwargs)
        attempt = 0
        while True:
            yielded = False
            try:
                async for event in self._client.async_stream_with_tools(
                    messages=messages, tools=tools, **call_kwargs
                ):
                    yielded = True
                    yield event
                return
            except Exception as e:
                # 重试边界：只要有一个事件已经流给调用方（前端据此渲染增量文本/
                # 工具调用），失败就不能重试——重新跑一次会把已经展示的内容
                # 重复一遍。未产出任何事件时的失败才是"这次请求压根没打通"，
                # 重试与首次尝试对调用方是等价的。
                if not yielded and _is_transient(e) and attempt < cfg.LLM_RETRY_MAX_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    attempt += 1
                    logger.warning(
                        "LLM 流式调用遇到瞬时错误，%.1fs 后重试（第 %d/%d 次）：%s",
                        delay, attempt, cfg.LLM_RETRY_MAX_ATTEMPTS, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMException(f"LLM流式调用失败: {e}") from e


# ── 辅助小模型 ────────────────────────────────────────────────────
# 用途：上下文压缩摘要、记忆提取、记忆召回筛选等「非主任务」调用。
# 主任务的推理与工具调用始终使用主模型，不受此处配置影响。

_AUX_CACHE: dict[tuple, "BaseAgentLLM"] = {}


def get_aux_llm() -> BaseAgentLLM | None:
    """返回辅助小模型客户端；未配置或构造失败时返回 None（调用方回退主模型）。

    模型名解析优先级：环境变量 LLM_AUX_MODEL_ID > settings.json 的
    LLM_AUX_MODEL_ID；留空即不启用。
    api_key / base_url 默认复用主模型的（LLM_API_KEY / LLM_BASE_URL），
    也可用 LLM_AUX_API_KEY / LLM_AUX_BASE_URL 单独指定（例如换一家更便宜的供应商）。

    构造失败一律返回 None —— 辅助能力不得影响主流程，调用方负责回退主模型。
    失败结果不缓存（避免 env 尚未加载时把 None 永久钉死）。
    """
    model = (os.getenv("LLM_AUX_MODEL_ID") or cfg.LLM_AUX_MODEL_ID or "").strip()
    if not model:
        return None

    api_key  = os.getenv("LLM_AUX_API_KEY")  or os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_AUX_BASE_URL") or os.getenv("LLM_BASE_URL")
    key = (model, api_key, base_url, cfg.LLM_AUX_TEMPERATURE, cfg.LLM_AUX_MAX_TOKENS)

    cached = _AUX_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        llm = BaseAgentLLM(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=cfg.LLM_AUX_TEMPERATURE,
            max_tokens=cfg.LLM_AUX_MAX_TOKENS,
        )
    except Exception:
        return None
    _AUX_CACHE[key] = llm
    return llm


def reset_aux_llm() -> None:
    """清空辅助模型缓存（配置热更新 / 测试隔离时调用）。"""
    _AUX_CACHE.clear()
