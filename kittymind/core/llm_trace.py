"""LLM 调用留档：把每次真实调用的请求参数与响应结果追加到 JSONL。

用于排查「模型侧 vs 代码侧」的疑难问题——例如中转站返回 400（system 角色
校验失败）这类错误，若没有请求/响应的完整留档，只能靠猜；有了留档一眼能定位
是请求体构造错还是服务端拒绝。

设计对齐 tools/audit.py 的既定约定：
  - 追加写 JSONL，一行一个调用，便于 tail / jq / grep 排查；
  - 写入失败绝不影响 LLM 主流程（record 内部吞异常）；
  - 线程安全（invoke 可能经 to_thread 并发，异步流式也可能并行）。
  - 默认关闭（LLM_TRACE_ENABLED=False），排查时打开，避免日常运行堆积文件。

记录内容不含 API key。messages 的 content 会截断，避免单条 trace 无限膨胀。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import cfg

# 单条消息 content / 单次响应文本的截断上限（字符），超出打省略号
_MAX_CONTENT_CHARS = 4_000


def _truncate(value: object, limit: int = _MAX_CONTENT_CHARS) -> object:
    """递归截断字符串内容；非字符串原样返回。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...<truncated {len(value)} chars>"
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    return value


class LLMTraceLog:
    """LLM 调用留档器（JSONL 追加）。"""

    def __init__(self, jsonl_path: Path):
        self._jsonl_path = Path(jsonl_path)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, *, model: str, base_url: str, stream: bool, request: dict,
               response: dict | None = None, error: str | None = None,
               duration_ms: int = 0) -> None:
        """写一行 trace。request/response 里不该含 key（调用方脱敏）。"""
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "base_url": base_url,
            "stream": bool(stream),
            "request": _truncate(request),
            "response": _truncate(response) if response is not None else None,
            "error": error,
            "duration_ms": int(duration_ms),
        }
        with self._lock:
            try:
                with self._jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass


# ── 进程级单例（所有适配器共享一份日志；None 表示关闭） ──
# 用单元素列表做可变 holder，避免在函数里用 global 语句（PLW0603）。

_instance: list[LLMTraceLog | None] = [None]
_instance_lock = threading.Lock()


def get_llm_trace_log() -> LLMTraceLog | None:
    """返回留档器；LLM_TRACE_ENABLED 为 False 时返回 None（调用方跳过留档）。"""
    if not cfg.LLM_TRACE_ENABLED:
        return None
    if _instance[0] is None:
        with _instance_lock:
            if _instance[0] is None:
                _instance[0] = LLMTraceLog(cfg.LLM_TRACE_JSONL)
    return _instance[0]
