from pydantic import BaseModel, Field

from ..core.agent import Agent

from ..tools.base import BaseTool

class RouteParams(BaseModel):
    agent_name: str = Field(description="要调用的Agent名称")
    input_text: str = Field(description="传递给子Agent的输入文本")

class RouteTool(BaseTool):
    name = "route_tool"
    param_class = RouteParams

    def __init__(self, agents: list[Agent]):
        self.agents = {agent.name: agent for agent in agents}
        description = self._build_description() # 动态生成Tool的description,列出所有可用Agent
        super().__init__(description=description)

    def _build_description(self) -> str:
        # 遍历 self.agents,拼出"可用 Agent 列表"文字
        desc = "路由到指定子Agent的工具。可用 Agent 列表(名称 - 描述):\n"
        for agent in self.agents.values():
            desc += f"{agent.name} - {agent.description}\n"
        return desc

    def execute(self, parameters: RouteParams):
        agent_name = parameters.agent_name
        if agent_name not in self.agents:
            raise ValueError(f"Agent '{agent_name}' 不存在。可用 Agent 列表: {list(self.agents.keys())}")
        agent = self.agents[agent_name]
        return agent.run(None, parameters.input_text)

    