import locale
import os
import subprocess
import sys

from pydantic import BaseModel, Field

from ..base import BaseTool
from ...config import cfg

_TIMEOUT = cfg.GIT_TIMEOUT

if sys.platform == "win32":
    import ctypes
    _SYS_ENCODING = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
else:
    _SYS_ENCODING = locale.getpreferredencoding(False) or "utf-8"

_ALLOWED_ACTIONS = {"status", "diff", "add", "commit", "log", "branch", "show"}


class GitToolParam(BaseModel):
    action: str = Field(description="git 操作: status、diff、add、commit、log、branch、show")
    files: str = Field(default="", description="add 操作的目标文件，空格分隔；留空暂存所有")
    message: str = Field(default="", description="commit 操作的提交信息")
    count: int = Field(default=cfg.GIT_LOG_COUNT, description="log 显示的提交条数")
    extra: str = Field(default="", description="附加给 git 命令的额外参数")
    workdir: str = Field(default=".", description="执行 git 命令的工作目录")


class GitTool(BaseTool):
    name: str = "git"
    category: str = "执行"
    description: str = "执行常用 git 操作：查看状态、查看 diff、暂存文件、提交、查看日志。"
    param_class = GitToolParam

    def execute(self, parameters: GitToolParam) -> str:
        action = parameters.action.lower().strip()
        if action not in _ALLOWED_ACTIONS:
            return f"错误: 不支持的操作 '{action}'，可用: {', '.join(sorted(_ALLOWED_ACTIONS))}"
        workdir = os.path.abspath(parameters.workdir)
        if not os.path.isdir(workdir): return f"错误: 工作目录不存在 — {workdir}"
        cmd = self._build(action, parameters)
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, capture_output=True, text=True,
                timeout=_TIMEOUT, encoding=_SYS_ENCODING, errors="replace",
            )
        except FileNotFoundError:
            return "错误: 未找到 git 命令"
        except subprocess.TimeoutExpired:
            return f"错误: git 命令超时（{_TIMEOUT}s）"
        except Exception as e:
            return f"错误: {e}"
        parts = []
        if proc.stdout.strip(): parts.append(proc.stdout.rstrip())
        if proc.stderr.strip(): parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
        if proc.returncode != 0 and not parts: parts.append(f"[退出码: {proc.returncode}]")
        return "\n".join(parts) if parts else "(无输出)"

    def _build(self, action: str, p: GitToolParam) -> list[str]:
        base = ["git"]
        if action == "status":   cmd = base + ["status", "--short", "--branch"]
        elif action == "diff":   cmd = base + ["diff"] + (["--"] + p.files.split() if p.files else [])
        elif action == "add":    cmd = base + ["add", "--"] + (p.files.split() if p.files.strip() else ["."])
        elif action == "commit": cmd = base + (["commit", "-m", p.message] if p.message.strip() else ["status"])
        elif action == "log":    cmd = base + ["log", f"--max-count={p.count}", "--oneline", "--decorate"]
        elif action == "branch": cmd = base + ["branch", "-vv"]
        elif action == "show":   cmd = base + ["show", "--stat"]
        else:                    cmd = base + [action]
        if p.extra.strip(): cmd += p.extra.split()
        return cmd
