"""KittyMind 包根。

KittyAgent 走 **PEP 562 懒导出**：此前顶层 `from .agent import KittyAgent` 会让
`import kittymind.session.store` 这种子模块导入连带拉入 pydantic / openai 整棵
依赖树（实证：未装依赖的解释器导入直接炸）。需要时 `from kittymind import
KittyAgent` 仍然可用——属性访问时才触发真实导入。
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import KittyAgent

__all__ = ["KittyAgent"]


def __getattr__(name: str):
    if name == "KittyAgent":
        from .agent import KittyAgent
        return KittyAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
