"""Bing 搜索后端（Azure Cognitive Services Bing Search v7）。"""

from __future__ import annotations

from .base import SearchResult


class BingBackend:
    def __init__(self, api_key: str, *, base_url: str = "", timeout: int = 10) -> None:
        import httpx

        self._url = (base_url or "https://api.bing.microsoft.com").rstrip("/") + "/v7.0/search"
        self._client = httpx.Client(timeout=timeout, headers={"Ocp-Apim-Subscription-Key": api_key})

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        resp = self._client.get(self._url, params={"q": query, "count": top_k})
        resp.raise_for_status()
        data = resp.json()
        pages = (data.get("webPages") or {}).get("value") or []
        return [
            SearchResult(
                title=str(item.get("name") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or ""),
            )
            for item in pages
        ]
