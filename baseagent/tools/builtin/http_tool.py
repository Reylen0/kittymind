import urllib.request
import urllib.error
from pydantic import BaseModel, Field

from ..base import BaseTool

_MAX_BYTES = 200_000  # 单次最多读取 200 KB
_TIMEOUT = 10         # 请求超时秒数
_UA = "Mozilla/5.0 (compatible; BaseAgent/1.0)"


class HttpToolParam(BaseModel):
    url: str = Field(description="请求的完整 URL，例如 https://api.example.com/data")
    headers: str = Field(
        default="",
        description="附加请求头，格式为 Key1:Value1,Key2:Value2，留空则不添加"
    )


class HttpTool(BaseTool):
    """HTTP GET 请求工具"""

    name: str = "http_get"
    description: str = "发送 HTTP GET 请求，获取网页或 API 的文本响应"
    param_class = HttpToolParam

    def execute(self, parameters: HttpToolParam) -> str:
        req = urllib.request.Request(parameters.url, headers={"User-Agent": _UA})

        if parameters.headers:
            for item in parameters.headers.split(","):
                item = item.strip()
                if ":" in item:
                    k, v = item.split(":", 1)
                    req.add_header(k.strip(), v.strip())

        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                charset = _parse_charset(resp.headers.get_content_charset())
                raw = resp.read(_MAX_BYTES)
                body = raw.decode(charset, errors="replace")
                truncated = len(raw) >= _MAX_BYTES
                status = resp.status
        except urllib.error.HTTPError as e:
            return f"HTTP 错误 {e.code}: {e.reason}  URL={parameters.url}"
        except urllib.error.URLError as e:
            return f"请求失败: {e.reason}  URL={parameters.url}"
        except Exception as e:
            return f"错误: {e}"

        header = f"[状态: {status}  URL: {parameters.url}{'  (已截断至前 200 KB)' if truncated else ''}]\n"
        return header + body


def _parse_charset(charset: str | None) -> str:
    if charset:
        return charset
    return "utf-8"
