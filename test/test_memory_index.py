"""Phase 17M-1/2：embedding 客户端与记忆向量索引。

三块：
  1. `OpenAICompatEmbedding` —— 批大小切分、dimensions 透传、**不支持 dimensions 时
     降级重试并学习真实维度**、空输入短路（用假 OpenAI 客户端，不打网络）
  2. `get_embedding()` —— 未配置 / 半配置 / 构造失败一律返回 None，不抛
  3. `MemoryVectorIndex` —— 对账（只重嵌变化项）、模型漂移全量重嵌、孤儿清理、
     库损坏删除重建、近重复候选对、检索闭环
"""

import numpy as np
import pytest
from types import SimpleNamespace
from typing import ClassVar

from kittymind import config as config_module
from kittymind.config import cfg
from kittymind.embeddings import get_embedding, reset_embedding
from kittymind.memory.index import (
    MemoryVectorIndex,
    index_db_path,
    index_text,
    reset_memory_index,
)
from kittymind.memory.store import MemoryStore


# ── 测试替身 ─────────────────────────────────────────────────────

def _vec(text: str, dim: int) -> list[float]:
    """确定性伪向量：字符重叠越多越接近（够用来验证排序，不需要真语义）。"""
    arr = np.zeros(dim, dtype=np.float32)
    for ch in text:
        arr[ord(ch) % dim] += 1.0
    norm = float(np.linalg.norm(arr))
    return (arr / norm if norm else arr).tolist()


class FakeEmbedder:
    """记录每次被要求编码的文本，便于断言"只重嵌了变化的那条"。"""

    def __init__(self, model_id: str = "fake-embed", dim: int = 64):
        self._model_id = model_id
        self._dim = dim
        self.calls: list[list[str]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]):
        self.calls.append(list(texts))
        return [_vec(t, self._dim) for t in texts]

    def embedded(self) -> list[str]:
        return [t for call in self.calls for t in call]


class FakeEmbeddingsAPI:
    def __init__(self, owner: "FakeOpenAI"):
        self._owner = owner

    def create(self, *, model, input, **kwargs):
        self._owner.calls.append({"model": model, "input": list(input), **kwargs})
        if "dimensions" in kwargs and self._owner.reject_dimensions:
            raise RuntimeError("400 Bad Request: unknown parameter dimensions")
        dim = kwargs.get("dimensions", self._owner.server_dim)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=_vec(t, dim)) for t in input],
            usage=SimpleNamespace(prompt_tokens=len(input)),
        )


class FakeOpenAI:
    """假 OpenAI SDK 客户端（shape 与真实一致：.embeddings.create）。"""

    instances: ClassVar[list["FakeOpenAI"]] = []

    def __init__(self, *, server_dim: int = 512, reject_dimensions: bool = False, **kwargs):
        self.calls: list[dict] = []
        self.server_dim = server_dim
        self.reject_dimensions = reject_dimensions
        self.init_kwargs = kwargs
        self.embeddings = FakeEmbeddingsAPI(self)
        FakeOpenAI.instances.append(self)


@pytest.fixture
def fake_openai(monkeypatch):
    """把 openai.OpenAI 换成本地假客户端，可按用例改服务端行为。"""
    FakeOpenAI.instances.clear()
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    yield FakeOpenAI
    FakeOpenAI.instances.clear()


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory", selfcheck=False)


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def index(store, embedder):
    idx = MemoryVectorIndex(index_db_path(store.directory), embedder, maintenance=False)
    yield idx
    idx.close()


def _openai_embedding(**kwargs):
    from kittymind.embeddings.openai_compat import OpenAICompatEmbedding
    defaults = {"dim": 0, "batch": 16, "timeout": 30}
    defaults.update(kwargs)
    return OpenAICompatEmbedding("embedding-3", "key", "http://x", **defaults)


# ── 17M-1：OpenAI 兼容客户端 ─────────────────────────────────────

def test_batch_split_by_configured_size(fake_openai):
    emb = _openai_embedding(batch=2)
    out = emb.embed(["a", "b", "c", "d", "e"])
    client = FakeOpenAI.instances[-1]
    assert [len(c["input"]) for c in client.calls] == [2, 2, 1]
    assert len(out) == 5


def test_dimensions_passed_through(fake_openai):
    emb = _openai_embedding(dim=512)
    emb.embed(["hello"])
    assert FakeOpenAI.instances[-1].calls[0]["dimensions"] == 512
    assert emb.dim == 512


def test_dim_zero_omits_dimensions(fake_openai):
    emb = _openai_embedding(dim=0)
    emb.embed(["hello"])
    assert "dimensions" not in FakeOpenAI.instances[-1].calls[0]


def test_dimensions_unsupported_falls_back_and_learns_dim(fake_openai):
    """服务端不认 dimensions：降级重试一次，并从返回值学习真实维度。"""
    emb = _openai_embedding(dim=512)
    emb.embed(["hello"])  # 先让客户端建出来
    client = FakeOpenAI.instances[-1]
    client.reject_dimensions = True
    client.server_dim = 2048

    out = emb.embed(["重试这一条"])

    assert len(out[0]) == 2048, "应退回服务端默认维度而不是抛错"
    assert emb.dim == 2048, "应从返回值学习真实维度"
    assert "dimensions" in client.calls[-2], "第一次仍带 dimensions（先试才知道不支持）"
    assert "dimensions" not in client.calls[-1], "降级重试必须去掉 dimensions"


