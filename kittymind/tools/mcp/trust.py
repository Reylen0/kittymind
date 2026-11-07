"""MCP 工具的免审批白名单（叶子模块，无其它内部依赖）。

`permission.py` 单向导入本模块判断是否跳过审批；本模块不导入 `permission.py`
或本包其它文件，避免循环引用（与 `builtin/_dangerous.py` 的约束同理）。

进程级单例：`loader.discover_mcp_tools()` 在发现阶段写入，`check_permission()`
在调用阶段读取。
"""

_AUTO_APPROVE: set[str] = set()


def is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__")


def mark_auto_approve(prefixed_name: str) -> None:
    _AUTO_APPROVE.add(prefixed_name)


def is_auto_approved(name: str) -> bool:
    return name in _AUTO_APPROVE


def reset() -> None:
    """测试 / 重新发现时清空（避免旧进程的白名单跨重连残留）。"""
    _AUTO_APPROVE.clear()
