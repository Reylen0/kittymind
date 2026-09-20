"""
LLM 输出里的 JSON 解析 —— 集中处理「模型给的不是严格 JSON」这件事。

项目里三处要解析模型输出：记忆提取/整合、记忆召回选择、工具调用参数。
共同步骤是「剥 ``` 围栏 → 按括号截取」，共同纪律是「失败要留痕，不许静默当空值」。
差异在失败语义：工具参数的解析失败必须 **fail-closed**（见 parse_tool_arguments），
其余两处是后台增强，失败退化成降级路径即可。
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_text(text: str, opener: str = "[", closer: str = "]") -> str | None:
    """从模型输出里取出 JSON 片段，找不到返回 None。

    容忍两种常见包装：``` 代码围栏、数组前后的说明文字。
    """
    stripped = (text or "").strip()
    fence = _FENCE_RE.search(stripped)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find(opener), stripped.rfind(closer) + 1
    if start == -1 or end <= start:
        return None
    return stripped[start:end]


def parse_tool_arguments(raw: object) -> tuple[dict | None, str | None]:
    """解析工具调用的 arguments，返回 (args, error)。

    **error 非 None 时调用方必须拒绝执行**，不能兜底成 `{}`：工具会在「空参数」
    下跑起来，而守护栏与权限判定同样是按 args 做的——静默失败等于既绕过检查、
    又执行了一次参数错误的调用。宁可把错误回给模型让它重发。

    arguments 缺省或空串视为合法无参调用（模型对无参工具常省略该字段）。
    """
    if isinstance(raw, dict):
        return raw, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if not isinstance(raw, str):
        return None, f"arguments 类型非法：{type(raw).__name__}"

    try:
        args = json.loads(raw)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(args, dict):
        return None, f"arguments 必须是 JSON 对象，实际是 {type(args).__name__}"
    return args, None


def log_parse_failure(where: str, text: str, detail: str, limit: int = 300) -> None:
    """统一的解析失败日志：带上原始输出的截断片段，便于事后定位。"""
    snippet = (text or "").strip().replace("\n", " ")[:limit]
    logger.warning("%s 解析失败：%s | 原始输出：%s", where, detail, snippet)
