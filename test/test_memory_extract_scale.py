"""Phase 17M-4/5：提取去重视图与整合预筛（规模化链路）。

这两条链路原来都是 O(全量)：
  - 提取：`existing_catalog[:4000]` 硬截断 ≈ 32 条，之后后半记忆对提取完全失明；
  - 整合：全部正文灌 prompt，300 条实测 68k tokens（小模型上下文必爆）。

这里验证新的候选集路径，以及**无 embedding 时退回改造前行为**。
"""

import json
from types import SimpleNamespace

import pytest

from kittymind.config import cfg
from kittymind.memory import extract as ex
from kittymind.memory.store import MemoryStore


class FakeLLM:
    def __init__(self, reply="[]"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content=self.reply)

    @property
    def last_prompt(self) -> str:
        return self.calls[-1][0]["content"]


class StubIndex:
    def __init__(self, scores=None, pairs=None):
        self._scores = dict(scores or {})
        self._pairs = pairs
        self.syncs = 0
        self.synced_names: list[list[str]] = []

    def sync(self, memories):
        self.syncs += 1
        self.synced_names.append([m["name"] for m in memories])
        return 0

    def scores(self, query, top_k=0):
        items = sorted(self._scores.items(), key=lambda kv: -kv[1])
        if 0 < top_k < len(items):
            items = items[:top_k]
        return dict(items)

    def near_duplicate_pairs(self, min_sim):
        return self._pairs


class BrokenIndex(StubIndex):
    def scores(self, query, top_k=0):
        raise RuntimeError("embed endpoint 500")


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory", selfcheck=False)


def _seed(store, n: int):
    for i in range(n):
        store.write(
            f"mem-{i:03d}", "project",
            f"第{i}条记忆的详细描述文本内容", f"正文{i}",
        )


@pytest.fixture
def stub_index(monkeypatch):
    """把 extract 模块里的索引入口换成可控替身，返回 (holder, index)。"""
    def install(index):
        monkeypatch.setattr(ex, "get_memory_index", lambda *a, **k: index)
        return index
    return install


# ── 17M-4：提取去重视图 ──────────────────────────────────────────

def test_dedup_view_uses_semantic_top_n(store, stub_index, monkeypatch):
    _seed(store, 6)
    stub_index(StubIndex({
        "mem-000": 0.9, "mem-003": 0.8, "mem-001": 0.5,
        "mem-002": 0.4, "mem-004": 0.3, "mem-005": 0.2,
    }))
    monkeypatch.setattr(cfg, "MEMORY_EXTRACT_CATALOG_TOP_N", 2)

    view = ex._dedup_view(store, "随便一段对话")

    assert "mem-000" in view and "mem-003" in view
    assert "mem-005" not in view, "top_n 之外不该进视野"


def test_dedup_view_survives_scale_where_legacy_truncates(store, stub_index):
    """120 条记忆时，被 4000 字符截断掉的"后半部分"依然能进入视野。"""
    _seed(store, 120)
    stub_index(StubIndex({"mem-118": 0.9, "mem-115": 0.8}))

    view = ex._dedup_view(store, "对话")

    assert "mem-118" in view and "mem-115" in view
    legacy = store.catalog()[:4000]
    assert "mem-118" not in legacy, "改造前的 4000 字符截断确实看不到这一条"
    assert "mem-115" not in legacy


def test_dedup_view_without_index_uses_legacy_truncation(store, stub_index):
    _seed(store, 5)
    stub_index(None)
    assert ex._dedup_view(store, "对话") == store.catalog()[:4000]


def test_dedup_view_falls_back_on_search_error(store, stub_index):
    _seed(store, 5)
    stub_index(BrokenIndex())
    assert ex._dedup_view(store, "对话") == store.catalog()[:4000]


def test_dedup_view_falls_back_when_index_empty(store, stub_index):
    _seed(store, 5)
    stub_index(StubIndex({}))
    assert ex._dedup_view(store, "对话") == store.catalog()[:4000]


def test_dedup_view_honours_token_budget(store, stub_index, monkeypatch):
    _seed(store, 5)
    stub_index(StubIndex({f"mem-{i:03d}": 1.0 - i / 10 for i in range(5)}))
    monkeypatch.setattr(cfg, "MEMORY_EXTRACT_CATALOG_TOKENS", 20)

    view = ex._dedup_view(store, "对话")

    kept = [i for i in range(5) if f"mem-{i:03d}" in view]
    assert kept == [0], "预算只够第一条"


def test_dedup_view_skips_stale_entries(store, stub_index):
    _seed(store, 3)
    stub_index(StubIndex({"ghost": 0.99, "mem-001": 0.5}))
    view = ex._dedup_view(store, "对话")
    assert "ghost" not in view and "mem-001" in view


