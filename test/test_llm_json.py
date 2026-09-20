"""LLM 输出 JSON 解析工具测试（kittymind/core/llm_json.py，无需 LLM）。

覆盖三块：
  1. extract_json_text —— 剥 ``` 围栏、按括号截取、找不到返回 None
  2. parse_tool_arguments —— 失败必须返回 error（fail-closed），不许静默成 {}
  3. 三处调用点的失败语义 —— 工具拒绝执行 / 召回降级但留日志
"""

from types import SimpleNamespace

from kittymind.core.llm_json import extract_json_text, log_parse_failure, parse_tool_arguments
from kittymind.memory.recall import MemoryRecall
from kittymind.memory.store import MemoryStore


class FakeLLM:
    def __init__(self, content: str):
        self.content = content

    def invoke(self, messages, **_kwargs):
        return SimpleNamespace(content=self.content)


# ── 1. extract_json_text ─────────────────────────────────────────

def test_extract_plain_array():
    assert extract_json_text('["a", "b"]') == '["a", "b"]'


def test_extract_strips_code_fence():
    text = '这是结果：\n```json\n[{"op": "keep"}]\n```\n以上。'
    assert extract_json_text(text) == '[{"op": "keep"}]'


def test_extract_tolerates_surrounding_prose():
    assert extract_json_text('我认为 [1, 2] 是答案') == "[1, 2]"


def test_extract_returns_none_without_array():
    assert extract_json_text("这些记忆都挺好，不用改。") is None
    assert extract_json_text("") is None
    assert extract_json_text("[]"[1:]) is None  # 只有右括号


def test_extract_supports_object_delimiters():
    assert extract_json_text('说明 {"a": 1} 结尾', "{", "}") == '{"a": 1}'


# ── 2. parse_tool_arguments（fail-closed）────────────────────────

def test_parse_accepts_dict_and_json_string():
    assert parse_tool_arguments({"a": 1}) == ({"a": 1}, None)
    assert parse_tool_arguments('{"a": 1}') == ({"a": 1}, None)


def test_parse_treats_missing_and_empty_as_no_args():
    """无参工具常省略 arguments —— 这是合法调用，不能报错。"""
    for raw in (None, "", "   "):
        assert parse_tool_arguments(raw) == ({}, None)


def test_parse_rejects_broken_json_with_reason():
    args, error = parse_tool_arguments('{"a": 1,')
    assert args is None
    assert error is not None and "JSONDecodeError" in error


def test_parse_rejects_unescaped_quote():
    """与记忆整合现场同一类错误：字符串里未转义的双引号。"""
    args, error = parse_tool_arguments('{"command": "echo "hi""}')
    assert args is None and "Expecting ',' delimiter" in error


def test_parse_rejects_non_object():
    for raw in ("[1, 2]", "42", '"text"'):
        args, error = parse_tool_arguments(raw)
        assert args is None and error is not None


def test_parse_rejects_non_str_non_dict():
    args, error = parse_tool_arguments(123)
    assert args is None and "类型非法" in error


# ── 3. 调用点：日志与降级 ────────────────────────────────────────

def test_log_parse_failure_truncates_raw(caplog):
    with caplog.at_level("WARNING", logger="kittymind.core.llm_json"):
        log_parse_failure("测试", "x" * 500, "boom", limit=50)
    message = caplog.records[0].message
    assert "测试 解析失败：boom" in message
    assert "x" * 50 in message and "x" * 51 not in message


def test_recall_select_returns_none_and_logs_on_garbage(tmp_path, caplog):
    """召回选择解析失败 → 降级（返回 None 交给关键词兜底）但必须留日志。"""
    recall = MemoryRecall(MemoryStore(tmp_path / "memory"), FakeLLM("这几条都挺相关的。"))
    with caplog.at_level("WARNING", logger="kittymind.core.llm_json"):
        assert recall._llm_select("你好", "1. [user] a: b", 1) is None
    assert any("记忆召回选择" in r.message for r in caplog.records)


def test_recall_select_parses_indices(tmp_path):
    recall = MemoryRecall(MemoryStore(tmp_path / "memory"), FakeLLM("[0, 2]"))
    assert recall._llm_select("你好", "1. [user] a: b", 3) == [0, 2]
