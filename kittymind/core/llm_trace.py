"""LLM 调用留档：把每次真实调用的请求参数与响应结果追加到 JSONL。

用于排查「模型侧 vs 代码侧」的疑难问题——例如中转站返回 400（system 角色
校验失败）这类错误，若没有请求/响应的完整留档，只能靠猜；有了留档一眼能定位
是请求体构造错还是服务端拒绝。

设计对齐 tools/audit.py 的既定约定：
  - 追加写 JSONL，一行一个调用，便于 tail / jq / grep 排查；
  - 写入失败绝不影响 LLM 主流程（record 内部吞异常）；
  - 线程安全（invoke 可能经 to_thread 并发，异步流式也可能并行）。
  - 三档级别（LLM_TRACE_LEVEL）：`off` 不记录；`on_error` 只记失败调用（默认，
    日常运行不堆积文件又不丢排查线索）；`all` 成功失败都记。级别判断放在
    调用方 `_trace()`（llm_adapters.py）而不是 `LLMTraceLog.record()`——
    record 只管"写一行"，是否该写由上层按本次调用有没有 error 决定。

记录内容不含 API key。messages 的 content 会截断，避免单条 trace 无限膨胀。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import cfg

logger = logging.getLogger(__name__)

# 单条消息 content / 单次响应文本的截断上限（字符），超出打省略号
_MAX_CONTENT_CHARS = 4_000

TRACE_LEVELS = ("off", "on_error", "all")
_WARNED_LEVELS: set[str] = set()


def trace_level() -> str:
    """规范化后的留档级别；非法值告警一次（同一错误值只告一次）并按 on_error 处理。"""
    raw = str(cfg.LLM_TRACE_LEVEL or "").strip().lower()
    if raw in TRACE_LEVELS:
        return raw
    if raw not in _WARNED_LEVELS:
        _WARNED_LEVELS.add(raw)
        logger.warning(
            "LLM_TRACE_LEVEL=%r 不是合法级别（off/on_error/all），按 on_error 处理",
            cfg.LLM_TRACE_LEVEL,
        )
    return "on_error"


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
    """LLM 调用留档器（JSONL 追加，带体积轮转）。"""

    def __init__(self, jsonl_path: Path, max_bytes: int | None = None):
        self._jsonl_path = Path(jsonl_path)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        # None → 读配置；显式传值便于测试
        self._max_bytes = cfg.LLM_TRACE_MAX_BYTES if max_bytes is None else max_bytes
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
                self._rotate_if_needed()
                with self._jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _rotate_if_needed(self) -> None:
        """超过上限就把当前文件改名成 `.1`（覆盖上一份轮转文件）。

        留档是排查用的临时材料，不是审计凭证——保留"当前 + 上一份"足够回看，
        比无限增长导致磁盘被悄悄吃掉要好。轮转只在写入前检查，热路径上就是
        一次 stat，代价可忽略。
        """
        if self._max_bytes <= 0:
            return
        try:
            if self._jsonl_path.stat().st_size < self._max_bytes:
                return
            rotated = self._jsonl_path.with_name(self._jsonl_path.name + ".1")
            if rotated.exists():
                rotated.unlink()
            self._jsonl_path.rename(rotated)
        except OSError:
            pass


# ── 进程级单例（所有适配器共享一份日志；None 表示关闭） ──
# 用单元素列表做可变 holder，避免在函数里用 global 语句（PLW0603）。

_instance: list[LLMTraceLog | None] = [None]
_instance_lock = threading.Lock()


def get_llm_trace_log() -> LLMTraceLog | None:
    """返回留档器；级别为 off 时返回 None（调用方跳过留档）。

    on_error/all 都要返回同一个单例——on_error 下"这次该不该写"由调用方
    拿到的 error 决定，不是在这里就能判断的，所以级别只影响"要不要有这个
    对象"，不影响"对象能不能写"（那是 record 的事，它始终无条件写）。
    """
    if trace_level() == "off":
        return None
    if _instance[0] is None:
        with _instance_lock:
            if _instance[0] is None:
                _instance[0] = LLMTraceLog(cfg.LLM_TRACE_JSONL)
    return _instance[0]
