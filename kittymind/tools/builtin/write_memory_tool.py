"""write_memory 工具 —— 让 Agent 主动写入一条持久化记忆。"""

from typing import Literal

from pydantic import BaseModel, Field

from ..base import BaseTool
from ...memory.store import MemoryStore


class WriteMemoryToolParam(BaseModel):
    name: str = Field(
        description="记忆的唯一名称，kebab-case 英文短语，如 user-prefers-dark-theme"
    )
    mem_type: Literal["user", "feedback", "project", "reference"] = Field(
        description=(
            "记忆类型：\n"
            "  user       = 用户偏好（语言、风格、习惯等）\n"
            "  feedback   = 行为指导（不要做什么、怎么做更好）\n"
            "  project    = 项目事实（架构决策、模块用途等）\n"
            "  reference  = 外部资源指针（文档链接、工单号等）"
        )
    )
    description: str = Field(description="一行摘要（80字以内），用于记忆目录索引")
    body: str = Field(description="记忆的详细内容")


class WriteMemoryTool(BaseTool):
    """
    写入一条持久化记忆，在未来的会话中可被按需召回。

    适用场景：
    - 用户说"记住这个偏好"
    - 发现值得长期保留的项目事实或用户习惯
    - 记录对后续工作有持续指导意义的原则或资源

    不适合记录只对当前任务有效的临时指令。
    """

    name: str = "write_memory"
    category: str = "工具"
    description: str = (
        "写入一条持久化记忆，在未来的会话中可被按需召回。"
        "适合记录用户偏好、项目事实、可复用的行为指导、外部资源指针。"
        "不适合记录只对当前任务有效的临时指令。"
    )
    param_class = WriteMemoryToolParam

    def __init__(self, memory_store: MemoryStore):
        self._store = memory_store

    def execute(self, parameters: WriteMemoryToolParam) -> str:
        path = self._store.write(
            name=parameters.name,
            mem_type=parameters.mem_type,
            description=parameters.description,
            body=parameters.body,
        )
        print(f"[memory] written: {path.name}", flush=True)
        return f"Memory saved: {parameters.name} ({parameters.mem_type})"
