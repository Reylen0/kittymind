"""SearXNG 搜索后端（自建实例，https://docs.searxng.org/dev/search_api.html）。

没有官方公共端点——`base_url` 必须指向用户自己部署的实例，通常不需要 API key。
"""

from __future__ import annotations

from .base import SearchResult


class SearxngBackend:
    def __init__(self, base_url: str, *, api_key: str = "", timeout: int = 10) -> None:
        import httpx

        self._url = base_url.rstrip("/") + "/search"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        resp = self._client.get(self._url, params={"q": query, "format": "json"})
        resp.raise_for_status()
        data = resp.json()
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""),
            )
            for item in (data.get("results") or [])[:top_k]
        ]
