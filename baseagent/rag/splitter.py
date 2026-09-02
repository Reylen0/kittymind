from baseagent.core.exceptions import RAGException


class Splitter:
    """文本分割器"""

    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        if chunk_size <= 0:
            raise RAGException("chunk_size必须为正整数")
        if chunk_overlap < 0:
            raise RAGException("chunk_overlap必须为非负整数")
        if chunk_overlap >= chunk_size:
            raise RAGException("chunk_overlap必须小于chunk_size")
        
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[str]:
        """使用滑动窗口将文本分割为列表"""
        chunks = []
        for i in range(0, len(text), self.chunk_size - self.chunk_overlap):
            chunk = text[i:i + self.chunk_size]
            if chunk.strip():  # 忽略空白块
                chunks.append(chunk)
        return chunks