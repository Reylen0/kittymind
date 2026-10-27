"""联网搜索工具。后端由 WEB_SEARCH_BACKEND 配置决定，工具层不关心具体是哪家。"""

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ...config import cfg
from ...web_search import get_web_search_backend


class WebSearchToolParam(BaseModel):
    query: str = Field(description="搜索关键词")
    top_k: int = Field(default=5, ge=1, le=10, description="返回结果条数（上限由服务端配置）")


class WebSearchTool(BaseTool):
    """联网搜索，返回标题/链接/摘要列表。"""
    name: str = "web_search"
    description: str = (
        "联网搜索，返回若干条「标题 + 链接 + 摘要」。"
        "只返回搜索引擎的摘要文本，不会打开网页抓取完整正文。"
    )
    param_class = WebSearchToolParam
    is_concurrency_safe = True  # 只读、无本地副作用，可同批并行

    def execute(self, parameters: WebSearchToolParam) -> ToolResult:
        backend = get_web_search_backend()
        if backend is None:
            return ToolResult(
                False,
                "联网搜索未配置：设置环境变量 WEB_SEARCH_BACKEND"
                "（tavily/bing/searxng）+ WEB_SEARCH_API_KEY 后重启生效。",
            )

        top_k = min(parameters.top_k, cfg.WEB_SEARCH_MAX_RESULTS)
        try:
            results = backend.search(parameters.query, top_k)
        except Exception as e:
            return ToolResult(False, f"搜索失败: {type(e).__name__}: {e}")

        if not results:
            return ToolResult(True, "(无搜索结果)")

        lines = [
            f"{i}. {r.title}\n   {r.url}\n   {r.snippet}"
            for i, r in enumerate(results, 1)
        ]
        return ToolResult(True, "\n\n".join(lines))
