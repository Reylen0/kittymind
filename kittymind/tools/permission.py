"""权限检查模块。

check_permission(name, args, ask_fn) -> str | None（async）
  返回 None 表示放行；返回拒绝原因字符串表示阻止。

闸门顺序：
  1. 硬拒绝（始终生效，不受 ask_fn 是否配置影响）
  2. 规则匹配（路径越界、删除操作、系统路径写入、chmod 777、
     git 写历史/破坏性参数、剪贴板读写、verify 执行任意命令）
  3. 用户审批：`await ask_fn(...)`；ask_fn=None 时跳过，直接放行——用于没有
     人在场点按钮的场景（无 GUI 的程序化调用、测试），不是给某类 Agent 专用的

注意：硬拒绝是「按命令字符串」检查，凡是能执行模型给定命令的工具都必须列进
_SHELL_TOOLS——漏一个（例如曾经的 verify）就等于给 bash 开了个无防护镜像。
"""

import asyncio
from collections.abc import Awaitable, Callable

from .builtin._dangerous import find_dangerous
from .builtin._paths import is_inside, resolve_path
from .builtin.bash_tool import bash_cwd


# ── 闸门 1：硬拒绝（bash / verify 共用同一份黑名单） ──────────────
# verify 的 type=command 同样把模型给的字符串交给 subprocess(shell=True)，
# 能力等同 bash，因此必须与 bash 一视同仁。
_SHELL_TOOLS: set[str] = {"bash", "verify"}

# 黑名单本体在 builtin/_dangerous.py（全仓唯一事实源，叶子模块）。
# 放在那边而不是这里：permission 依赖 bash_tool 取 bash_cwd，
# 表若定义在本模块，bash_tool 回头 import 就成环。

# git 附加参数中的破坏性 / 改写历史选项（长选项按前缀匹配，短选项按整词匹配）
_GIT_DANGEROUS_FLAGS: list[str] = [
    "--force", "--amend", "--hard", "--delete", "--no-verify",
    "-f", "-d", "-D",
]


# ── 闸门 2：软规则 ─────────────────────────────────────────────
def _check_path_outside(args: dict, key: str = "path") -> bool:
    """路径是否在工作区之外。

    解析基准必须与工具实际落点一致——统一走 _paths.resolve_path（bash_cwd 基准），
    否则会出现「权限检查查的是工作区里的路径，实际写的是另一个位置」。
    """
    return not is_inside(resolve_path(args.get(key) or "."), bash_cwd.get())


def _shell_cmd(args: dict) -> str:
    """取命令文本；verify 仅在 type=command 时才有命令。"""
    return str(args.get("command") or "")


def _git_commits(args: dict) -> bool:
    """仅当确实会生成提交时才触发（message 为空时 git 工具退化为 status）。"""
    return (
        (args.get("action") or "").lower().strip() == "commit"
        and bool((args.get("message") or "").strip())
    )


def _is_action(name: str):
    """生成 action 匹配器（大小写/空白不敏感）。"""
    return lambda args: (args.get("action") or "").lower().strip() == name


