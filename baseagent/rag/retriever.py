from typing import Optional

from .vector_store import VectorStore

from .loader import Loader
from .splitter import Splitter

from .embedder import BaseEmbedder, OpenAIEmbedder


class Retriever:
    """检索器基类"""

    def __init__(
        self,
        embedder: Optional[BaseEmbedder] = None,
        vector_store: Optional[VectorStore] = None,
        splitter: Optional[Splitter] = None,
        loader: Optional[Loader] = None,
    ):
        self.embedder = embedder or OpenAIEmbedder()
        self.vector_store = vector_store or VectorStore()
        self.splitter = splitter or Splitter()
        self.loader = loader or Loader()

    def add_documents(self, path: str):
        """添加文档到向量存储"""
        docs = self.loader.load(path)
        docs = self.splitter.split(docs)
        embeddings = self.embedder.embed_documents(docs)
        self.vector_store.add(docs, embeddings)

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """检索相关文档"""
        query_embedding = self.embedder.embed_query(query)
        results = self.vector_store.query(query_embedding, top_k=top_k)
        return results