"""测试全局隔离：默认关闭向量路。

Phase 17M 起，只要环境里配了 `EMBEDDING_*`（开发者本机 `.env` 就是这样），任何
触发记忆提取 / 召回的测试都会真的去调 embedding 端点——慢、依赖网络、结果不确定，
而且会往真实 `~/.kittymind` 写索引库。问题的隐蔽之处在于 `EMBEDDING_*` 是
**进程级环境变量**：只要某个测试导入过 `server/app.py`（它按设计在 import 期
`load_dotenv()`），后面所有测试就都能读到它们，于是污染随测试执行顺序出现。

这里统一清掉这些变量，让测试默认走「未配 embedding」路径——那正是
"行为与改造前逐字节一致"的基准。需要向量路的测试自己注入假 embedder
（见 test_memory_index.py / test_recall_fusion.py）。
"""

import pytest

_EMBEDDING_ENV = (
    "EMBEDDING_MODEL_ID",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_DIM",
    "EMBEDDING_BATCH",
    "EMBEDDING_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _disable_embedding(monkeypatch):
    """每个用例默认无 embedding；结束后清掉单例缓存，避免连接跨用例泄漏。"""
    for key in _EMBEDDING_ENV:
        monkeypatch.delenv(key, raising=False)

    from kittymind.embeddings import reset_embedding
    from kittymind.memory.index import reset_memory_index
    from kittymind.memory.ledger import reset_memory_ledger

    reset_embedding()
    reset_memory_index()
    reset_memory_ledger()
    yield
    reset_embedding()
    reset_memory_index()
    reset_memory_ledger()
