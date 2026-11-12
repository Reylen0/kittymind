import subprocess
import sys
from contextvars import ContextVar

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ...config import cfg
from ...encoding import decode_subprocess_output
from ._dangerous import find_dangerous

# 由 KittyAgent 在每次 run_conversation 开始前设置
bash_cwd: ContextVar[str] = ContextVar("bash_cwd", default=str(cfg.DEFAULT_WORKSPACE_DIR))


class BashToolParam(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    timeout: int = Field(
        default_factory=lambda: cfg.BASH_TIMEOUT,
        description="超时秒数（默认取 cfg.BASH_TIMEOUT，上限 cfg.BASH_MAX_TIMEOUT）",
    )


class BashTool(BaseTool):
    """执行 Shell 命令工具"""
    name: str = "bash"
    description: str = (
        "在本地执行 shell 命令并返回标准输出和标准错误。"
        "命令同步执行并有超时上限，请勿运行不会自行退出的长驻进程（如开发服务器、watch）。"
    )
    param_class = BashToolParam

    def execute(self, parameters: BashToolParam) -> ToolResult:
        command = parameters.command.strip()
        # 与 permission 闸门 1 共用同一份黑名单（builtin/_dangerous.py）。
        # 这里再查一遍不是冗余：execute() 可以被直接调用而绕过 ToolExecutor，
        # 那样闸门 1 根本不会跑。两处同源，因此不存在"两份黑名单漂移"的老问题。
        reason = find_dangerous(command)
        if reason is not None:
            return ToolResult(False, f"Error: 危险命令已阻止 — {reason}")

        # 夹住超时：防止模型传入过大值导致长时间阻塞
        timeout = max(1, min(parameters.timeout, cfg.BASH_MAX_TIMEOUT))

        cwd = bash_cwd.get()

        if sys.platform == "win32":
            shell_args, use_shell = command, True
        else:
            shell_args, use_shell = ["bash", "-c", command], False

        try:
            proc = subprocess.run(
                shell_args, shell=use_shell, capture_output=True,
                timeout=timeout,
                cwd=cwd,
                check=False,  # 非零退出码是正常结果，由 ToolResult 表达
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"错误: 命令执行超时 ({timeout}s)")
        except Exception as e:
            return ToolResult(False, f"错误: {e}")

        # 子进程按字节捕获（不在 subprocess.run 里固定 text/encoding）：是否是
        # UTF-8 输出取决于具体跑的命令，解码策略见 _encoding.decode_subprocess_output。
        stdout = decode_subprocess_output(proc.stdout)
        stderr = decode_subprocess_output(proc.stderr)

        parts = [f"[工作目录: {cwd}]"]
        if stdout: parts.append(stdout[:cfg.TOOL_MAX_OUTPUT])
        if stderr: parts.append(f"[stderr]\n{stderr[:cfg.TOOL_MAX_OUTPUT]}")
        if proc.returncode != 0: parts.append(f"[退出码: {proc.returncode}]")
        content = "\n".join(parts) if len(parts) > 1 else parts[0] + "\n(无输出)"
        return ToolResult(proc.returncode == 0, content)
