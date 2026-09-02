from abc import ABC, abstractmethod
import os
from typing import Optional
from openai import OpenAI

from ..core.exceptions import RAGException


class BaseEmbedder(ABC):
    """基础嵌入器类"""
    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """将查询文本转换为向量"""
        pass

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """将文档文本列表转换为向量列表"""
        pass

class OpenAIEmbedder(BaseEmbedder):
    """OpenAI嵌入器类"""
    def __init__(
        self, 
        model: Optional[str] = None, 
        api_key: Optional[str] = None, 
        base_url: Optional[str] = None
    ):
        self.model = model or os.getenv("EMBEDDING_MODEL_ID")
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY")
        self.base_url = base_url or os.getenv("EMBEDDING_BASE_URL")

        # 验证必要参数
        if not self.model:
            raise RAGException("必须提供嵌入模型名称（model参数或EMBEDDING_MODEL_ID环境变量）")
        if not self.api_key:
            raise RAGException("必须提供API密钥（api_key参数或EMBEDDING_API_KEY环境变量）")
        if not self.base_url:
            raise RAGException("必须提供服务地址（base_url参数或EMBEDDING_BASE_URL环境变量）")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

    def embed_query(self, text: str) -> list[float]:
        response = self.client.embeddings.create(
            model=self.model,
            input=text
        )
        return response.data[0].embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(
            model=self.model,
            input=texts
        )
        return [item.embedding for item in response.data]