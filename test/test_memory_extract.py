"""记忆提取 / 整合单元测试（无需真实 LLM）。

覆盖三块：
  1. 整合指令的翻译 —— _plan_ops 是纯函数，非法指令只忽略不扩散
  2. 整合的两个安全属性 —— **未被提及的记忆必须保留**（旧实现 delete_all()
     后按模型输出重建，模型少写一条就静默丢记忆）、解析失败必须回滚到快照
  3. 提示词输入的引号归一化 —— 记忆正文里的半角 `"` 不得进入 prompt，
     否则模型会照抄成未转义的裸引号（现场报错 Expecting ',' delimiter）
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kittymind.config import cfg
from kittymind.memory import extract as ex
from kittymind.memory.store import MemoryStore


class FakeLLM:
    """返回固定内容的假模型，同时记录收到的 prompt。"""

    def __init__(self, content: str):
        self.content = content
        self.prompts: list[str] = []

    def invoke(self, messages, **_kwargs):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(content=self.content)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """隔离 cfg.MEMORY_DIR：_dump_raw 落盘位置由它推导，绝不能写到真实 ~/.kittymind。"""
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path / "memory")
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


def _seed(store: MemoryStore, records: list[tuple[str, str, str]]) -> None:
    """records: [(name, description, body)]，type 统一 project。"""
    for name, description, body in records:
        store.write(name, "project", description, body)


def _names(store: MemoryStore) -> list[str]:
    return sorted(m["name"] for m in store.all_memories())


# ── 1. 指令翻译（纯函数） ──────────────────────────────────────────

def test_plan_merge_combines_bodies_and_description():
    by_name = {
        "proj-a": {"name": "proj-a", "type": "project", "description": "A",
                   "body": "路径：G:\\a\n技术栈：Java"},
        "proj-a-scale": {"name": "proj-a-scale", "type": "project", "description": "规模",
                         "body": "技术栈：Java\n日活 1500 万"},
        "user-name": {"name": "user-name", "type": "user", "description": "名字", "body": "用户叫小明"},
    }
    ops = [{"op": "merge", "into": "proj-a", "from": ["proj-a-scale"], "description": "A 项目与规模"}]

    updates, deletions = ex._plan_ops(ops, by_name)

    assert [u["name"] for u in updates] == ["proj-a"]
    assert updates[0]["description"] == "A 项目与规模"
    assert updates[0]["body"].splitlines() == ["路径：G:\\a", "技术栈：Java", "日活 1500 万"]  # 重复行已去重
    assert deletions == ["proj-a-scale"]


def test_plan_drop_only_removes_named_memory():
    by_name = {
        "a": {"name": "a", "type": "project", "description": "d", "body": "b"},
        "b": {"name": "b", "type": "project", "description": "d", "body": "b"},
    }
    updates, deletions = ex._plan_ops([{"op": "drop", "name": "b"}], by_name)
    assert updates == [] and deletions == ["b"]


def test_plan_ignores_invalid_ops():
    by_name = {
        "a": {"name": "a", "type": "project", "description": "d", "body": "b"},
        "b": {"name": "b", "type": "project", "description": "d", "body": "b"},
    }
    ops = [
        "不是对象",
        {"op": "drop", "name": "不存在的名字"},
        {"op": "merge", "into": "也不存在", "from": ["a"]},
        {"op": "merge", "into": "a", "from": ["不存在", "a"]},  # 来源全非法（含自身）
        {"op": "unknown", "name": "a"},
        {"op": "keep", "name": "a"},
    ]
    updates, deletions = ex._plan_ops(ops, by_name)
    assert updates == [] and deletions == []


def test_plan_never_drops_a_merged_target():
    """同一批里既 merge 到 A 又 drop A：drop 必须被忽略，否则合并内容会丢。"""
    by_name = {
        "a": {"name": "a", "type": "project", "description": "d", "body": "b-a"},
        "b": {"name": "b", "type": "project", "description": "d", "body": "b-b"},
    }
    ops = [
        {"op": "merge", "into": "a", "from": ["b"]},
        {"op": "drop", "name": "a"},
    ]
    updates, deletions = ex._plan_ops(ops, by_name)
    assert [u["name"] for u in updates] == ["a"]
    assert deletions == ["b"]


# ── 2. 落盘与安全属性 ────────────────────────────────────────────

def test_consolidate_merge_and_drop(store):
    _seed(store, [
        ("proj-a", "A 项目", "路径：G:\\a"),
        ("proj-a-scale", "A 的规模", "日活 1500 万"),
        ("stale-note", "过时笔记", "已经作废"),
    ])
    llm = FakeLLM(json.dumps([
        {"op": "merge", "into": "proj-a", "from": ["proj-a-scale"], "description": "A 项目与规模"},
        {"op": "drop", "name": "stale-note"},
    ]))

    ex._consolidate(llm, store)

    assert _names(store) == ["proj-a"]
    merged = store.read("proj-a")
    assert merged["description"] == "A 项目与规模"
    assert "日活 1500 万" in merged["body"] and "路径：G:\\a" in merged["body"]


def test_consolidate_keeps_unmentioned_memories(store):
    """回归：旧实现 delete_all() 后按模型输出重建，模型漏写的记忆会被静默删除。"""
    _seed(store, [
        ("keep-me-1", "第一条", "正文一"),
        ("keep-me-2", "第二条", "正文二"),
        ("merge-src", "第三条", "正文三"),
    ])
    llm = FakeLLM(json.dumps([
        {"op": "merge", "into": "keep-me-1", "from": ["merge-src"], "description": "合并后"},
    ]))

    ex._consolidate(llm, store)

    assert _names(store) == ["keep-me-1", "keep-me-2"]  # 未被提及的 keep-me-2 必须原样在
    assert store.read("keep-me-2")["body"] == "正文二"


def test_consolidate_rolls_back_on_bad_json(store, tmp_path):
    """未转义双引号 = 现场那次失败的形态，必须回滚且留档原始输出。"""
    records = [
        ("reply-end-with-miao", '每次回复结尾加上"喵！"', '用户要求加上"喵！"作为结尾。'),
        ("user-name", "用户的名字", "用户叫小明"),
    ]
    _seed(store, records)
    before = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "memory").glob("*.md")}
    index_before = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")

    # 故意留一处未转义的双引号（= 现场那次失败的形态）
    bad = (
        '[\n  {\n    "op": "merge",\n    "into": "user-name",\n'
        '    "from": ["reply-end-with-miao"],\n'
        '    "description": "用户叫"小明""\n  }\n]'
    )
    llm = FakeLLM(bad)

    ex._consolidate(llm, store)  # 不应抛异常

    after = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "memory").glob("*.md")}
    assert after == before
    assert (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8") == index_before
    dump = tmp_path / "consolidate-failed-raw.txt"
    assert dump.is_file() and "JSONDecodeError" in dump.read_text(encoding="utf-8")


def test_consolidate_returns_early_on_non_array(store, tmp_path):
    _seed(store, [("a", "d", "b"), ("b", "d", "b")])
    ex._consolidate(FakeLLM('{"op": "drop", "name": "a"}'), store)
    assert _names(store) == ["a", "b"]  # 非数组 → 不动数据
    assert (tmp_path / "consolidate-failed-raw.txt").is_file()


def test_consolidate_skips_when_single_memory(store):
    _seed(store, [("only", "d", "b")])
    llm = FakeLLM("[]")
    ex._consolidate(llm, store)
    assert llm.prompts == []  # 不足两条直接返回，不该白调模型


def test_consolidate_dumps_when_no_array(store, tmp_path):
    _seed(store, [("a", "d", "b"), ("b", "d", "b")])
    ex._consolidate(FakeLLM("我觉得这些记忆都挺好的。"), store)
    assert _names(store) == ["a", "b"]
    dump = (tmp_path / "consolidate-failed-raw.txt").read_text(encoding="utf-8")
    assert "no-json-array" in dump and "我觉得这些记忆都挺好的。" in dump


# ── 3. 提示词输入归一化与触发时机 ────────────────────────────────

def test_prompt_input_has_no_halfwidth_quotes(store):
    """正文里的半角引号必须换成中文引号后再进 prompt（模型无从照抄裸引号）。"""
    _seed(store, [
        ("reply-end-with-miao", '每次回复结尾加上"喵！"', '用户要求加上"喵！"作为结尾。'),
        ("user-name", "用户的名字", "用户叫小明"),
    ])
    llm = FakeLLM("[]")

    ex._consolidate(llm, store)

    records = llm.prompts[0].split("以下是现有的记忆记录：")[1]
    assert '"喵！"' not in records
    assert "“喵！”" in records


def test_to_cjk_quotes_alternates():
    assert ex._to_cjk_quotes('他说"你好"，又说"再见"') == "他说“你好”，又说“再见”"
    assert ex._to_cjk_quotes("没有引号") == "没有引号"


def test_llm_extract_logs_failure(caplog):
    llm = FakeLLM('[\n  {"name": "x" "type": "user"}\n]')
    with caplog.at_level("WARNING", logger="kittymind.memory.extract"):
        assert ex._llm_extract("对话内容", llm) == []
    assert any("记忆提取失败" in r.message for r in caplog.records)


def test_extract_triggers_consolidate_at_threshold(store, monkeypatch):
    """写够阈值后必须触发整合（钉住 extract_memories → _consolidate 的连线）。"""
    called: list[int] = []
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_THRESHOLD", 1)
    monkeypatch.setattr(ex, "_consolidate", lambda llm, st: called.append(st.count()))

    _seed(store, [("old", "旧记忆", "正文")])
    llm = FakeLLM(json.dumps([
        {"name": "new-one", "type": "project", "scope": "persistent",
         "description": "新记忆", "body": "正文"},
    ]))
    assert ex.extract_memories([{"role": "user", "content": "你好"}], llm, store) is True
    assert called == [2]


def test_dump_never_writes_into_memory_dir(store, tmp_path):
    """留档文件不能落在记忆目录里，否则会污染 all_memories / count / 快照。"""
    _seed(store, [("a", "d", "b"), ("b", "d", "b")])
    ex._consolidate(FakeLLM("不是 JSON"), store)
    assert not list(Path(tmp_path / "memory").glob("*consolidate*"))
    assert (tmp_path / "consolidate-failed-raw.txt").is_file()