def test_embed_empty_short_circuits(fake_openai):
    emb = _openai_embedding(dim=512)
    assert emb.embed([]) == []
    assert not FakeOpenAI.instances[-1].calls


def test_embed_batch_failure_propagates(fake_openai):
    """失败必须抛（不能返回空占位：静默的空向量会变成"召回到完全无关的记忆"）。"""
    emb = _openai_embedding(dim=0)
    client = FakeOpenAI.instances[-1]

    class Boom(FakeEmbeddingsAPI):
        def create(self, **kwargs):
            raise RuntimeError("network down")

    client.embeddings = Boom(client)
    with pytest.raises(RuntimeError, match="network down"):
        emb.embed(["y"])


# ── get_embedding 的配置判定 ─────────────────────────────────────

def test_get_embedding_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    reset_embedding()
    assert get_embedding() is None


def test_get_embedding_none_when_half_configured(monkeypatch):
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "embedding-3")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    reset_embedding()
    assert get_embedding() is None


def test_get_embedding_none_without_api_key(monkeypatch):
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "embedding-3")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "http://x")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    reset_embedding()
    assert get_embedding() is None


def test_get_embedding_none_when_constructor_fails(monkeypatch):
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "embedding-3")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "http://x")
    monkeypatch.setenv("EMBEDDING_API_KEY", "key")
    import openai

    def boom(**kwargs):
        raise RuntimeError("bad base_url")

    monkeypatch.setattr(openai, "OpenAI", boom)
    reset_embedding()
    assert get_embedding() is None, "构造失败必须返回 None，不能影响主流程"


def test_get_embedding_env_wins_over_settings(monkeypatch, fake_openai):
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "from-settings")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "http://settings")
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "from-env")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://env")
    monkeypatch.setenv("EMBEDDING_API_KEY", "key")
    reset_embedding()
    emb = get_embedding()
    assert emb is not None and emb.model_id == "from-env"


# ── 17M-2：索引 —— 路径与检索 ────────────────────────────────────

def test_index_db_path_follows_memory_dir(tmp_path):
    """派生库路径由记忆目录名派生（测试天然隔离，不往 ~/.kittymind 写）。

    v2 起库名是 `<记忆目录名>.db`（真实目录叫 `memory`，于是正好是 `memory.db`）。
    带目录名而不是写死 `memory.db`，是为了同父目录下的两个记忆目录不互相覆盖。
    """
    path = index_db_path(tmp_path / "memory")
    assert path == tmp_path / "memory.db"
    assert index_db_path(tmp_path / "other") == tmp_path / "other.db"


def test_index_text_includes_body_head():
    text = index_text({"type": "user", "name": "n", "description": "d", "body": "正文" * 200}, 300)
    assert text.startswith("[user] n: d")
    assert len(text) < 300 + 40
    assert "正文" in text


def test_sync_then_search_picks_relevant(store, index):
    store.write("sqlite-store", "project", "存储层用 SQLite + WAL", "迁移链自己实现")
    store.write("windows-encoding", "feedback", "Windows 脚本乱码", "终端编码用 UTF-8")
    store.write("user-name", "user", "用户称呼", "叫小明")
    assert index.sync(store.all_memories()) == 3

    scores = index.scores("存储层 迁移链", 3)
    assert max(scores, key=scores.get) == "sqlite-store"
    assert set(scores) == {"sqlite-store", "windows-encoding", "user-name"}


def test_search_on_empty_index_returns_empty(store, index):
    assert index.scores("任意", 5) == {}
    assert index.near_duplicate_pairs(0.9) is None


# ── 17M-2：对账 ──────────────────────────────────────────────────

def test_second_sync_is_noop(store, index, embedder):
    store.write("a", "project", "描述A", "正文A")
    assert index.sync(store.all_memories()) == 1
    embedder.calls.clear()
    assert index.sync(store.all_memories()) == 0
    assert embedder.calls == [], "文件没动过就不该再编码（stat 快路径）"


def test_only_changed_entry_is_reembedded(store, index, embedder):
    store.write("a", "project", "描述A", "正文A")
    store.write("b", "project", "描述B", "正文B")
    index.sync(store.all_memories())
    embedder.calls.clear()

    store.write("a", "project", "描述A", "正文A-改过")

    assert index.sync(store.all_memories()) == 1
    assert len(embedder.embedded()) == 1
    assert embedder.embedded()[0].startswith("[project] a:")


def test_touched_file_does_not_reembed(store, index, embedder):
    """只改 mtime（内容 hash 未变）→ 刷新元数据，不重嵌。"""
    import os
    store.write("a", "project", "描述A", "正文A")
    index.sync(store.all_memories())
    embedder.calls.clear()

    path = store.directory / "a.md"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    assert index.sync(store.all_memories()) == 0
    assert embedder.calls == []


