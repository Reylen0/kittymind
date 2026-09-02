from ..prompts.hub import SUMMARIZE_PROMPT_TEMPLATE

from ..core.llm import BaseAgentLLM
from ..core.message import Message
from .base import BaseMemory

class SummaryMemory(BaseMemory):
    """
    基于摘要的会话历史存储
    
    args:
        llm: 用于生成摘要的语言模型
        window_size: 历史消息窗口大小，超过该大小的消息将被摘要
    """
    def __init__(self, llm: BaseAgentLLM, window_size: int = 5):
        self.llm = llm
        self.window_size = window_size
        self.histories: dict[str, list[Message]]= {}
        self.summaries: dict[str, str] = {}
 
    def save(self, session_id: str, message: Message) -> None:
        """保存消息到历史中，并在超过窗口大小时生成摘要"""
        if session_id not in self.histories:
            self.histories[session_id] = []
        self.histories[session_id].append(message)
        if len(self.histories[session_id]) > self.window_size:
            summary = self.summarize(session_id, self.histories[session_id])
            self.summaries[session_id] = summary
            self.histories[session_id].clear()

    def get_history(self, session_id: str) -> list[Message]:
        if session_id not in self.histories:
            return []
        return self.histories[session_id]
    
    def get_context(self, session_id: str) -> list[dict]:
        """获取上下文，包括历史摘要和历史消息"""
        contexts = []
        if self.summaries.get(session_id):
            contexts.append({"role": "system", "content": f"历史对话摘要：{self.summaries[session_id]}"})
        for msg in self.get_history(session_id):
            contexts.append(msg.to_dict())
        return contexts
            
    def summarize(self, session_id: str, messages: list[Message]) -> str:
        """生成摘要"""
        summary_text = self.summaries.get(session_id, '无历史摘要')
        conversation_text = "\n".join([msg.__str__() for msg in messages])
        prompt = SUMMARIZE_PROMPT_TEMPLATE.format(history_summary=summary_text, conversation=conversation_text)
        messages = [{"role": "user", "content": prompt}]
        summary_response = self.llm.invoke(messages=messages)
        return summary_response.content

    def clear(self, session_id: str) -> None:
        if session_id in self.histories:
            self.histories[session_id].clear()
        if session_id in self.summaries:
            del self.summaries[session_id]