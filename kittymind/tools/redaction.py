"""敏感信息脱敏（保守版）。

仅对**高置信度**密钥/凭据模式做替换，宁漏勿误——避免把正常内容误当密钥。
用于两处：
  - 工具输出进入 LLM 上下文前（防止把密钥发给模型供应商）
  - 写入审计日志的参数串前（防止密钥落盘）

工具**实际执行**仍用未脱敏的真实参数；脱敏只作用于「对外呈现/落盘」的副本。
"""

from __future__ import annotations

import json
import re

from ..config import cfg

_MASK = "[REDACTED]"

# 高置信度模式（保守：模式够独特才纳入）
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM 私钥块（放最前，避免被其他规则拆碎）
    (re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL),
     "[REDACTED PRIVATE KEY]"),
    # OpenAI / Anthropic 风格：sk-...
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), _MASK),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _MASK),
    # GitHub token：ghp_ / gho_ / ghu_ / ghs_ / ghr_
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), _MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _MASK),
    # Slack token：xoxb- / xoxp- / xoxa- / xoxr- / xoxs-
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), _MASK),
    # Google API key：AIza...
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), _MASK),
    # HTTP Authorization: Bearer <token>
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "Bearer " + _MASK),
    # key=value / key: value，键名明确暗示密钥时脱敏其值（值需 ≥4 字符）
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
        r"|private[_-]?key|client[_-]?secret|auth[_-]?token)"
        r"\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?"),
     r"\1=" + _MASK),
]


def redact(text: str) -> str:
    """对字符串做保守脱敏；非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_args_for_audit(args: dict) -> str:
    """把工具参数序列化为脱敏 + 截断后的字符串，供审计落盘。"""
    try:
        raw = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        raw = str(args)
    redacted = redact(raw)
    limit = cfg.TOOL_AUDIT_ARGS_MAX_CHARS
    if len(redacted) > limit:
        redacted = redacted[:limit] + "…"
    return redacted
