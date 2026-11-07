"""load_skill 工具 —— 按名称加载指定技能的完整内容。

system prompt 里已经常驻一份技能目录（名称+简介，见
`KittyAgent._inject_skill_catalog`），模型判断需要某个技能的详细流程/规范时
调用本工具拉取全文。纯只读、确定性返回，不走权限审批——读的是本项目自己
管理的技能目录，不是不受信任的第三方代码（与 file_read 同一信任等级）。
"""

from pydantic import BaseModel, Field

from ...skills.store import SkillStore
from ..base import BaseTool, ToolResult


class LoadSkillInput(BaseModel):
    name: str = Field(description="技能名称，必须与技能目录中列出的名称完全一致")


class LoadSkillTool(BaseTool):
    """加载指定技能的完整内容。"""

    name: str = "load_skill"
    description: str = (
        "加载指定技能的完整内容。system prompt 已列出可用技能目录（名称+简介），"
        "需要某个技能的详细流程/规范时调用本工具加载全文。"
    )
    param_class = LoadSkillInput
    is_concurrency_safe = True  # 只读、给定输入确定性返回，可同批并行

    def __init__(self, store: SkillStore):
        self._store = store

    def execute(self, parameters: LoadSkillInput) -> ToolResult:
        skill = self._store.read(parameters.name)
        if skill is None:
            catalog = self._store.catalog() or "(当前没有可用技能)"
            return ToolResult(False, f"技能 '{parameters.name}' 不存在。可用技能：\n{catalog}")
        return ToolResult(True, skill["body"])
