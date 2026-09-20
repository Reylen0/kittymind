"""
危险命令黑名单 —— 全仓唯一事实源。
"""

import re

# (正则, 拒绝理由)。正则统一 re.IGNORECASE，理由用于直接展示给用户/模型。
DANGEROUS_COMMANDS: list[tuple[str, str]] = [
    # ── 递归删除 / 根目录 ──────────────────────────────────────
    (r"rm\s+-\S*r", "递归删除（rm -r / rm -rf）"),
    (r"rm\s+/", "删除根目录"),
    (r"rd\s+/s", "Windows 递归删除目录"),
    (r"del\s+/[sqf]", "Windows 强制删除"),
    # ── 提权删除 ───────────────────────────────────────────────
    (r"sudo\s+rm", "以 sudo 执行删除"),
    # ── 磁盘 / 文件系统级破坏 ──────────────────────────────────
    (r"mkfs", "格式化文件系统"),
    (r"dd\s+", "dd 直写设备或文件"),
    (r">\s*/dev/sd", "直接写入块设备"),
    (r"format\s+[a-zA-Z]:", "格式化磁盘"),
    # ── 关机 / 重启 ────────────────────────────────────────────
    (r"shutdown", "关机"),
    (r"reboot", "重启"),
    # ── 危险权限 ───────────────────────────────────────────────
    (r"chmod\s+-R\s+777", "递归设置 777 权限"),
    # ── fork bomb ──────────────────────────────────────────────
    (r":\(\)\s*\{", "fork bomb"),
]

# 单条联合正则：只需要「命中/不命中」，不关心具体是哪个模式。
_RE = re.compile("|".join(f"(?:{p})" for p, _ in DANGEROUS_COMMANDS), re.IGNORECASE)


def find_dangerous(command: str) -> str | None:
    """返回命中的拒绝理由；未命中返回 None。

    逐条匹配而非用联合正则，是为了拿到**具体理由**（联合正则只能给出「命中了」）。
    12 条模式的逐条 search 开销可忽略，换来的可读性值得。
    """
    if not command:
        return None
    for pattern, reason in DANGEROUS_COMMANDS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None


def is_dangerous(command: str) -> bool:
    """快速布尔判定（联合正则一次扫描）。"""
    return bool(_RE.search(command)) if command else False