_RULES: list[tuple[set, object, str]] = [
    (
        {"file_write", "file_edit"},
        _check_path_outside,  # 唯一参数就是 args，无需 lambda 包装
        "写入路径在工作目录之外",
    ),
    (
        {"file_read"},
        _check_path_outside,  # 唯一参数就是 args，无需 lambda 包装
        "读取路径在工作目录之外",
    ),
    # 读类搜索工具：同样是「把文件内容送到模型」，与 file_read 同级对待。
    # 曾遗漏 glob/grep/ls——三者都能指定 path，等于给了一条无审批的跨工作区读取通道。
    # （glob 的 pattern 另可带 ../ 逃逸，已在 glob_tool 内部按 root 做 containment 过滤。）
    (
        {"glob", "grep", "ls"},
        _check_path_outside,  # 唯一参数就是 args，无需 lambda 包装
        "搜索路径在工作目录之外",
    ),
    (
        _SHELL_TOOLS,
        lambda args: _has_any(_shell_cmd(args), ["rm ", "del ", "rmdir ", "Remove-Item"]),
        "命令包含删除操作",
    ),
    (
        _SHELL_TOOLS,
        lambda args: _has_any(_shell_cmd(args), [
            "> /etc/", "> /usr/", "> /bin/", "> /boot/",
            "> C:\\Windows\\", "> C:\\System32",
        ]),
        "命令向系统路径写入",
    ),
    (
        _SHELL_TOOLS,
        lambda args: "chmod 777" in _shell_cmd(args),
        "命令将权限设置为 777",
    ),
    # git：commit 写入仓库历史（add 仅改索引、可随手 unstage，故不拦截）
    (
        {"git"},
        _git_commits,
        "git commit 会写入仓库历史",
    ),
    # git：附加参数含 --amend / --force / --hard 等破坏性或改写历史的选项
    (
        {"git"},
        lambda args: _has_flag(args.get("extra", ""), _GIT_DANGEROUS_FLAGS),
        "git 附加参数含强制或改写历史的选项",
    ),
    # git：在别的仓库上操作
    (
        {"git"},
        lambda args: _check_path_outside(args, "workdir"),
        "git 工作目录在工作目录之外",
    ),
    # 剪贴板：读取会把用户剪贴板内容（可能含密码等敏感信息）发送给模型
    (
        {"clipboard"},
        _is_action("read"),
        "读取剪贴板会把其内容（可能含敏感信息）发送给模型",
    ),
    # 剪贴板：写入会覆盖用户当前剪贴板内容
    (
        {"clipboard"},
        _is_action("write"),
        "写入剪贴板会覆盖用户当前剪贴板内容",
    ),
    # verify：type=command 时执行任意命令，能力等同 bash，必须过审批
    # （上面三条 _SHELL_TOOLS 规则已覆盖更具体的危险形态，故本条放在最后兜底）
    (
        {"verify"},
        lambda args: (args.get("type") or "command").strip().lower() == "command"
        and bool(_shell_cmd(args).strip()),
        "verify 会执行任意命令（能力等同于 bash）",
    ),
]


def _inside_workdir(path: str, workdir: str) -> bool:
    """兼容旧调用点；实现统一在 _paths.is_inside。"""
    return is_inside(path, workdir)


def _has_any(text: str, patterns: list[str]) -> bool:
    t = text.lower()
    return any(p.lower() in t for p in patterns)


def _has_flag(text: str, flags: list[str]) -> bool:
    """按空白切词匹配命令行选项，避免 '-f' 命中 '--file' 这类子串误判。

    长选项（--x）按前缀匹配，可覆盖 --force-with-lease 这类变形。
    """
    for word in text.split():
        for flag in flags:
            if word == flag or (flag.startswith("--") and word.startswith(flag)):
                return True
    return False


def _short(val: object, limit: int = 80) -> str:
    s = str(val)
    return s[:limit] + "…" if len(s) > limit else s


# ── 默认 ask_fn：CLI 交互 ──────────────────────────────────────
async def _cli_ask(tool_name: str, args: dict, reason: str) -> bool:
    """阻塞的 `input()` 挪进线程池执行，不占事件循环。"""
    display = {k: (_short(v, 100) if k != "content" else f"<{len(str(v))} chars>") for k, v in args.items()}
    print(f"\n[WARN] {reason}")
    print(f"    tool : {tool_name}")
    for k, v in display.items():
        print(f"    {k}: {v}")
    try:
        answer = await asyncio.to_thread(input, "    允许执行？[y/N] ")
        return answer.strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ── 公共接口 ─────────────────────────────────────────────────

def _hard_deny_reason(name: str, args: dict) -> str | None:
    """闸门 1：硬拒绝（bash / verify 共用，黑名单见 builtin/_dangerous.py）。始终生效。"""
    if name in _SHELL_TOOLS:
        reason = find_dangerous(_shell_cmd(args))
        if reason is not None:
            return f"硬拒绝: {reason}"
    return None


async def check_permission(
    name: str,
    args: dict,
    ask_fn: Callable[[str, dict, str], Awaitable[bool]] | None = None,
) -> str | None:
    """检查工具调用权限。返回 None 表示放行；返回字符串表示拒绝原因。

    闸门1（硬拒绝）始终生效，含 ask_fn=None 的路径。
    闸门2/3 仅当名称匹配规则时触发；ask_fn=None 时跳过用户审批直接放行。
    """
    reason = _hard_deny_reason(name, args)
    if reason is not None:
        return reason

    # 闸门 2 + 3：规则 → 用户审批
    for tool_names, check_fn, reason in _RULES:
        if name not in tool_names:
            continue
        try:
            triggered = check_fn(args)
        except Exception:
            triggered = False
        if not triggered:
            continue
        # ask_fn=None → 没有审批入口，跳过闸门 3 直接放行（不算阻断）
        if ask_fn is None:
            break
        if not await ask_fn(name, args, reason):
            return "用户拒绝执行"
        break

    return None
