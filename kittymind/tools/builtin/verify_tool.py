from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ...config import cfg
from ...agent.verify.runner import CommandVerifier, ProbeVerifier
from .bash_tool import bash_cwd

_ALLOWED_TYPES = {"command", "probe"}


class VerifyToolParam(BaseModel):
    type: str = Field(
        default="command",
        description="验证方式: command（跑验证命令，如测试/lint/build）或 probe（探测服务是否就绪）",
    )
    command: str = Field(
        default="", description="type=command 时要执行的验证命令，如 'pytest -q'",
    )
    target: str = Field(
        default="", description="type=probe 时的探测目标：HTTP URL（http://...）或 host:port",
    )
    timeout: int = Field(
        default=cfg.VERIFY_TIMEOUT,
        description=f"command 超时秒数，默认 {cfg.VERIFY_TIMEOUT}，上限 {cfg.VERIFY_MAX_TIMEOUT}",
    )
    retries: int = Field(
        default=cfg.VERIFY_PROBE_RETRIES, description="probe 重试次数",
    )
    interval: float = Field(
        default=cfg.VERIFY_PROBE_INTERVAL, description="probe 重试间隔秒数",
    )


class VerifyTool(BaseTool):
    """自我验证工具：修改代码/配置或启动服务后，主动确认是否真的达到预期。"""

    name: str = "verify"
    description: str = (
        "修改代码/文件或启动服务后主动验证是否达到预期效果，而不是假设操作成功。"
        "type=command 跑一条验证命令（如 pytest/eslint/npm run build），退出码 0 视为通过；"
        "type=probe 探测服务是否就绪（HTTP URL 或 host:port）。"
        "验证失败时请仔细核对返回的证据，尝试修复后再重新验证，而不是忽略。"
    )
    param_class = VerifyToolParam

    def execute(self, parameters: VerifyToolParam) -> ToolResult:
        kind = parameters.type.strip().lower()
        if kind not in _ALLOWED_TYPES:
            return ToolResult(False, f"错误: 不支持的验证类型 '{kind}'，可用: {', '.join(sorted(_ALLOWED_TYPES))}")

        if kind == "command":
            if not parameters.command.strip():
                return ToolResult(False, "错误: type=command 时必须提供 command")
            timeout = max(1, min(parameters.timeout, cfg.VERIFY_MAX_TIMEOUT))
            verifier = CommandVerifier(parameters.command, timeout, cfg.VERIFY_MAX_OUTPUT)
        else:
            if not parameters.target.strip():
                return ToolResult(False, "错误: type=probe 时必须提供 target")
            verifier = ProbeVerifier(parameters.target, parameters.retries, parameters.interval)

        result = verifier.run(Path(bash_cwd.get()))
        return ToolResult(result.ok, result.evidence)
