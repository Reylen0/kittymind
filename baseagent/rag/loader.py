import os

from baseagent.core.exceptions import RAGException

class Loader:
    """文本加载器"""

    def __init__(self):
        pass

    def load(self, path: str) -> str:
        """从文件中加载文本"""
        if not os.path.exists(path):
            raise RAGException(f"文件不存在: {path}")
        if path.endswith(".txt"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                raise RAGException(f"加载文件失败: {path}. 错误: {str(e)}") from e
        raise RAGException(f"不支持的文件类型: {path}.")

