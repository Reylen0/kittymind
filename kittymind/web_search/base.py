"""联网搜索后端接口。

只暴露一件事：query → 结果列表。标题/摘要/url 是三家后端（Tavily/Bing/SearXNG）
共有的最小交集，统一成 `SearchResult` 后工具层完全不用关心具体是哪个后端。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@runtime_checkable
class BaseWebSearchBackend(Protocol):
    """query → 结果列表。实现必须满足：失败抛异常，不返回空列表充当"查无结果"。"""

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        """返回最多 top_k 条结果，按后端自身的相关度排序。

        真的查无结果时返回空列表（这是合法结果，不是错误）；网络 / 鉴权 /
        限流等失败必须抛异常——不要吞成空列表，那样"搜索失败"和"确实没搜到"
        在调用方眼里会变成同一件事，没法分别处理或提示用户。
        """
        ...
