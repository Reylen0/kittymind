"""Tavily 搜索后端（https://tavily.com）。专为 LLM 场景设计的搜索 API，国内可直连。"""

from __future__ import annotations

from .base import SearchResult


class TavilyBackend:
    def __init__(self, api_key: str, *, base_url: str = "", timeout: int = 10) -> None:
        # 懒导入：没配这个后端的用户不该为它多付启动时间
        import httpx

        self._api_key = api_key
        self._url = (base_url or "https://api.tavily.com").rstrip("/") + "/search"
        self._client = httpx.Client(timeout=timeout)

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        resp = self._client.post(self._url, json={
            "api_key": self._api_key,
            "query": query,
            "max_results": top_k,
        })
        resp.raise_for_status()
        data = resp.json()
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""),
            )
            for item in data.get("results") or []
        ]
