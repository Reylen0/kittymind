"""异常体系"""

class BaseAgentException(Exception):
    """BaseAgents基础异常类"""
    pass

class LLMException(BaseAgentException):
    """LLM相关异常"""
    pass

class AgentException(BaseAgentException):
    """Agent相关异常"""
    pass

class ConfigException(BaseAgentException):
    """配置相关异常"""
    pass

class ToolException(BaseAgentException):
    """工具相关异常"""
    pass

class MemoryException(BaseAgentException):
    """内存相关异常"""
    pass

class RAGException(BaseAgentException):
    """RAG相关异常"""
    pass
