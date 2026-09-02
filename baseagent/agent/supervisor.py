from typing import Optional

from ..memory.base import BaseMemory

from ..core.llm import BaseAgentLLM
from ..prompts.hub import DEFAULT_SUPERVISOR_PROMPT

from ..core.agent import Agent
from ..tools.route_tool import RouteTool
from ..agent.tool_agent import ToolAgent

class SuperVisor(ToolAgent):
    """全局控制agent，主要用于分配任务和管理子agent"""
    
    def __init__(
            self,
            name: str,
            llm: BaseAgentLLM,
            agents: list[Agent],
            system_prompt: Optional[str] = None,
            memory: Optional[BaseMemory] = None,
            max_iterations: int = 10
    ):
        route_tool = RouteTool(agents)
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt or DEFAULT_SUPERVISOR_PROMPT,
            tools=[route_tool],   # 唯一的工具就是路由工具
            memory=memory,
            max_iterations=max_iterations
        )