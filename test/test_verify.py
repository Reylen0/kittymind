"""Phase 13 — 自我验证（verify 工具）单元测试。"""

import socket
import sys
import threading


from kittymind.agent.verify.runner import CommandVerifier, ProbeVerifier
from kittymind.tools.builtin.verify_tool import VerifyTool, VerifyToolParam


# ── CommandVerifier ──────────────────────────────────────────────────

def test_command_verifier_success(tmp_path):
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    result = CommandVerifier(cmd, timeout=10).run(tmp_path)
    assert result.ok
    assert "退出码: 0" in result.evidence


def test_command_verifier_failure(tmp_path):
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    result = CommandVerifier(cmd, timeout=10).run(tmp_path)
    assert not result.ok
    assert "退出码: 1" in result.evidence


def test_command_verifier_timeout(tmp_path):
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    result = CommandVerifier(cmd, timeout=1).run(tmp_path)
    assert not result.ok
    assert "超时" in result.evidence


def test_command_verifier_captures_output(tmp_path):
    cmd = f'"{sys.executable}" -c "print(\'hello-verify\')"'
    result = CommandVerifier(cmd, timeout=10).run(tmp_path)
    assert result.ok
    assert "hello-verify" in result.evidence


# ── ProbeVerifier ─────────────────────────────────────────────────────

def _start_tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_loop():
        try:
            while True:
                conn, _ = srv.accept()
                conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()
    return srv, port


def test_probe_verifier_tcp_success(tmp_path):
    srv, port = _start_tcp_server()
    try:
        result = ProbeVerifier(f"127.0.0.1:{port}", retries=2, interval=0.1).run(tmp_path)
        assert result.ok
        assert "探测成功" in result.evidence
    finally:
        srv.close()


def test_probe_verifier_tcp_failure_closed_port(tmp_path):
    # 找一个当前未监听的端口
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_sock.bind(("127.0.0.1", 0))
    closed_port = probe_sock.getsockname()[1]
    probe_sock.close()

    result = ProbeVerifier(f"127.0.0.1:{closed_port}", retries=1, interval=0.1).run(tmp_path)
    assert not result.ok
    assert "探测失败" in result.evidence


def test_probe_verifier_invalid_target(tmp_path):
    result = ProbeVerifier("not-a-valid-target", retries=1, interval=0.1).run(tmp_path)
    assert not result.ok


# ── VerifyTool ────────────────────────────────────────────────────────

def test_verify_tool_command_dispatch():
    tool = VerifyTool()
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    result = tool.execute(VerifyToolParam(type="command", command=cmd))
    assert result.ok


def test_verify_tool_command_missing_command():
    tool = VerifyTool()
    result = tool.execute(VerifyToolParam(type="command", command=""))
    assert not result.ok
    assert "command" in result.content


def test_verify_tool_probe_missing_target():
    tool = VerifyTool()
    result = tool.execute(VerifyToolParam(type="probe", target=""))
    assert not result.ok
    assert "target" in result.content


def test_verify_tool_unknown_type():
    tool = VerifyTool()
    result = tool.execute(VerifyToolParam(type="bogus"))
    assert not result.ok
    assert "不支持的验证类型" in result.content


def test_verify_tool_timeout_clamped(monkeypatch):
    captured = {}
    from kittymind.tools.builtin import verify_tool as vt_module

    class _CaptureVerifier:
        def __init__(self, command, timeout, max_output):
            captured["timeout"] = timeout
        def run(self, workdir):
            from kittymind.agent.verify.runner import VerifyResult
            return VerifyResult(True, "ok")

    monkeypatch.setattr(vt_module, "CommandVerifier", _CaptureVerifier)
    tool = VerifyTool()
    tool.execute(VerifyToolParam(type="command", command="echo hi", timeout=99999))
    assert captured["timeout"] == vt_module.cfg.VERIFY_MAX_TIMEOUT
