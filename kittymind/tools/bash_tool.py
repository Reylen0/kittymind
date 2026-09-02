import locale
import re
import subprocess
import sys
from typing import Optional

from pydantic import BaseModel, Field

from baseagent.tools.base import BaseTool

_TIMEOUT = 30
_MAX_OUTPUT = 50_000

if sys.platform == "win32":
    import ctypes
    _SYS_ENCODING = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
else:
    _SYS_ENCODING = locale.getpreferredencoding(False) or "utf-8"

_DANGEROUS_PATTERNS = [
    r"rm\s+-[^\s]*r",
    r"rm\s+/",
    r":\(\)\{",
    r"mkfs",
    r"dd\s+",
    r">\s*/dev/sd",
    r"chmod\s+-R\s+777",
    r"sudo\s+rm",
    r"shutdown",
    r"reboot",
    r"format\s+[a-zA-Z]:",
    r"del\s+/[sqf]",
    r"rd\s+/s",
]

_DANGEROUS_RE = re.compile("|".join(_DANGEROUS_PATTERNS), re.IGNORECASE)


class BashToolParam(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    timeout: int = Field(default=_TIMEOUT, description="超时秒数，默认 30")
    run_in_background: bool = Field(default=False, description="设为 true 时在后台线程执行，立刻返回任务 ID")


class BashTool(BaseTool):
    """执行 Shell 命令工具"""

    name: str = "bash"
    description: str = (
        "在本地执行 shell 命令并返回标准输出和标准错误。"
        "耗时命令可设 run_in_background=true 在后台执行，不阻塞 Agent。"
    )
    param_class = BashToolParam

    def execute(self, parameters: BashToolParam) -> str:
        command = parameters.command.strip()
        if _DANGEROUS_RE.search(command):
            return "Error: Dangerous command blocked"

        if sys.platform == "win32":
            shell_args, use_shell = command, True
        else:
            shell_args, use_shell = ["bash", "-c", command], False

        try:
            proc = subprocess.run(
                shell_args, shell=use_shell, capture_output=True,
                text=True, timeout=parameters.timeout,
                encoding=_SYS_ENCODING, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"错误: 命令执行超时 ({parameters.timeout}s)"
        except Exception as e:
            return f"错误: {e}"

        parts = []
        if proc.stdout:
            parts.append(proc.stdout[:_MAX_OUTPUT])
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr[:_MAX_OUTPUT]}")
        if proc.returncode != 0:
            parts.append(f"[退出码: {proc.returncode}]")
        return "\n".join(parts) if parts else "(无输出)"
