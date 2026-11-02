"""网页正文抓取能力测试（无真实网络请求）。

覆盖三层：
  1. extract_text() 纯函数：标签剥离、脚本/样式过滤、标题提取、空白折叠（无网络依赖）
  2. fetch_html()：协议校验、content-type 过滤、字节上限、charset 解码、各类 httpx 异常映射
     —— 用 httpx.MockTransport 而非真实网络，MockTransport 是 httpx 自带的测试设施，不引入新依赖
  3. WebExtractTool：成功/失败两类工具结果的格式化（monkeypatch fetch_html，不碰 httpx 细节）
"""

import httpx
import pytest

from kittymind.tools.builtin.web_extract_tool import WebExtractTool
from kittymind.web_extract import WebExtractError, extract_text, fetch_html


# ── extract_text() ───────────────────────────────────────────────

def test_extract_text_basic_paragraphs():
    html = "<html><body><p>第一段</p><p>第二段</p></body></html>"
    title, text = extract_text(html)
    assert title == ""
    assert text == "第一段\n第二段"


def test_extract_text_strips_script_and_style():
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>alert(1)</script><p>正文内容</p></body></html>"
    )
    _, text = extract_text(html)
    assert text == "正文内容"
    assert "alert" not in text
    assert "color" not in text


def test_extract_text_title():
    html = "<html><head><title>页面标题</title></head><body><p>内容</p></body></html>"
    title, text = extract_text(html)
    assert title == "页面标题"
    assert text == "内容"


def test_extract_text_collapses_whitespace():
    html = "<p>  多个   空格  </p>\n\n<p>   </p>\n<p>下一段</p>"
    _, text = extract_text(html)
    # 行内多空格压成一个，纯空白行整行丢弃
    assert text == "多个 空格\n下一段"


def test_extract_text_decodes_entities():
    html = "<p>A &amp; B &lt;tag&gt;</p>"
    _, text = extract_text(html)
    assert text == "A & B <tag>"


def test_extract_text_br_and_malformed_tags_dont_crash():
    html = "<p>一行<br>二行<br/>三行</p><div><span>未闭合"
    _, text = extract_text(html)
    assert "一行" in text and "二行" in text and "三行" in text


def test_extract_text_empty_html_returns_empty():
    title, text = extract_text("")
    assert title == ""
    assert text == ""


# ── fetch_html() ─────────────────────────────────────────────────

def _mock_client_factory(monkeypatch, handler):
    """把 httpx.Client 的构造替换成强制带上 MockTransport 的版本。"""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


def test_fetch_html_rejects_non_http_scheme():
    with pytest.raises(WebExtractError, match="不支持的 URL 协议"):
        fetch_html("ftp://example.com/x", timeout=5, max_bytes=1000)


def test_fetch_html_success(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content="<html><body><p>你好</p></body></html>".encode("utf-8"),
        )
    _mock_client_factory(monkeypatch, handler)

    html = fetch_html("http://example.com", timeout=5, max_bytes=10_000)
    assert "你好" in html


def test_fetch_html_decodes_declared_charset(monkeypatch):
    body = "<html><body><p>测试</p></body></html>".encode("gbk")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html; charset=gbk"}, content=body)
    _mock_client_factory(monkeypatch, handler)

    html = fetch_html("http://example.com", timeout=5, max_bytes=10_000)
    assert "测试" in html


def test_fetch_html_rejects_non_html_content_type(monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")
    _mock_client_factory(monkeypatch, handler)

    with pytest.raises(WebExtractError, match="非网页内容"):
        fetch_html("http://example.com/file.pdf", timeout=5, max_bytes=10_000)


def test_fetch_html_rejects_over_size_limit(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<p>" + b"x" * 5000 + b"</p>",
        )
    _mock_client_factory(monkeypatch, handler)

    with pytest.raises(WebExtractError, match="字节上限"):
        fetch_html("http://example.com", timeout=5, max_bytes=100)


def test_fetch_html_maps_http_status_error(monkeypatch):
    def handler(request):
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")
    _mock_client_factory(monkeypatch, handler)

    with pytest.raises(WebExtractError, match="HTTP 404"):
        fetch_html("http://example.com/missing", timeout=5, max_bytes=10_000)


def test_fetch_html_maps_timeout(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)
    _mock_client_factory(monkeypatch, handler)

    with pytest.raises(WebExtractError, match="超时"):
        fetch_html("http://example.com", timeout=5, max_bytes=10_000)


# ── WebExtractTool ────────────────────────────────────────────────

def test_tool_success_formats_title_and_text(monkeypatch):
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_extract_tool.fetch_html",
        lambda url, timeout, max_bytes: "<html><head><title>标题</title></head><body><p>正文</p></body></html>",
    )
    result = WebExtractTool().run({"url": "http://example.com"})
    assert result.ok is True
    assert result.content == "# 标题\n\n正文"


def test_tool_success_without_title(monkeypatch):
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_extract_tool.fetch_html",
        lambda url, timeout, max_bytes: "<html><body><p>正文</p></body></html>",
    )
    result = WebExtractTool().run({"url": "http://example.com"})
    assert result.ok is True
    assert result.content == "正文"


def test_tool_empty_body_reports_ok_with_placeholder(monkeypatch):
    monkeypatch.setattr(
        "kittymind.tools.builtin.web_extract_tool.fetch_html",
        lambda url, timeout, max_bytes: "<html><head><script>x</script></head><body></body></html>",
    )
    result = WebExtractTool().run({"url": "http://example.com"})
    assert result.ok is True
    assert "无法提取" in result.content


def test_tool_fetch_failure_returns_ok_false(monkeypatch):
    def boom(url, timeout, max_bytes):
        raise WebExtractError("请求超时（15s）")
    monkeypatch.setattr("kittymind.tools.builtin.web_extract_tool.fetch_html", boom)

    result = WebExtractTool().run({"url": "http://example.com"})
    assert result.ok is False
    assert "超时" in result.content
