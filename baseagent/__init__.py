"""
BaseAgent —— 轻量、可扩展的 Agent 开发脚手架

快速上手:
    from baseagent import BaseAgentLLM, SimpleAgent, ToolAgent
"""

# LLM
from .core.llm import BaseAgentLLM
from .core.llm_response import LLMResponse
from .core.message import Message

# Agents
from .agent.simple_agent import SimpleAgent
from .agent.tool_agent import ToolAgent
from .agent.supervisor import SuperVisor

# Tools
from .tools.base import BaseTool
from .tools.registry import ToolRegistry
from .tools.executor import ToolExecutor
from .tools.retrieve_tool import RetrieveTool
from .tools.builtin import GetCurrentTimeTool, CalculatorTool, FileReaderTool, HttpTool

# Memory
from .memory.base import BaseMemory
from .memory.buffer_memory import BufferMemory
from .memory.summary_memory import SummaryMemory

# Prompts
from .prompts.template import PromptTemplate

# Callbacks
from .callbacks.base import BaseCallBack

# RAG
from .rag.embedder import BaseEmbedder, OpenAIEmbedder
from .rag.loader import Loader
from .rag.splitter import Splitter
from .rag.vector_store import VectorStore
from .rag.retriever import Retriever

# Exceptions
from .core.exceptions import (
    BaseAgentException,
    LLMException,
    AgentException,
    ToolException,
    MemoryException,
    RAGException,
    ConfigException,
)

__all__ = [
    # LLM
    "BaseAgentLLM",
    "LLMResponse",
    "Message",
    # Agents
    "SimpleAgent",
    "ToolAgent",
    "SuperVisor",
    # Tools
    "BaseTool",
    "ToolRegistry",
    "ToolExecutor",
    "RetrieveTool",
    "GetCurrentTimeTool",
    "CalculatorTool",
    "FileReaderTool",
    "HttpTool",
    # Memory
    "BaseMemory",
    "BufferMemory",
    "SummaryMemory",
    # Prompts
    "PromptTemplate",
    # Callbacks
    "BaseCallBack",
    # RAG
    "BaseEmbedder",
    "OpenAIEmbedder",
    "Loader",
    "Splitter",
    "VectorStore",
    "Retriever",
    # Exceptions
    "BaseAgentException",
    "LLMException",
    "AgentException",
    "ToolException",
    "MemoryException",
    "RAGException",
    "ConfigException",
]