def test_deleted_memory_dropped_from_index(store, index):
    store.write("a", "project", "描述A", "正文A")
    store.write("b", "project", "描述B", "正文B")
    index.sync(store.all_memories())
    assert index.stats()["count"] == 2

    store.delete("b")
    index.sync(store.all_memories())
    assert index.stats()["count"] == 1
    assert "b" not in index.scores("描述B", 5)


def test_model_change_triggers_full_reembed(store, tmp_path):
    store.write("a", "project", "描述A", "正文A")
    store.write("b", "project", "描述B", "正文B")
    db = index_db_path(store.directory)

    first = MemoryVectorIndex(db, FakeEmbedder(model_id="model-1"), maintenance=False)
    assert first.sync(store.all_memories()) == 2
    first.close()

    # 换模型（顺带换维度）：向量不可比，必须整表重嵌
    second = FakeEmbedder(model_id="model-2", dim=32)
    idx = MemoryVectorIndex(db, second, maintenance=False)
    assert idx.sync(store.all_memories()) == 2
    assert idx.stats()["model_id"] == "model-2"
    assert idx.stats()["dim"] == 32
    idx.close()


def test_corrupt_index_db_is_rebuilt(store, tmp_path):
    """索引损坏 → 删库重建；记忆文件不受任何影响。"""
    store.write("a", "project", "描述A", "正文A")
    store.write("b", "project", "描述B", "正文B")
    db = index_db_path(store.directory)

    first = MemoryVectorIndex(db, FakeEmbedder(), maintenance=False)
    first.sync(store.all_memories())
    first.close()
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        if p.exists():
            p.unlink()

    db.write_bytes(b"definitely not a sqlite database" * 40)  # 纯垃圾字节

    idx = MemoryVectorIndex(db, FakeEmbedder())  # maintenance 走默认（开启自检）
    assert idx.sync(store.all_memories()) == 2, "重建后应能重新索引"
    assert store.count() == 2, "记忆文件必须完好"
    assert (store.directory / "a.md").is_file()
    idx.close()


def test_near_duplicate_pairs(store, index):
    store.write("dup-a", "project", "同一件事的两种说法", "正文完全一样的内容")
    store.write("dup-b", "project", "同一件事的两种说法", "正文完全一样的内容")
    store.write("other", "user", "完全无关的主题", "另一套词")
    index.sync(store.all_memories())

    # 实测：dup-a/dup-b 余弦 0.9808，与 other 只有 0.7029（后者是噪声底）
    pairs = index.near_duplicate_pairs(0.9)
    assert pairs is not None
    assert ("dup-a", "dup-b") in [tuple(sorted(p)) for p in pairs]
    assert all("other" not in p for p in pairs)


# ── 单例 ─────────────────────────────────────────────────────────

def test_get_memory_index_none_without_embedding(monkeypatch, tmp_path):
    from kittymind.memory.index import get_memory_index
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "")
    reset_embedding()
    reset_memory_index()
    assert get_memory_index(tmp_path / "memory") is None
    assert not (tmp_path / "memory_index.db").exists(), "没配 embedding 不该建库"


def test_get_memory_index_cached_per_path(monkeypatch, tmp_path, fake_openai):
    from kittymind.memory.index import get_memory_index
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL_ID", "embedding-3")
    monkeypatch.setattr(cfg, "EMBEDDING_BASE_URL", "http://x")
    monkeypatch.setenv("EMBEDDING_API_KEY", "key")
    reset_embedding()
    reset_memory_index()

    a = get_memory_index(tmp_path / "memory")
    b = get_memory_index(tmp_path / "memory")
    c = get_memory_index(tmp_path / "other")
    assert a is not None and a is b, "同一目录应复用同一实例"
    assert c is not a, "不同目录必须各自独立（测试隔离依赖这一点）"
    reset_memory_index()


def test_config_keys_are_overridable():
    """新增配置项必须进 _OVERRIDABLE，否则 settings.json 里配了会被静默忽略。"""
    for key in (
        "EMBEDDING_MODEL_ID", "EMBEDDING_BASE_URL", "EMBEDDING_DIM", "EMBEDDING_BATCH",
        "EMBEDDING_TIMEOUT", "MEMORY_RECALL_MODE", "MEMORY_VECTOR_TOP_K",
        "MEMORY_RECALL_KEYWORD_WEIGHT", "MEMORY_RECALL_TIMEOUT",
        "MEMORY_RECALL_MIN_SCORE", "MEMORY_RECALL_MIN_MARGIN",
        "MEMORY_INDEX_TEXT_BODY_CHARS", "MEMORY_EXTRACT_CATALOG_TOP_N",
        "MEMORY_EXTRACT_CATALOG_TOKENS", "MEMORY_CONSOLIDATE_MIN_SIM",
        "MEMORY_CONSOLIDATE_FULL_MAX",
    ):
        assert key in config_module._OVERRIDABLE, f"{key} 未加入 _OVERRIDABLE"
        assert hasattr(cfg, key), f"{key} 未定义"
