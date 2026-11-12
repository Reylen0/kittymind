import asyncio
import contextlib
import os
import signal
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
    # 走 asyncio 子进程而不是 subprocess.run + to_thread：后者的 task.cancel()
    # 只是不再等待线程，线程里真正跑着的 subprocess 杀不掉，会在后台裸跑到自然
    # 结束。原生 await 让取消点落在协程自己身上，能在 CancelledError 里真正
    # 把子进程杀掉。
    is_async = True

    def execute(self, parameters: BashToolParam) -> ToolResult:
        raise RuntimeError("bash 已改为异步工具，只能通过 aexecute() 调用")

    async def aexecute(self, parameters: BashToolParam) -> ToolResult:
        command = parameters.command.strip()
        # 与 permission 闸门 1 共用同一份黑名单（builtin/_dangerous.py）。
        # 这里再查一遍不是冗余：aexecute() 可以被直接调用而绕过 ToolExecutor，
        # 那样闸门 1 根本不会跑。两处同源，因此不存在"两份黑名单漂移"的老问题。
        reason = find_dangerous(command)
        if reason is not None:
            return ToolResult(False, f"Error: 危险命令已阻止 — {reason}")

        # 夹住超时：防止模型传入过大值导致长时间阻塞
        timeout = max(1, min(parameters.timeout, cfg.BASH_MAX_TIMEOUT))

        cwd = bash_cwd.get()

        try:
            if sys.platform == "win32":
                proc = await asyncio.create_subprocess_shell(
                    command, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            else:
                # start_new_session=True：命令进程自成一个新的进程组（pgid=pid），
                # _kill() 据此用 os.killpg 把它和它 fork 出的所有子进程一并杀掉——
                # 不开这个的话，`bash -c "a | b"` 这类管道场景下 kill 只能杀到
                # bash 本身，管道另一端的进程会变成孤儿继续裸跑。
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-c", command, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except Exception as e:
            return ToolResult(False, f"错误: {e}")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill(proc)
            return ToolResult(False, f"错误: 命令执行超时 ({timeout}s)")
        except asyncio.CancelledError:
            # task.cancel()（用户点"停止"）落在这里：子进程必须真被杀掉，否则
            # 它会继续在后台裸跑——这正是本方法要解决的问题，所以 kill 之后
            # 仍需原样重新抛出 CancelledError，取消语义不能被这里吞掉。
            await _kill(proc)
            raise
        except Exception as e:
            await _kill(proc)
            return ToolResult(False, f"错误: {e}")

        # 子进程按字节捕获（不在创建子进程时固定 text/encoding）：是否是
        # UTF-8 输出取决于具体跑的命令，解码策略见 _encoding.decode_subprocess_output。
        stdout = decode_subprocess_output(stdout_b)
        stderr = decode_subprocess_output(stderr_b)

        parts = [f"[工作目录: {cwd}]"]
        if stdout: parts.append(stdout[:cfg.TOOL_MAX_OUTPUT])
        if stderr: parts.append(f"[stderr]\n{stderr[:cfg.TOOL_MAX_OUTPUT]}")
        if proc.returncode != 0: parts.append(f"[退出码: {proc.returncode}]")
        content = "\n".join(parts) if len(parts) > 1 else parts[0] + "\n(无输出)"
        return ToolResult(proc.returncode == 0, content)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其派生的子孙进程，等它们真正退出。

    直接 `proc.kill()` 只杀得到 asyncio 直接认识的那一个进程：
      - Windows 上 shell（cmd.exe）会为真正执行的命令另起一个子进程，
        TerminateProcess 不会连带杀掉它的子进程（Windows 没有 POSIX 那种
        进程组语义），必须用 `taskkill /T` 按整条进程树去杀。
      - POSIX 上创建时已 `start_new_session=True`（pgid=pid），用
        `os.killpg` 把整个进程组一起杀，覆盖 shell 内部再 fork 出的子进程
        （管道/子 shell 等场景）。
    进程已经自然结束时，kill/wait 都会安静地失败，不是错误。
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()
