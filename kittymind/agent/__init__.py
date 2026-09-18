"""Agent 包。

KittyAgent 懒导出（PEP 562）：tools/builtin/verify_tool 需要导入
agent.verify.runner，若本包顶层拉起 kitty_agent，会形成
tools → agent → tools.executor 的循环导入链——目前恰好能跑通，但任何
一处改成包级导入就会炸。懒导出后 `kittymind.agent.verify.runner` 不再
连带拉入 kitty_agent，链路彻底断开。
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .kitty_agent import KittyAgent

__all__ = ["KittyAgent"]


def __getattr__(name: str):
    if name == "KittyAgent":
        from .kitty_agent import KittyAgent
        return KittyAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
