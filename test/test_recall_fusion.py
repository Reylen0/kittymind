"""Phase 17M-3：融合召回与降级链。

最要紧的一条是 `test_auto_without_embedding_matches_legacy_path`：**没配 embedding
的用户行为必须与改造前逐字节一致**。它用一个"改造前逻辑的独立复现"当基准（以
commit ed8a12a 的 recall.py 为准），覆盖 LLM 成功 / 返回空数组 / 返回非 JSON /
返回越界序号 / LLM 抛异常五条分支。

其余覆盖：向量赢 / 关键词赢各一场、四种 mode 的边界、门控、超时降级到关键词。
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kittymind.agent.kitty_agent import KittyAgent
from kittymind.config import cfg
from kittymind.core.llm_json import extract_json_text, log_parse_failure
from kittymind.memory.recall import MemoryRecall
from kittymind.memory.store import MemoryStore
from kittymind.prompts import build_memory_recall_select_prompt


# ── 测试替身 ─────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, reply="[]"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=self.reply)


class StubIndex:
    """可控假索引：直接给定 name → 分，便于构造"向量赢/关键词赢"两种场面。"""

    def __init__(self, scores: dict | None = None, pairs=None):
        self._scores = dict(scores or {})
        self._pairs = pairs
        self.synced: list[list[str]] = []

    def sync(self, memories):
        self.synced.append([m["name"] for m in memories])
        return 0

    def scores(self, query, top_k=0):
        items = sorted(self._scores.items(), key=lambda kv: -kv[1])
        if 0 < top_k < len(items):
            items = items[:top_k]
        return dict(items)

    def near_duplicate_pairs(self, min_sim):
        return self._pairs


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory", selfcheck=False)


def _seed(store):
    store.write("sqlite-store", "project", "存储层用 SQLite + WAL", "迁移链自己实现")
    store.write("windows-encoding", "feedback", "Windows 脚本乱码", "终端编码用 UTF-8")
    store.write("user-name", "user", "用户称呼", "叫小明")


# ── 改造前逻辑的独立复现（回滚对照基准，以 commit ed8a12a 为准）────

def _legacy_llm_select(llm, catalog, user_input, max_relevant):
    if not catalog:
        return []
    prompt = build_memory_recall_select_prompt(catalog, user_input, max_relevant)
    text = ""
    try:
        response = llm.invoke([{"role": "user", "content": prompt}])
        text = (response.content or "").strip()
        payload = extract_json_text(text)
        if payload is None:
            log_parse_failure("记忆召回选择", text, "输出里找不到 JSON 数组")
            return None
        indices = json.loads(payload)
        if isinstance(indices, list):
            return [int(i) for i in indices if isinstance(i, (int, float))]
        log_parse_failure("记忆召回选择", text, "输出不是数组")
    except Exception as e:
        log_parse_failure("记忆召回选择", text, f"{type(e).__name__}: {e}")
    return None


def _legacy_keyword_select(user_input, memories, max_relevant):
    rec = MemoryRecall(None, None)
    words = rec._extract_keywords(user_input)
    if not words:
        return []
    scored = []
    for i, m in enumerate(memories):
        text = f"{m.get('name','')} {m.get('description','')} {m.get('body','')}".lower()
        score = sum(1 for w in words if w in text)
        if score > 0:
            scored.append((score, i))
    scored.sort(reverse=True)
    return [i for _, i in scored[:max_relevant]]


def _legacy_select_relevant(store, llm, user_input, max_relevant):
    memories = store.all_memories()
    if not memories:
        return []
    indices = _legacy_llm_select(llm, store.catalog(), user_input, max_relevant)
    if indices is None:
        indices = _legacy_keyword_select(user_input, memories, max_relevant)
    return [memories[i] for i in indices if 0 <= i < len(memories)][:max_relevant]


# ── 硬要求：无 embedding 时行为不变 ──────────────────────────────

@pytest.mark.parametrize("reply", [
    "[1, 0]",          # 正常
    "[]",              # 模型认为都不相关
    "不是 JSON",        # 解析失败 → 关键词兜底
    "[99, -1, 2]",     # 越界序号 → 只取合法的
    '```json\n[2]\n```',  # 带 markdown 包裹
    RuntimeError("endpoint down"),  # LLM 抛异常 → 关键词兜底
])
def test_auto_without_embedding_matches_legacy_path(store, monkeypatch, reply):
    """auto 且无 embedding：结果必须与改造前逐字节一致。"""
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    _seed(store)

    query = "存储层 迁移链 乱码 称呼"
    rec = MemoryRecall(store, FakeLLM(reply))
    got = [m["name"] for m in rec.select_relevant(query)]
    want = [
        m["name"]
        for m in _legacy_select_relevant(
            store, FakeLLM(reply), query, cfg.MEMORY_RECALL_MAX_RELEVANT
        )
    ]
    assert got == want


def test_auto_without_embedding_uses_llm(store, monkeypatch):
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    _seed(store)
    llm = FakeLLM("[0]")
    rec = MemoryRecall(store, llm)
    assert [m["name"] for m in rec.select_relevant("hi")] == ["sqlite-store"]
    assert len(llm.calls) == 1, "旧路径必须仍然调用 LLM"


def test_auto_without_embedding_never_creates_index_db(store, monkeypatch):
    """auto 模式总会"试一下"向量路，但无 key 时不该真的建出索引库来。"""
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    _seed(store)
    llm = FakeLLM("[0]")

    rec = MemoryRecall(store, llm)
    assert [m["name"] for m in rec.select_relevant("hi")] == ["sqlite-store"]
    assert not (store.directory.parent / "memory_index.db").exists()


# ── 融合 ─────────────────────────────────────────────────────────

def _fusion_store(store):
    """专门为融合测试构造：alpha 只靠向量，beta 只靠关键词。"""
    store.write("alpha-note", "project", "甲甲甲甲甲", "丙丙丙丙丙")
    store.write("beta-note", "project", "乙乙乙乙乙", "丁丁丁丁丁")


def test_vector_wins_when_keywords_miss(store):
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.35, "beta-note": 0.30})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)

    picked = [m["name"] for m in rec.select_relevant("qqq zzz")]

    assert picked[0] == "alpha-note"


def test_keyword_can_overtake_vector(store):
    """向量几乎并列时，关键词命中应该把候选顶上去（融合而不是替换）。"""
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.35, "beta-note": 0.30})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)

    picked = [m["name"] for m in rec.select_relevant("乙乙乙 乙乙")]

    assert picked[0] == "beta-note", "关键词满分应压过 0.05 的向量差"


def test_vector_path_syncs_index_before_search(store):
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.9})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)
    rec.select_relevant("qqq")
    assert index.synced == [["alpha-note", "beta-note"]], "检索前必须懒对账"


def test_ignores_index_entries_for_deleted_memories(store):
    _fusion_store(store)
    index = StubIndex({"ghost": 0.99, "alpha-note": 0.2})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)
    picked = [m["name"] for m in rec.select_relevant("qqq")]
    assert picked == ["alpha-note"], "索引里的陈旧条目不能出现在结果里"


def test_short_circuits_when_index_returns_nothing(store):
    _fusion_store(store)
    llm = FakeLLM("[0]")
    rec = MemoryRecall(store, llm, index_getter=lambda: StubIndex({}))
    picked = [m["name"] for m in rec.select_relevant("qqq")]
    assert picked == ["alpha-note"], "空索引应退回 LLM 选序号"
    assert len(llm.calls) == 1


def test_index_exception_degrades_to_llm(store):
    _fusion_store(store)
    llm = FakeLLM("[0]")

    def boom():
        raise RuntimeError("db locked")

    rec = MemoryRecall(store, llm, index_getter=boom)
    assert [m["name"] for m in rec.select_relevant("qqq")] == ["alpha-note"]
    assert len(llm.calls) == 1, "索引异常必须降级，不能把 turn 拖死"


def test_search_exception_degrades_to_llm(store):
    _fusion_store(store)

    class BrokenIndex(StubIndex):
        def scores(self, query, top_k=0):
            raise RuntimeError("embed endpoint 500")

    def getter():
        return BrokenIndex()

    llm = FakeLLM("[0]")
    rec = MemoryRecall(store, llm, index_getter=getter)
    assert [m["name"] for m in rec.select_relevant("qqq")] == ["alpha-note"]
    assert len(llm.calls) == 1


# ── 四种 mode 的边界 ─────────────────────────────────────────────

def test_vector_mode_never_calls_llm(store):
    _fusion_store(store)
    llm = FakeLLM("[0]")
    backup = cfg.MEMORY_RECALL_MODE
    cfg.MEMORY_RECALL_MODE = "vector"
    try:
        index = StubIndex({"alpha-note": 0.5})
        rec = MemoryRecall(store, llm, index_getter=lambda: index)
        assert [m["name"] for m in rec.select_relevant("qqq")] == ["alpha-note"]
        assert llm.calls == [], "vector 模式不应调用 LLM"
    finally:
        cfg.MEMORY_RECALL_MODE = backup


def test_vector_mode_falls_back_to_keyword_when_unavailable(store):
    _fusion_store(store)
    llm = FakeLLM("[0]")
    backup = cfg.MEMORY_RECALL_MODE
    cfg.MEMORY_RECALL_MODE = "vector"
    try:
        rec = MemoryRecall(store, llm, index_getter=lambda: None)
        assert [m["name"] for m in rec.select_relevant("乙乙乙 乙乙")] == ["beta-note"]
        assert llm.calls == [], "vector 模式即使向量不可用也不该回头调 LLM"
    finally:
        cfg.MEMORY_RECALL_MODE = backup


def test_keyword_mode_touches_neither_index_nor_llm(store):
    _fusion_store(store)
    llm = FakeLLM("[0]")
    touched = []
    backup = cfg.MEMORY_RECALL_MODE
    cfg.MEMORY_RECALL_MODE = "keyword"
    try:
        rec = MemoryRecall(store, llm, index_getter=lambda: touched.append(1))
        assert [m["name"] for m in rec.select_relevant("乙乙乙")] == ["beta-note"]
    finally:
        cfg.MEMORY_RECALL_MODE = backup
    assert llm.calls == [] and touched == [], "隐私模式必须零外发、零索引访问"


def test_llm_mode_ignores_available_index(store):
    _fusion_store(store)
    llm = FakeLLM("[1]")
    index = StubIndex({"alpha-note": 0.99})
    backup = cfg.MEMORY_RECALL_MODE
    cfg.MEMORY_RECALL_MODE = "llm"
    try:
        rec = MemoryRecall(store, llm, index_getter=lambda: index)
        assert [m["name"] for m in rec.select_relevant("qqq")] == ["beta-note"]
        assert index.synced == [], "llm 模式不该碰索引"
    finally:
        cfg.MEMORY_RECALL_MODE = backup


def test_unknown_mode_falls_back_to_auto(store, monkeypatch):
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MODE", "nonsense")
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    _seed(store)
    llm = FakeLLM("[0]")
    rec = MemoryRecall(store, llm)
    assert [m["name"] for m in rec.select_relevant("hi")] == ["sqlite-store"]


# ── 门控（默认全关，打开后生效）──────────────────────────────────

def test_gates_are_off_by_default(store):
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.05, "beta-note": 0.05})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)
    assert rec.select_relevant("qqq") != [], "默认不门控：低分也要给结果"


def test_min_margin_gate_rejects_flat_scores(store, monkeypatch):
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MIN_MARGIN", 0.2)
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.40, "beta-note": 0.39})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)
    assert rec.select_relevant("qqq") == []


def test_min_score_gate_rejects_low_top1(store, monkeypatch):
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MIN_SCORE", 0.5)
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.40, "beta-note": 0.10})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)
    assert rec.select_relevant("qqq") == []


def test_gate_passes_when_margin_is_wide(store, monkeypatch):
    """门控只决定"要不要给结果"，不逐条过滤——间距够宽时低分项照样返回。"""
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MIN_MARGIN", 0.05)
    _fusion_store(store)
    index = StubIndex({"alpha-note": 0.60, "beta-note": 0.20})
    rec = MemoryRecall(store, FakeLLM("[]"), index_getter=lambda: index)
    assert [m["name"] for m in rec.select_relevant("qqq")] == ["alpha-note", "beta-note"]


# ── 超时降级（kitty_agent 层）────────────────────────────────────

def _agent():
    return KittyAgent(name="kitty", llm=MagicMock(), system_prompt="SYS", tools=[])


class SlowRecall:
    """select_relevant 慢慢来；关键词路立刻返回。"""

    def __init__(self, delay=0.3):
        self.delay = delay

    def select_relevant(self, _input):
        time.sleep(self.delay)
        return [{"type": "x", "name": "slow", "body": "b"}]

    def select_relevant_keyword(self, _input):
        return [{"type": "x", "name": "fast", "body": "b"}]

    def build_recall_section(self, memories):
        return " | ".join(m["name"] for m in memories)


async def test_recall_timeout_degrades_to_keyword(monkeypatch):
    monkeypatch.setattr(cfg, "MEMORY_RECALL_TIMEOUT", 0.02)
    agent = _agent()
    agent._memory_recall = SlowRecall(delay=0.3)

    section = await agent._compute_recall_section("hi")

    assert section == "fast", "超时必须用关键词结果兜底，而不是空段"


async def test_recall_no_timeout_uses_vector_result(monkeypatch):
    monkeypatch.setattr(cfg, "MEMORY_RECALL_TIMEOUT", 5.0)
    agent = _agent()
    agent._memory_recall = SlowRecall(delay=0.0)

    assert await agent._compute_recall_section("hi") == "slow"


async def test_recall_timeout_disabled_by_nonpositive(monkeypatch):
    """timeout<=0 表示不设超时（长任务不被截断）。"""
    monkeypatch.setattr(cfg, "MEMORY_RECALL_TIMEOUT", 0)
    agent = _agent()
    agent._memory_recall = SlowRecall(delay=0.05)

    assert await agent._compute_recall_section("hi") == "slow"


async def test_recall_failure_returns_empty_section(monkeypatch):
    monkeypatch.setattr(cfg, "MEMORY_RECALL_TIMEOUT", 5.0)
    agent = _agent()

    class Broken:
        def select_relevant(self, _):
            raise RuntimeError("boom")

        def build_recall_section(self, _):
            return "never"

    agent._memory_recall = Broken()
    assert await agent._compute_recall_section("hi") == ""


async def test_recall_keyword_fallback_failure_is_contained(monkeypatch):
    """兜底路径也抛异常时不能把异常放出去（召回是增强能力）。"""
    monkeypatch.setattr(cfg, "MEMORY_RECALL_TIMEOUT", 0.02)
    agent = _agent()

    class SlowBroken:
        def select_relevant(self, _):
            time.sleep(0.3)

        def select_relevant_keyword(self, _):
            raise RuntimeError("keyword path broke")

        def build_recall_section(self, _):
            return "never"

    agent._memory_recall = SlowBroken()
    assert await agent._compute_recall_section("hi") == ""


async def test_timeout_does_not_cancel_index_build():
    """超时只是不再等结果；后台的 to_thread 任务要继续把索引建完（下一轮就快了）。"""
    done = []

    def slow_index_build(_):
        time.sleep(0.15)
        done.append(True)
        return []

    agent = _agent()
    agent._memory_recall = SimpleNamespace(
        select_relevant=slow_index_build,
        select_relevant_keyword=lambda _q: [],
        build_recall_section=lambda _m: "",
    )
    cfg_backup = cfg.MEMORY_RECALL_TIMEOUT
    cfg.MEMORY_RECALL_TIMEOUT = 0.01
    try:
        assert await agent._compute_recall_section("hi") == ""
        await asyncio.sleep(0.3)
    finally:
        cfg.MEMORY_RECALL_TIMEOUT = cfg_backup
    assert done == [True], "被超时的向量任务必须继续跑完（否则永远建不成索引）"
