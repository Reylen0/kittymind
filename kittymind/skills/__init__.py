"""技能系统。

技能是 YAML frontmatter + Markdown 正文的知识/流程文档，存在
`~/.kittymind/skills/<slug>/SKILL.md`。目录（名称+简介）每轮作为独立 system
消息注入，Agent 需要时显式调用 `load_skill(name)` 加载全文——不走记忆召回
那套"LLM 预筛选 + session 缓存"机制，因为目录内容只随技能集合变化，天然
字节级稳定。
"""

from .store import SkillStore

__all__ = ["SkillStore"]
