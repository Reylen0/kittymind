"""Embedding 能力接入（Phase 17M）。

只解决一个问题：**谁算向量**。它与「记忆存在哪、谁负责抽取与检索」是两件事
——后者刻意不引入可插拔后端抽象（取舍见 ARCHITECTURE.md §5 Phase 17M）。

密钥只从环境变量 `EMBEDDING_API_KEY` 读，不进 `settings.json`（与
`LLM_AUX_API_KEY` 一致）。未配置时 `get_embedding()` 返回 `None`，调用方按
`None` 退化到旧路径（LLM 选序号 → 2-gram 关键词），**未配 embedding 的用户
行为一字不变**。这是硬要求：记忆能力不能变成"必须再申请一个 key 才能用"。

`get_aux_llm()` 同构的两个细节：
  - **构造失败返回 None 而不是抛错**：辅助能力不得影响主流程；
  - **失败结果不缓存**：否则 dotenv 尚未加载时一次失败就把 None 永久钉死。
"""

from __future__ import annotations

import logging
import os

from ..config import cfg
from .base import BaseEmbedding
from .openai_compat import OpenAICompatEmbedding

logger = logging.getLogger(__name__)

_CACHE: dict[tuple, BaseEmbedding] = {}
_WARNED: set[str] = set()


def embedding_settings() -> tuple[str, str]:
    """返回 (model_id, base_url)；环境变量优先级高于 settings.json。"""
    model = (os.getenv("EMBEDDING_MODEL_ID") or cfg.EMBEDDING_MODEL_ID or "").strip()
    base_url = (os.getenv("EMBEDDING_BASE_URL") or cfg.EMBEDDING_BASE_URL or "").strip()
    return model, base_url


def get_embedding() -> BaseEmbedding | None:
    """返回 embedding 客户端；未配置或构造失败时返回 None。"""
    model, base_url = embedding_settings()
    if not model and not base_url:
        return None  # 完全没配 = 用户选择的"关闭向量路"，不是错误，不告警

    if not model or not base_url:
        _warn_once(
            "half-config",
            "EMBEDDING_MODEL_ID / EMBEDDING_BASE_URL 只配了一半，向量路未启用"
            f"（model={model!r}, base_url={base_url!r}）",
        )
        return None

    api_key = (os.getenv("EMBEDDING_API_KEY") or "").strip()
    if not api_key:
        _warn_once(
            "no-key",
            "未找到 EMBEDDING_API_KEY 环境变量，向量路未启用"
            "（记忆召回退回 LLM 选序号 + 关键词）",
        )
        return None

    key = (
        model, base_url, api_key,
        int(cfg.EMBEDDING_DIM), int(cfg.EMBEDDING_BATCH), int(cfg.EMBEDDING_TIMEOUT),
    )
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        client = OpenAICompatEmbedding(
            model, api_key, base_url,
            dim=cfg.EMBEDDING_DIM,
            batch=cfg.EMBEDDING_BATCH,
            timeout=cfg.EMBEDDING_TIMEOUT,
        )
    except Exception as e:  # 构造失败（SDK 缺依赖 / base_url 非法）不得影响主流程
        _warn_once("build-failed", f"embedding 客户端构造失败：{type(e).__name__}: {e}")
        return None

    _CACHE[key] = client
    return client


def reset_embedding() -> None:
    """清空缓存（配置热更新 / 测试隔离时调用）。"""
    _CACHE.clear()
    _WARNED.clear()


def _warn_once(tag: str, message: str) -> None:
    """同类配置问题只告警一次，避免每轮召回都刷屏。"""
    if tag in _WARNED:
        return
    _WARNED.add(tag)
    logger.warning("%s", message)


__all__ = [
    "BaseEmbedding",
    "OpenAICompatEmbedding",
    "embedding_settings",
    "get_embedding",
    "reset_embedding",
]
