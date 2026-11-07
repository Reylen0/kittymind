"""write_skill 工具 —— 让 Agent 把可复用的任务流程/规范保存为技能。

四段式结构照抄 write_memory_tool.py：工具层只收参数、调一次 Store.write()，
去重/校验/落盘全部下沉到 SkillStore——这是 ARCHITECTURE.md Phase 25
"Agent 自创建技能"的落地。不走权限审批：与 write_memory 同一信任等级，
写入的是本项目自己管理的受控存储，不是系统路径/shell 这类高风险操作。
"""

import logging

from pydantic import BaseModel, Field

from ...skills.store import SkillStore
from ..base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class WriteSkillInput(BaseModel):
    name: str = Field(description="技能名称，简洁的英文短语，如 'git-commit-flow'")
    description: str = Field(description="一行摘要（80字以内），用于技能目录展示")
    body: str = Field(description="技能的完整 Markdown 正文：使用时机、流程步骤、规范细节")


class WriteSkillTool(BaseTool):
    """
    把一套可复用的任务流程/规范保存为技能，之后在技能目录中可见、可被加载复用。

    适合沉淀反复出现的操作模式；不适合保存一次性的临时指令。
    同名技能会被整篇替换（不是合并）——重复调用等于覆盖更新。
    """

    name: str = "write_skill"
    description: str = (
        "把一套可复用的任务流程/规范保存为技能，之后在技能目录中可见、可被加载复用。"
        "适合沉淀反复出现的操作模式；不适合一次性的临时指令。"
    )
    param_class = WriteSkillInput

    def __init__(self, store: SkillStore):
        self._store = store

    def execute(self, parameters: WriteSkillInput) -> ToolResult:
        path = self._store.write(
            name=parameters.name,
            description=parameters.description,
            body=parameters.body,
        )
        logger.info("写入技能: %s", path.name)
        return ToolResult(True, f"技能已保存：{parameters.name}")
