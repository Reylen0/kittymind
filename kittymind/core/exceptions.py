class BaseAgentException(Exception):
    pass

class LLMException(BaseAgentException):
    pass

class AgentException(BaseAgentException):
    pass

class ConfigException(BaseAgentException):
    pass

class ToolException(BaseAgentException):
    pass

class MemoryException(BaseAgentException):
    pass
