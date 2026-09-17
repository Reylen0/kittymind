"""自我验证：修改类操作后，Agent 可主动调用验证器确认结果是否正确。

两类验证器：
  - CommandVerifier：跑一条命令（测试/lint/build），退出码 0 视为通过。
  - ProbeVerifier：探测服务是否就绪（HTTP URL 或 host:port），带重试。

设计取舍：只负责"跑验证 + 产出证据"，不做自动重试——是否重试、怎么修，
交给 LLM 看着 VerifyResult.evidence 自己决定，避免引入新的自动化状态机。
"""

from __future__ import annotations

import locale
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    _SYS_ENCODING = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
else:
    _SYS_ENCODING = locale.getpreferredencoding(False) or "utf-8"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    evidence: str


class Verifier(ABC):
    @abstractmethod
    def run(self, workdir: Path) -> VerifyResult:
        ...


class CommandVerifier(Verifier):
    """跑一条验证命令（如 `pytest -q`），退出码 0 = 通过。"""

    def __init__(self, command: str, timeout: int, max_output: int = 20_000):
        self._command = command
        self._timeout = timeout
        self._max_output = max_output

    def run(self, workdir: Path) -> VerifyResult:
        if sys.platform == "win32":
            shell_args, use_shell = self._command, True
        else:
            shell_args, use_shell = ["bash", "-c", self._command], False

        try:
            proc = subprocess.run(
                shell_args, shell=use_shell, capture_output=True,
                text=True, timeout=self._timeout,
                encoding=_SYS_ENCODING, errors="replace",
                cwd=str(workdir),
                check=False,  # 非零退出码是验证结果本身，由 VerifyResult 表达
            )
        except subprocess.TimeoutExpired:
            return VerifyResult(False, f"验证超时（{self._timeout}s）：{self._command}")
        except Exception as e:
            return VerifyResult(False, f"验证命令执行异常：{e}")

        parts = [f"[命令: {self._command}]", f"[工作目录: {workdir}]"]
        if proc.stdout:
            parts.append(proc.stdout[:self._max_output])
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr[:self._max_output]}")
        parts.append(f"[退出码: {proc.returncode}]")
        return VerifyResult(proc.returncode == 0, "\n".join(parts))


def _probe_once(target: str, timeout: float) -> tuple[bool, str]:
    if target.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(target, timeout=timeout) as resp:
                return resp.status < 500, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return e.code < 500, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)
    else:
        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            return False, f"无效的探测目标: {target}（应为 URL 或 host:port）"
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, f"TCP 连接成功: {host}:{port}"
        except Exception as e:
            return False, str(e)


class ProbeVerifier(Verifier):
    """探测服务是否就绪：HTTP URL 判状态码 <500，host:port 判 TCP 连接是否成功。"""

    def __init__(self, target: str, retries: int, interval: float):
        self._target = target
        self._retries = max(1, retries)
        self._interval = interval

    def run(self, workdir: Path) -> VerifyResult:
        last_msg = ""
        for i in range(self._retries):
            ok, msg = _probe_once(self._target, timeout=self._interval)
            if ok:
                return VerifyResult(True, f"探测成功（第 {i + 1} 次）：{msg}")
            last_msg = msg
            if i < self._retries - 1:
                time.sleep(self._interval)
        return VerifyResult(False, f"探测失败（已重试 {self._retries} 次）：{last_msg}")
