"""简单Agent实现 - 基于OpenAI原生API"""

from typing import Optional

from ..memory.base import BaseMemory

from ..core.agent import Agent
from ..core.llm import BaseAgentLLM
from ..core.message import Message

class SimpleAgent(Agent):
    """简单的对话Agent"""
    
    def __init__(
        self,
        name: str,
        llm: BaseAgentLLM,
        system_prompt: Optional[str] = None,
        memory: Optional[BaseMemory] = None,
        description: Optional[str] = None,
    ):
        super().__init__(name, llm, system_prompt, memory, description=description)
    
    def run(self, session_id: str | None, input_text: str, **kwargs) -> str:
        """
        运行简单Agent
        
        Args:
            session_id: 会话ID
            input_text: 用户输入
            **kwargs: 其他参数
            
        Returns:
            Agent响应
        """
        # 构建消息列表
        messages = []
        
        # 添加系统消息
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        
        # 添加历史消息
        if session_id is not None:
            for msg in self.get_context(session_id):
                messages.append(msg)
        
        # 添加当前用户消息
        messages.append({"role": "user", "content": input_text})
        
        # 调用LLM
        response = self.llm.invoke(messages, **kwargs)
        
        # 保存到历史记录
        if session_id is not None:
            self.add_message(session_id, Message(input_text, "user"))
            self.add_message(session_id, Message(response.content, "assistant"))
        
        return response.content
    
    def stream_run(self, session_id: str | None, input_text: str, **kwargs):
        """
        流式运行Agent
        
        Args:
            session_id: 会话ID
            input_text: 用户输入
            **kwargs: 其他参数
            
        Yields:
            Agent响应片段
        """
        # 构建消息列表
        messages = []
        
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        if session_id is not None:
            for msg in self.get_context(session_id):
                messages.append(msg)
        
        messages.append({"role": "user", "content": input_text})
        
        # 流式调用LLM
        full_response = ""
        for chunk in self.llm.think(messages, **kwargs):
            full_response += chunk
            yield chunk
        
        # 保存完整对话到历史记录
        if session_id is not None:
            self.add_message(session_id, Message(input_text, "user"))
            self.add_message(session_id, Message(full_response, "assistant"))
