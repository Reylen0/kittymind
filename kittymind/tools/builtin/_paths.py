"""工具入参路径解析的唯一入口。

为什么必须统一：工作区隔离是这套权限模型的**强约束**（其余软规则都是字符串匹配），
而 `permission._check_path_outside` 以 `bash_cwd`（当前会话工作区）为基准判断越界。
如果某个工具解析相对路径时改用 `os.path.abspath`（进程 CWD），权限判定与实际落点
就会脱钩：检查的是工作区里的路径（判定"合规"），真正写下去的是另一个位置。

Electron 打包后服务端进程的 CWD 通常是用户主目录或安装目录，与用户选的工作区大概率
不同——所以这不是理论风险。**所有文件类工具解析路径一律走 resolve_path()。**
"""

import os
from pathlib import Path

from .bash_tool import bash_cwd


def resolve_path(raw: object) -> str:
    """把工具入参路径解析为绝对路径。

    绝对路径原样规范化；相对路径以当前工作区（bash_cwd）为基准。
    """
    if isinstance(raw, str):
        text = raw.strip()
    else:
        text = "" if raw is None else str(raw).strip()
    if not text:
        text = "."
    if os.path.isabs(text):
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(bash_cwd.get(), text))


def is_inside(path: str, root: str) -> bool:
    """判断 path 是否落在 root 之内（解析符号链接后比较）。"""
    try:
        Path(os.path.abspath(path)).resolve().relative_to(
            Path(os.path.abspath(root)).resolve()
        )
        return True
    except ValueError:
        return False
