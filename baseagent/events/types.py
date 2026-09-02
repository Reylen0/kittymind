from dataclasses import dataclass, field

AGENT_START = "agent.start"
AGENT_THINKING = "agent.thinking"
AGENT_CHUNK = "agent.chunk"
AGENT_TOOL_CALL = "agent.tool_call"
AGENT_TOOL_RESULT = "agent.tool_result"
AGENT_DONE = "agent.done"
AGENT_ERROR = "agent.error"


@dataclass
class AgentEvent:
    type: str
    data: dict = field(default_factory=dict)
