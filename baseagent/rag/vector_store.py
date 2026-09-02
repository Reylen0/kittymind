import numpy as np


class VectorStore:
    """向量存储类，用于存储和查询向量化的文档"""
    def __init__(self):
        self.vectors = None  # 存储向量
        self.texts: list[str] = []    # 存储对应的文本
        
    def add(self, texts: list[str], vectors: list[list[float]]):
        """添加文档到向量存储"""
        self.texts.extend(texts)
        self.vectors = np.array(vectors) if self.vectors is None else np.vstack([self.vectors, vectors])

    def query(self, query_vector: list[float], top_k: int = 5) -> list[dict]:
        """查询向量存储,返回top_k条最相似的文档"""
        if self.vectors is None or len(self.texts) == 0:
            return []
        
        # 计算余弦相似度
        query_vector_np = np.array(query_vector)
        similarities = self.vectors @ query_vector_np / (np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(query_vector_np))
        
        # 获取top_k索引
        top_k_indices = np.argsort(similarities)[-top_k:][::-1]
        
        # 返回结果
        results = []
        for idx in top_k_indices:
            results.append({
                "text": self.texts[idx],
                "similarity": float(similarities[idx])
            })
        return results