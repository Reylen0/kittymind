"""LLM 调用留档（llm_trace）测试。

覆盖：
  1. 开启时成功调用留档（含请求/响应/耗时/模型/base_url）；
  2. 异常调用也留档（error 字段）；
  3. 关闭时不留档（返回 None 且不写文件）；
  4. 留档不含 api_key（脱敏）。
"""

import json

import pytest

from kittymind.core import llm_trace as lt
from kittymind.core.llm_trace import LLMTraceLog, get_llm_trace_log


@pytest.fixture
def tmp_jsonl(tmp_path):
    return tmp_path / "llm_trace.jsonl"


def _read_lines(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ── LLMTraceLog 基础行为 ──────────────────────────────────────────

def test_record_writes_jsonl(tmp_jsonl):
    log = LLMTraceLog(tmp_jsonl)
    log.record(
        model="claude-sonnet-4-6",
        base_url="https://newapi.dzkjm.cn/",
        stream=False,
        request={"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]},
        response={"content": "hello"},
        duration_ms=123,
    )
    rows = _read_lines(tmp_jsonl)
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "claude-sonnet-4-6"
    assert r["base_url"] == "https://newapi.dzkjm.cn/"
    assert r["stream"] is False
    assert r["response"] == {"content": "hello"}
    assert r["duration_ms"] == 123
    assert r["error"] is None


def test_record_error(tmp_jsonl):
    log = LLMTraceLog(tmp_jsonl)
    log.record(model="m", base_url="", stream=True, request={}, error="HTTP 400")
    r = _read_lines(tmp_jsonl)[0]
    assert r["error"] == "HTTP 400"
    assert r["response"] is None


def test_record_truncates_long_content(tmp_jsonl):
    log = LLMTraceLog(tmp_jsonl)
    log.record(model="m", base_url="", stream=False,
               request={"messages": [{"content": "x" * 10_000}]})
    r = _read_lines(tmp_jsonl)[0]
    content = r["request"]["messages"][0]["content"]
    assert len(content) < 10_000
    assert "truncated" in content


def test_record_never_raises_on_bad_path(tmp_path):
    # 路径是目录（无法 open 写），record 应静默吞掉异常
    bad = tmp_path / "subdir"
    bad.mkdir()
    log = LLMTraceLog(bad)  # __init__ 里 mkdir(parents=True) 幂等，不炸
    log.record(model="m", base_url="", stream=False, request={})  # 不应抛出


# ── get_llm_trace_log 开关 ────────────────────────────────────────

def test_trace_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_ENABLED", False)
    # 清掉可能的单例残留
    lt._instance[0] = None
    assert get_llm_trace_log() is None


def test_trace_enabled_returns_singleton(monkeypatch, tmp_jsonl):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_ENABLED", True)
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    lt._instance[0] = None
    a = get_llm_trace_log()
    b = get_llm_trace_log()
    assert a is b
    assert a is not None


def test_trace_request_excludes_api_key():
    # 留档内容由适配器构造（不含 api_key）；这里验证 _trace 组装后的 request
    # 不携带 key——直接检查适配器层不把 key 放进 request。
    from kittymind.core.llm_adapters import OpenAIAdapter
    adapter = OpenAIAdapter("m", "sk-SECRET-KEY", "https://x/", 10)
    # 构造一次 invoke 请求并捕获留档（用假的 client 避免真实网络）
    class _Msg:
        def __init__(self):
            self.tool_calls = None
            self.content = "ok"
    class _Choice:
        def __init__(self):
            self.message = _Msg()
    class _Resp:
        def __init__(self):
            self.choices = [_Choice()]
    class _Client:
        def __init__(self):
            self.chat = self
        class completions:
            @staticmethod
            def create(**kw):
                return _Resp()
    adapter._client = _Client()
    import tempfile
    from kittymind.config import cfg as _cfg
    old_enabled, old_path = _cfg.LLM_TRACE_ENABLED, _cfg.LLM_TRACE_JSONL
    try:
        with tempfile.TemporaryDirectory() as d:
            _cfg.LLM_TRACE_ENABLED = True
            _cfg.LLM_TRACE_JSONL = __import__("pathlib").Path(d) / "t.jsonl"
            lt._instance[0] = None
            adapter.invoke([{"role": "user", "content": "hi"}])
            row = _read_lines(_cfg.LLM_TRACE_JSONL)[0]
            blob = json.dumps(row, ensure_ascii=False)
            assert "sk-SECRET-KEY" not in blob
    finally:
        _cfg.LLM_TRACE_ENABLED = old_enabled
        _cfg.LLM_TRACE_JSONL = old_path
        lt._instance[0] = None
