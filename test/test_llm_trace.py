"""LLM 调用留档（llm_trace）测试。

覆盖：
  1. LLMTraceLog.record 本身无条件写（级别判断不在这一层）；
  2. 异常调用也留档（error 字段）；
  3. 三档级别（off/on_error/all）通过 _trace() 端到端生效；
  4. 非法级别值回退 on_error 并告警一次；
  5. 留档不含 api_key（脱敏）。
"""

import json

import pytest

from kittymind.core import llm_trace as lt
from kittymind.core.llm_trace import LLMTraceLog, get_llm_trace_log, trace_level


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


# ── get_llm_trace_log / trace_level 级别 ──────────────────────────

@pytest.fixture(autouse=True)
def clean_trace_state(monkeypatch):
    """每个用例前清掉单例与告警去重集合，避免跨用例互相污染。"""
    lt._instance[0] = None
    lt._WARNED_LEVELS.clear()
    yield
    lt._instance[0] = None
    lt._WARNED_LEVELS.clear()


def test_trace_level_off_returns_none(monkeypatch):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "off")
    assert get_llm_trace_log() is None


def test_trace_level_on_error_returns_singleton(monkeypatch, tmp_jsonl):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "on_error")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    a = get_llm_trace_log()
    b = get_llm_trace_log()
    assert a is b
    assert a is not None


def test_trace_level_all_returns_singleton(monkeypatch, tmp_jsonl):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "all")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    assert get_llm_trace_log() is not None


def test_trace_level_invalid_value_falls_back_to_on_error(monkeypatch, caplog):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "verbose")
    assert trace_level() == "on_error"
    assert "verbose" in caplog.text


def test_trace_level_invalid_value_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "nope")
    trace_level()
    trace_level()
    assert caplog.text.count("LLM_TRACE_LEVEL") == 1


def _fake_openai_adapter(monkeypatch):
    """构造一个 invoke 必定成功、不碰真实网络的 OpenAIAdapter。"""
    from kittymind.core.llm_adapters import OpenAIAdapter

    class _Msg:
        tool_calls = None
        content = "ok"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Client:
        chat = None

        class completions:
            @staticmethod
            def create(**kw):
                return _Resp()

    _Client.chat = _Client
    adapter = OpenAIAdapter("m", "sk-SECRET-KEY", "https://x/", 10)
    adapter._client = _Client()
    return adapter


def test_trace_level_on_error_skips_successful_call(monkeypatch, tmp_jsonl):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "on_error")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    adapter = _fake_openai_adapter(monkeypatch)

    adapter.invoke([{"role": "user", "content": "hi"}])

    assert not tmp_jsonl.exists() or _read_lines(tmp_jsonl) == []


def test_trace_level_on_error_records_failed_call(monkeypatch, tmp_jsonl):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "on_error")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    adapter = _fake_openai_adapter(monkeypatch)
    adapter._client.chat.completions.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))

    with pytest.raises(Exception):
        adapter.invoke([{"role": "user", "content": "hi"}])

    rows = _read_lines(tmp_jsonl)
    assert len(rows) == 1
    assert "boom" in rows[0]["error"]


def test_trace_level_all_records_successful_call(monkeypatch, tmp_jsonl):
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "all")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    adapter = _fake_openai_adapter(monkeypatch)

    adapter.invoke([{"role": "user", "content": "hi"}])

    rows = _read_lines(tmp_jsonl)
    assert len(rows) == 1
    assert rows[0]["error"] is None


def test_trace_request_excludes_api_key(monkeypatch, tmp_jsonl):
    # 留档内容由适配器构造（不含 api_key）；all 级别下成功调用也会写，
    # 这里验证 _trace 组装后的 request 不携带 key。
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_LEVEL", "all")
    monkeypatch.setattr(lt.cfg, "LLM_TRACE_JSONL", tmp_jsonl)
    adapter = _fake_openai_adapter(monkeypatch)

    adapter.invoke([{"role": "user", "content": "hi"}])

    row = _read_lines(tmp_jsonl)[0]
    blob = json.dumps(row, ensure_ascii=False)
    assert "sk-SECRET-KEY" not in blob


# ── 体积轮转（Phase 17：留档文件不能无限增长）─────────────────────

def _write_rows(log, n):
    for i in range(n):
        log.record(model="m", base_url="u", stream=False,
                   request={"i": i, "pad": "x" * 200})


def test_trace_rotates_when_exceeding_limit(tmp_path):
    path = tmp_path / "t.jsonl"
    log = LLMTraceLog(path, max_bytes=600)
    _write_rows(log, 30)

    rotated = path.with_name(path.name + ".1")
    assert rotated.is_file(), "超过上限应轮转出 .1 文件"
    # 轮转是"写下一行之前"检查，当前文件会略微超过上限，但不会无限涨
    assert path.stat().st_size < 2 * 600
    # 轮转文件里保留的是较早的行
    assert _read_lines(rotated)
    assert _read_lines(path)


def test_trace_rotation_keeps_single_previous_file(tmp_path):
    """只保留"当前 + 上一份"：轮转是排查材料，不是审计凭证，不回滚堆积。"""
    path = tmp_path / "t.jsonl"
    log = LLMTraceLog(path, max_bytes=600)
    _write_rows(log, 40)

    assert path.with_name(path.name + ".1").is_file()
    assert not path.with_name(path.name + ".2").exists()


def test_trace_rotation_disabled_with_zero_limit(tmp_path):
    path = tmp_path / "t.jsonl"
    log = LLMTraceLog(path, max_bytes=0)
    _write_rows(log, 30)
    assert not path.with_name(path.name + ".1").exists()
    assert path.stat().st_size > 600