def test_dedup_view_with_no_memories_is_empty(store, stub_index):
    stub_index(StubIndex({"x": 0.9}))
    assert ex._dedup_view(store, "对话") == ""


def test_query_text_keeps_tail_of_long_conversation():
    """长对话截尾：最近的内容最能代表"本轮想干什么"。"""
    long_text = "开" * 3000 + "结尾提到的主题"
    query = ex._query_text(long_text)
    assert query.endswith("结尾提到的主题")
    assert len(query) == ex._QUERY_MAX_CHARS


def test_extract_uses_dedup_view_in_prompt(store, stub_index):
    """连线检查：送进提取 prompt 的是去重视图而不是全量目录。"""
    _seed(store, 6)
    stub_index(StubIndex({"mem-004": 0.9}))
    llm = FakeLLM("[]")

    ex.extract_memories([{"role": "user", "content": "你好"}], llm, store)

    prompt = llm.last_prompt
    assert "mem-004" in prompt
    assert "mem-000" not in prompt


def test_extract_refreshes_index_after_write(store, stub_index):
    index = stub_index(StubIndex())
    llm = FakeLLM(json.dumps([{
        "name": "brand-new", "type": "project", "scope": "persistent",
        "description": "新记忆", "body": "正文",
    }]))

    assert ex.extract_memories([{"role": "user", "content": "你好"}], llm, store) is True
    assert index.syncs >= 1, "写入后必须把新记忆补进索引"


# ── 17M-5：整合预筛 ──────────────────────────────────────────────

def test_consolidate_sends_only_candidate_pairs(store, stub_index, monkeypatch):
    _seed(store, 5)
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_FULL_MAX", 3)
    stub_index(StubIndex(pairs=[("mem-000", "mem-001")]))

    llm = FakeLLM("[]")
    ex._consolidate(llm, store)

    prompt = llm.last_prompt
    assert "mem-000" in prompt and "mem-001" in prompt
    assert "mem-004" not in prompt, "无近重复关系的记忆不该进 prompt"


def test_consolidate_keeps_full_set_below_threshold(store, stub_index, monkeypatch):
    """小库上保留今天的全量行为（更准，也是回滚对照的基准）。"""
    _seed(store, 5)
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_FULL_MAX", 40)
    stub_index(StubIndex(pairs=[("mem-000", "mem-001")]))

    llm = FakeLLM("[]")
    ex._consolidate(llm, store)

    for i in range(5):
        assert f"mem-{i:03d}" in llm.last_prompt


def test_consolidate_keeps_full_set_when_index_unavailable(store, stub_index, monkeypatch):
    _seed(store, 5)
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_FULL_MAX", 3)
    stub_index(None)

    llm = FakeLLM("[]")
    ex._consolidate(llm, store)

    for i in range(5):
        assert f"mem-{i:03d}" in llm.last_prompt


def test_consolidate_skips_llm_when_no_near_duplicates(store, stub_index, monkeypatch):
    _seed(store, 5)
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_FULL_MAX", 3)
    stub_index(StubIndex(pairs=[]))

    llm = FakeLLM("[]")
    ex._consolidate(llm, store)

    assert llm.calls == [], "没有候选对就不该做无意义的 LLM 调用"
    assert store.count() == 5, "跳过的轮次不能动任何记忆"


def test_consolidate_pairs_only_touch_candidates(store, stub_index, monkeypatch):
    """预筛只缩小模型视野：未出现在候选里的记忆必须原样保留。"""
    _seed(store, 5)
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_FULL_MAX", 3)
    stub_index(StubIndex(pairs=[("mem-000", "mem-001")]))

    llm = FakeLLM(json.dumps([
        {"op": "merge", "into": "mem-000", "from": ["mem-001"], "description": "合并后"},
    ]))
    ex._consolidate(llm, store)

    names = {m["name"] for m in store.all_memories()}
    assert names == {"mem-000", "mem-002", "mem-003", "mem-004"}
    assert store.read("mem-000")["description"] == "合并后"
    assert "正文1" in store.read("mem-000")["body"], "被并入的正文要拼进来"


def test_consolidate_pairs_fall_back_on_search_error(store, stub_index, monkeypatch):
    _seed(store, 5)
    monkeypatch.setattr(cfg, "MEMORY_CONSOLIDATE_FULL_MAX", 3)

    class BrokenPairs(StubIndex):
        def near_duplicate_pairs(self, min_sim):
            raise RuntimeError("db locked")

    stub_index(BrokenPairs())
    llm = FakeLLM("[]")
    ex._consolidate(llm, store)
    for i in range(5):
        assert f"mem-{i:03d}" in llm.last_prompt, "预筛失败必须退回全量"


def test_consolidate_does_nothing_below_two_memories(store, stub_index):
    _seed(store, 1)
    llm = FakeLLM("[]")
    ex._consolidate(llm, store)
    assert llm.calls == []
