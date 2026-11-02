"""网页正文抓取工具：与 web_search 互补——那边给标题/链接/摘要列表，拿到感兴趣的
链接后用这个工具抓该页的完整正文。"""

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ...config import cfg
from ...web_extract import WebExtractError, extract_text, fetch_html


class WebExtractToolParam(BaseModel):
    url: str = Field(description="要抓取正文的网页地址（必须是 http/https）")


class WebExtractTool(BaseTool):
    """抓取网页完整正文（标题 + 纯文本），不是摘要。"""
    name: str = "web_extract"
    description: str = (
        "抓取一个网页的完整正文（标题 + 纯文本），用于在 web_search 拿到链接后"
        "深入阅读某一页的详细内容。只支持 http/https 网页，不支持 PDF/图片等非 HTML 内容。"
    )
    param_class = WebExtractToolParam
    is_concurrency_safe = True  # 只读、无本地副作用，可同批并行

    def execute(self, parameters: WebExtractToolParam) -> ToolResult:
        try:
            html = fetch_html(
                parameters.url,
                timeout=cfg.WEB_EXTRACT_TIMEOUT,
                max_bytes=cfg.WEB_EXTRACT_MAX_BYTES,
            )
        except WebExtractError as e:
            return ToolResult(False, str(e))

        title, text = extract_text(html)
        if not text:
            return ToolResult(True, "(网页正文为空或无法提取文本)")
        return ToolResult(True, f"# {title}\n\n{text}" if title else text)
