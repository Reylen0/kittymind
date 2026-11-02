"""网页正文抓取：httpx 直连下载 + 标准库 HTMLParser 剥标签。

与 web_search/ 的多后端抽象不同——这里只有一种实现（直接 GET 网页 HTML），
没有"几家供应商选一个"的需求，没必要为单一实现套一层 Protocol/工厂。

不是 readability：只剔除肯定非正文的 script/style/svg 等不可读内容，不做
导航栏/广告等版式噪音的语义剔除——那需要启发式打分，复杂度与本工具"抓完整
正文"的定位（而非"精确提取文章主体"）不匹配，真需要时再升级成第三方库。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "nav", "table",
    "blockquote", "pre", "ul", "ol",
})

_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)


class WebExtractError(Exception):
    """抓取/解析失败，工具层据此给出用户可读的失败原因（不是裸异常堆栈）。"""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._chunks: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        self._open(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "title":
            self._in_title = False

    def _open(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "title":
            self._in_title = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            if not self.title:
                self.title = data.strip()
            return
        self._chunks.append(data)

    def get_text(self) -> str:
        # block 标签已经插了换行标记；这里只负责把每行内部的连续空白压成一个
        # 空格、去掉纯空白行——真正的"正文 vs 导航"取舍不在这层做。
        lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(self._chunks).splitlines())
        return "\n".join(ln for ln in lines if ln)


def extract_text(html: str) -> tuple[str, str]:
    """HTML → (title, 正文纯文本)。

    HTMLParser 对畸形标签本就很宽容；真触发异常的基本是非 HTML 内容被
    误当网页喂进来，这里按"提取不到正文"处理而不是向上抛，调用方只需要
    区分"抓取失败"（WebExtractError）与"抓到了但没正文"两种情况。
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.title, parser.get_text()


def _looks_like_html(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    return ct in ("text/html", "application/xhtml+xml") or ct.startswith("text/")


def fetch_html(url: str, *, timeout: float, max_bytes: int) -> str:
    """下载网页并解码为文本。

    流式读取、边读边累计字节数，一旦超过 max_bytes 立即中断连接——不等整页
    下完再截断：对方若是个几百 MB 的大文件，那样会先把带宽和内存都吃掉。
    """
    import httpx  # 懒导入：与 web_search/*.py 各后端一致的风格

    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise WebExtractError(f"不支持的 URL 协议：{scheme or url!r}（仅支持 http/https）")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; KittyMind/1.0; +web_extract tool)"}
    content_type = ""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client, \
                client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if content_type and not _looks_like_html(content_type):
                raise WebExtractError(f"非网页内容（content-type: {content_type}），跳过抓取正文")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise WebExtractError(f"网页正文超过 {max_bytes} 字节上限，已中断下载")
                chunks.append(chunk)
            raw = b"".join(chunks)
    except httpx.TimeoutException as e:
        raise WebExtractError(f"请求超时（{timeout}s）") from e
    except httpx.HTTPStatusError as e:
        raise WebExtractError(f"HTTP {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise WebExtractError(f"请求失败：{type(e).__name__}: {e}") from e

    m = _CHARSET_RE.search(content_type)
    charset = m.group(1) if m else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
