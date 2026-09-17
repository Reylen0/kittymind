r"""危险命令黑名单 —— 全仓唯一事实源（P1-4）。

## 为什么单独一个模块

仓库里曾经有两份**互不一致**的黑名单：

  - A `permission._BASH_HARD_DENY`：8 条明文子串。有 `rm -rf /`、`format c:`，
    **没有** `mkfs` / `dd` / `shutdown` / `reboot`。
  - B `bash_tool._DANGEROUS_PATTERNS`：13 条正则。有 `mkfs` / `dd` / `shutdown`，
    但缺 A 里的 Windows 写法（`rd /s /q c:\`、`del /f /s /q c:\`）。

两份都只在「bash 工具」生效——**`verify` 的 `type=command` 一份都不走**，而它同样把模型给的
字符串交给 `subprocess(shell=True)`，能力等同 bash。于是 `verify` 成了 bash 的无防护镜像：
`mkfs` / `shutdown` / `dd` 在这里一路放行。

现在两边都从这里取表：`DANGEROUS_COMMANDS` 是旧两份的**并集**（只收紧、不放松），
`find_dangerous()` 是唯一判定入口，`permission` 闸门 1 与 `bash_tool` 工具层共用。

本模块**不 import 任何项目内模块**，是叶子模块——`permission` 已经依赖 `bash_tool`
（取 `bash_cwd`），表若放在任一方都会形成循环导入。

## 定位：这是「明显误触」护栏，不是安全边界

必须说清楚它**不是**什么：明文字符串/正则匹配天然可绕——

    rm  -rf  /            （多空格）
    rm -rf --no-preserve-root /
    r''m -rf /            （引号拼接）
    $IFS 拼接、base64 解码后执行、写脚本再跑

这些一条都拦不住，也不该指望能拦住。真正的兜底是**结构性规则**，不在字符串匹配上：

  - `permission._RULES`：工作区之外的任何写操作、删除类命令、系统路径写入
    → 一律走用户审批（`_SHELL_TOOLS` 全体生效）
  - 工作区隔离（`builtin/_paths.py:resolve_path`，唯一基准 `bash_cwd`）

把黑名单继续加长是假安全感：条目越多，越容易让人误以为「危险命令已经被挡住了」。

## 已知的过宽点（有意保留，不是疏漏）

取并集意味着 `rm\s+-\S*r` 这条也进了闸门 1，于是 **`rm -rf build` 这类工作区内的合法清理
会被硬拒绝、连审批机会都没有**。这是刻意选择：

  - 取并集**只收紧、不放松**——任何旧版本能拦住的命令，现在在任何路径上仍然被拦住；
  - 反过来（把递归删除降为闸门 2 的审批项）会让 `ask_fn=None` 的子 Agent 路径
    静默放行 `rm -rf`，那是一次真实的**放松**，不该在"修复不一致"的批次里顺手做。

若日后要改成"可审批"，正确做法是把 `rm\s+-\S*r` 从本表移到 `permission._RULES`，
同时接受子 Agent 路径的放松——那是一个需要单独决策的变更，不是清理动作。
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
