from pydantic import BaseModel, Field

from ..rag.retriever import Retriever

from .base import BaseTool

class RetrieveParams(BaseModel):
    query: str = Field(description="用户的查询文本，用于检索相关信息")

class RetrieveTool(BaseTool):
    name = "retrieve_tool"
    description = "检索工具，用于从知识库中检索相关信息，当用户的问题涉及公司制度时使用本工具。"
    param_class = RetrieveParams

    def __init__(self, retriever: Retriever, description=None):
        self.retriever = retriever
        super().__init__(description=description or self.description)

    def execute(self, parameters: RetrieveParams):
        results = self.retriever.retrieve(parameters.query)
        return "\n".join(r["text"] for r in results)