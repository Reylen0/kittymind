"""技能文档的 frontmatter 模型 —— 字段定义、生成、序列化、解析。

与 `memory/schema.py` 同一套 round-trip 校验手法（`yaml.safe_dump` 后立刻
`safe_load` 比对，不一致就拒绝写盘），但字段集合压到最少：只有 `name` +
`description`，没有 `id`/`scope`/`status`/`created_at`/`source` 等——技能
就是一份整篇替换的 Markdown 文档，身份是 `name` 本身，不需要其它元信息。
"""

from __future__ import annotations

import logging
import re

import yaml

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_STR_FIELDS = ("name", "description")

# 新建条目的默认值。**只增不删**（与 memory/schema.py 同一约定）
DEFAULTS: dict = {
    "schema": SCHEMA_VERSION,
    "name": "",
    "description": "",
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

MAX_DESCRIPTION_CHARS = 200


def slugify(name: str) -> str:
    """名字 → 目录名 slug（与 memory/schema.py::slugify 同一逻辑）。"""
    slug = str(name or "").lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:64] or "skill"


def normalize_description(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) > limit:
        flat = flat[:limit].rstrip()
    return flat


def normalize(meta: dict) -> dict:
    """把任意来源（正则 / YAML / 手工构造）的元数据补齐并归一类型。"""
    out: dict = dict(DEFAULTS)
    for key, value in (meta or {}).items():
        out[key] = value  # 未知字段原样保留（前向兼容）

    for key in _STR_FIELDS:
        value = out.get(key)
        out[key] = "" if value is None else str(value).strip() if not isinstance(value, str) else value

    out["schema"] = SCHEMA_VERSION
    return out


def _dumpable(meta: dict) -> dict:
    payload = {"schema": SCHEMA_VERSION}
    for key, default in DEFAULTS.items():
        if key == "schema":
            continue
        payload[key] = (meta or {}).get(key, default)
    return payload


def dumps(meta: dict) -> str:
    """序列化 frontmatter 块（含首尾 `---`）。round-trip 不通过就抛错。"""
    payload = _dumpable(meta)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    try:
        back = yaml.safe_load(text)
    except yaml.YAMLError as e:  # pragma: no cover - 兜底
        raise ValueError(f"frontmatter 无法回读（YAML 解析失败）：{e}") from e
    if not isinstance(back, dict) or _strip_empty(back) != _strip_empty(payload):
        raise ValueError("frontmatter round-trip 不一致，拒绝写入")
    return f"---\n{text}---\n"


def _strip_empty(payload: dict) -> dict:
    return {key: (value if value else None) for key, value in payload.items()}


def loads(text: str) -> dict | None:
    """用 YAML 解析 frontmatter 正文字符串；失败返回 None。"""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        logger.debug("技能 frontmatter YAML 解析失败：%s", e)
        return None
    if not isinstance(parsed, dict):
        return None
    return normalize(parsed)


def parse(content: str) -> tuple[dict | None, str]:
    """把整份文件拆成 (元数据, 正文)。元数据解析不出来时返回 (None, 剩余文本)。"""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None, content
    block = m.group(1)
    body = content[m.end():].strip()
    return loads(block), body


def document(meta: dict, body: str) -> str:
    """拼出完整文件内容：frontmatter + 空行 + 正文。"""
    return f"{dumps(meta)}\n{str(body or '').strip()}\n"
