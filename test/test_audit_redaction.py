"""脱敏 + 工具审计单元测试。"""

import json
import sqlite3

import pytest

from kittymind.tools.redaction import redact, redact_args_for_audit
from kittymind.tools.audit import ToolAuditLog
from kittymind.tools.executor import ToolExecutor
from kittymind.tools.base import BaseTool, ToolResult


# ── 脱敏 ─────────────────────────────────────────────────────────────

def test_redact_openai_key():
    assert "[REDACTED]" in redact("key = sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert "sk-abcd" not in redact("sk-abcdefghijklmnopqrstuvwxyz0123456789")

def test_redact_aws_key():
    assert redact("AKIAIOSFODNN7EXAMPLE").count("[REDACTED]") == 1

def test_redact_github_token():
    out = redact("token: ghp_1234567890abcdefghijABCDEFGHIJ0000")
    assert "ghp_1234" not in out

def test_redact_bearer():
    out = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
    assert "Bearer [REDACTED]" in out

def test_redact_kv_password():
    out = redact("password=hunter2secret")
    assert out == "password=[REDACTED]"

def test_redact_kv_colon_and_case():
    out = redact("API_KEY: mySecretValue123")
    assert "[REDACTED]" in out and "mySecretValue" not in out

def test_redact_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert redact(pem) == "[REDACTED PRIVATE KEY]"

def test_redact_leaves_normal_text():
    text = "这是普通输出，包含 file.py 和 exit code 0，还有 http://example.com"
    assert redact(text) == text

def test_redact_short_value_not_masked():
    """保守：短值（<4 字符）不脱敏，避免误伤。"""
    assert redact("pwd=ab") == "pwd=ab"

def test_redact_non_string_passthrough():
    assert redact(None) is None
    assert redact(123) == 123

def test_redact_args_for_audit_truncates(monkeypatch):
    from kittymind.config import cfg
    monkeypatch.setattr(cfg, "TOOL_AUDIT_ARGS_MAX_CHARS", 20)
    out = redact_args_for_audit({"command": "x" * 100})
    assert len(out) <= 21  # 20 + 省略号
    assert out.endswith("…")

def test_redact_args_masks_secret():
    out = redact_args_for_audit({"command": "export TOKEN=supersecretvalue123"})
    assert "supersecretvalue" not in out


# ── ToolAuditLog（SQLite + JSONL 双写） ──────────────────────────────

def test_audit_writes_both_stores(tmp_path):
    db = tmp_path / "audit.db"
    jsonl = tmp_path / "audit.jsonl"
    log = ToolAuditLog(db, jsonl)
    log.record(
        session_id="s1", tool="bash", args='{"command":"echo hi"}',
        decision="allow", reason="", failed=False, duration_ms=12,
    )

    # SQLite
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT tool, decision, failed, duration_ms FROM tool_audit").fetchall()
    assert rows == [("bash", "allow", 0, 12)]

    # JSONL
    lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "bash" and rec["decision"] == "allow" and rec["session_id"] == "s1"
    assert "ts" in rec

def test_audit_multiple_records(tmp_path):
    log = ToolAuditLog(tmp_path / "a.db", tmp_path / "a.jsonl")
    for i in range(3):
        log.record(session_id=None, tool="ls", args="{}", decision="allow",
                   reason="", failed=False, duration_ms=i)
    conn = sqlite3.connect(str(tmp_path / "a.db"))
    assert conn.execute("SELECT COUNT(*) FROM tool_audit").fetchone()[0] == 3


# ── ToolExecutor 集成（脱敏 + 审计） ────────────────────────────────

class _FakeTool(BaseTool):
    name: str = "fake"
    description: str = "test"

    def __init__(self, result="ok"):
        super().__init__()
        self._result = result

    def execute(self, parameters):
        return ToolResult(True, self._result)

    def run(self, args: dict) -> ToolResult:
        return ToolResult(True, self._result)


class _FakeRegistry:
    def __init__(self, tool):
        self._tool = tool
    def get(self, name):
        return self._tool


class _FakeAudit:
    """捕获 record 调用的假审计。"""
    def __init__(self):
        self.records = []
    def record(self, **kw):
        self.records.append(kw)


def _call(name="fake", args=None):
    return {"id": "c1", "function": {"name": name, "arguments": json.dumps(args or {})}}


def test_executor_redacts_tool_output():
    """工具输出里的密钥进入上下文前被脱敏。"""
    tool = _FakeTool("your key is sk-abcdefghijklmnopqrstuvwxyz0123456789 done")
    executor = ToolExecutor(_FakeRegistry(tool))
    result = executor.execute(_call())
    assert "sk-abcdefghij" not in result["content"]
    assert "[REDACTED]" in result["content"]


def test_executor_audit_records_allow():
    tool = _FakeTool("ok")
    audit = _FakeAudit()
    executor = ToolExecutor(_FakeRegistry(tool), audit=audit)
    executor.begin_turn("sess-1")
    executor.execute(_call(args={"command": "echo hi"}))
    assert len(audit.records) == 1
    rec = audit.records[0]
    assert rec["tool"] == "fake"
    assert rec["decision"] == "allow"
    assert rec["session_id"] == "sess-1"
    assert rec["duration_ms"] >= 0


def test_executor_audit_records_denied():
    """权限拒绝也被审计记录为 denied。"""
    tool = _FakeTool("ok")
    audit = _FakeAudit()
    # 用真实 registry 无所谓——bash 硬拒绝在权限层，不到执行
    executor = ToolExecutor(_FakeRegistry(tool), audit=audit)
    call = {"id": "c1", "function": {"name": "bash", "arguments": json.dumps({"command": "rm -rf /"})}}
    result = executor.execute(call)
    assert "Permission denied" in result["content"]
    assert audit.records[-1]["decision"] == "denied"


def test_executor_audit_args_redacted():
    """审计记录的参数被脱敏。"""
    tool = _FakeTool("ok")
    audit = _FakeAudit()
    executor = ToolExecutor(_FakeRegistry(tool), audit=audit)
    executor.execute(_call(args={"command": "curl -H 'token=supersecretvalue999'"}))
    assert "supersecretvalue" not in audit.records[0]["args"]
