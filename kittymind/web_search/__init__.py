"""联网搜索能力接入（Phase 21）。

与 `embeddings/__init__.py` 同构的三个细节：
  - 密钥只从环境变量 `WEB_SEARCH_API_KEY` 读，不进 `settings.json`；
  - 未配置 `WEB_SEARCH_BACKEND` 时 `get_web_search_backend()` 返回 `None`，
    调用方（`web_search` 工具）按 `None` 给出"未配置"的工具结果，不报错中断主流程；
  - **构造失败不缓存**：否则 dotenv 尚未加载、或用户刚填完 key 还没重启时一次
    失败就把 `None` 永久钉死，必须重启进程才能恢复。
"""

from __future__ import annotations

import logging
import os

from ..config import cfg
from .base import BaseWebSearchBackend, SearchResult

logger = logging.getLogger(__name__)

_BACKENDS = {"tavily", "bing", "searxng"}

_CACHE: dict[tuple, BaseWebSearchBackend] = {}
_WARNED: set[str] = set()


def web_search_settings() -> tuple[str, str]:
    """返回 (backend, base_url)；环境变量优先级高于 settings.json。"""
    backend = (os.getenv("WEB_SEARCH_BACKEND") or cfg.WEB_SEARCH_BACKEND or "").strip().lower()
    base_url = (os.getenv("WEB_SEARCH_BASE_URL") or cfg.WEB_SEARCH_BASE_URL or "").strip()
    return backend, base_url


def get_web_search_backend() -> BaseWebSearchBackend | None:
    """返回联网搜索后端；未配置或构造失败时返回 None。"""
    backend, base_url = web_search_settings()
    if not backend:
        return None  # 完全没配 = 用户选择的"关闭联网搜索"，不是错误，不告警

    if backend not in _BACKENDS:
        _warn_once("bad-backend", f"WEB_SEARCH_BACKEND={backend!r} 不是受支持的后端"
                                   f"（可选 {sorted(_BACKENDS)}），联网搜索未启用")
        return None

    api_key = (os.getenv("WEB_SEARCH_API_KEY") or "").strip()
    if backend != "searxng" and not api_key:
        _warn_once("no-key", f"未找到 WEB_SEARCH_API_KEY 环境变量，{backend} 后端未启用")
        return None
    if backend == "searxng" and not base_url:
        _warn_once("no-base-url", "SearXNG 后端需要 WEB_SEARCH_BASE_URL 指向自建实例地址")
        return None

    key = (backend, base_url, api_key, int(cfg.WEB_SEARCH_TIMEOUT))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        client = _build(backend, api_key, base_url)
    except Exception as e:  # 构造失败（缺依赖 / base_url 非法）不得影响主流程
        _warn_once("build-failed", f"联网搜索后端构造失败：{type(e).__name__}: {e}")
        return None

    _CACHE[key] = client
    return client


def _build(backend: str, api_key: str, base_url: str) -> BaseWebSearchBackend:
    timeout = cfg.WEB_SEARCH_TIMEOUT
    if backend == "tavily":
        from .tavily import TavilyBackend
        return TavilyBackend(api_key, base_url=base_url, timeout=timeout)
    if backend == "bing":
        from .bing import BingBackend
        return BingBackend(api_key, base_url=base_url, timeout=timeout)
    from .searxng import SearxngBackend
    return SearxngBackend(base_url, api_key=api_key, timeout=timeout)


def reset_web_search_backend() -> None:
    """清空缓存（配置热更新 / 测试隔离时调用）。"""
    _CACHE.clear()
    _WARNED.clear()


def _warn_once(tag: str, message: str) -> None:
    """同类配置问题只告警一次，避免每次调用工具都刷屏。"""
    if tag in _WARNED:
        return
    _WARNED.add(tag)
    logger.warning("%s", message)


__all__ = [
    "BaseWebSearchBackend",
    "SearchResult",
    "get_web_search_backend",
    "reset_web_search_backend",
    "web_search_settings",
]
