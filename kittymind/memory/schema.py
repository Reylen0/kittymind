"""记忆条目的 frontmatter 模型（v2）—— 字段定义、生成、序列化、解析。

写入一律用 `yaml.safe_dump`，写后立刻做 round-trip 断言
（`safe_load(safe_dump(d)) == d`）：不一致就拒绝写盘，绝不产生一条
"写得进去但读不出来"的记忆（见 `dumps()`）。
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime

import yaml

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

MEMORY_TYPES = {"user", "feedback", "project", "reference"}
MEMORY_STATUSES = {"active", "cooling", "archived", "forgotten"}

# 供 `related`/`source` 等结构化字段使用
_LIST_FIELDS = ("related",)
_DICT_FIELDS = ("source",)
_BOOL_FIELDS = ("pinned", "local_only")
_INT_FIELDS = ("recall_count",)
_FLOAT_FIELDS = ("strength",)
_STR_FIELDS = (
    "id", "name", "type", "scope", "status", "description",
    "created_at", "updated_at", "last_confirmed_at", "last_recalled_at",
    "supersedes", "superseded_by", "merged_into", "content_hash",
)

# 新建条目的默认值。**只增不删**
DEFAULTS: dict = {
    "schema": SCHEMA_VERSION,
    "id": "",
    "name": "",
    "type": "project",
    "scope": "global",
    "status": "active",
    "pinned": False,
    "strength": 1.0,
    "description": "",
    "created_at": "",
    "updated_at": "",
    "last_confirmed_at": "",
    "last_recalled_at": "",
    "recall_count": 0,
    "local_only": False,
    "source": None,
    "related": [],
    "supersedes": None,
    "superseded_by": None,
    "merged_into": None,
    "content_hash": "",
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ID_RE = re.compile(r"^mem-\d{8}-\d{6}-[0-9a-f]{4}$")

MAX_DESCRIPTION_CHARS = 200
DEFAULT_TYPE = "project"


def new_id(now: datetime | None = None) -> str:
    """`mem-<yyyymmdd-HHMMSS>-<rand4>`：时间序可排序、人能粗读、碰撞概率可忽略。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"mem-{stamp}-{secrets.token_hex(2)}"


def id_suffix(memory_id: str) -> str:
    """取 id 末段，用于文件名消歧。"""
    return str(memory_id or "").rsplit("-", 1)[-1][:8] or "0000"


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.match(str(value or "")))


def now_stamp() -> str:
    """人类可读的本地时间戳"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso() -> str:
    """带时区的 ISO8601（`created_at` / `last_confirmed_at` 等新字段用）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(name: str) -> str:
    """名字 → 文件名 slug。**它只是属性，不是身份**（身份是 id）。"""
    slug = str(name or "").lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:64] or "memory"


def normalize_description(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """描述归一为**单行**"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) > limit:
        logger.debug("记忆描述超长（%d 字），截断到 %d 字", len(flat), limit)
        flat = flat[:limit].rstrip()
    return flat


def _as_bool(value) -> bool:
    """字符串 `"false"` 的布尔语义是 False —— 不能直接用 `bool()`。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip().strip("[]")
    return [t.strip().strip("'\"") for t in text.split(",") if t.strip()]


def _as_optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize(meta: dict) -> dict:
    """把任意来源（正则 / YAML / 手工构造）的元数据补齐并归一类型。

    **对旧值只做类型归一，不改语义**：`updated_at` 保持原字符串不重新格式化
    """
    out: dict = dict(DEFAULTS)
    for key, value in (meta or {}).items():
        if key not in DEFAULTS:
            out[key] = value          # 未知字段原样保留（前向兼容）
            continue
        out[key] = value

    for key in _STR_FIELDS:
        if key in ("supersedes", "superseded_by", "merged_into"):
            out[key] = _as_optional_str(out.get(key))
        else:
            value = out.get(key)
            if isinstance(value, datetime):     # safe_load 的类型推断，防一手
                value = value.strftime("%Y-%m-%d %H:%M:%S")
            elif value is None:
                value = ""
            out[key] = str(value).strip() if not isinstance(value, str) else value
    for key in _BOOL_FIELDS:
        out[key] = _as_bool(out.get(key))
    for key in _INT_FIELDS:
        try:
            out[key] = int(out.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    for key in _FLOAT_FIELDS:
        try:
            out[key] = float(out.get(key) or 1.0)
        except (TypeError, ValueError):
            out[key] = 1.0
    for key in _LIST_FIELDS:
        out[key] = _as_list(out.get(key))
    for key in _DICT_FIELDS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {str(k): v for k, v in value.items()}
        else:
            text = str(value or "").strip()
            out[key] = None if text in ("", "None", "null", "{}") else {"raw": text}

    if out["type"] not in MEMORY_TYPES:
        logger.debug("记忆类型非法 %r，按 %s 处理", out["type"], DEFAULT_TYPE)
        out["type"] = DEFAULT_TYPE
    if out["status"] not in MEMORY_STATUSES:
        out["status"] = "active"
    if not out["scope"]:
        out["scope"] = "global"
    out["schema"] = SCHEMA_VERSION
    return out


def _dumpable(meta: dict) -> dict:
    """挑出要写进 frontmatter 的字段并定序（`schema` 恒为第一键，供读取侧分流）。"""
    payload = {"schema": SCHEMA_VERSION}
    for key, default in DEFAULTS.items():
        if key == "schema":
            continue
        payload[key] = (meta or {}).get(key, default)
    return payload


def dumps(meta: dict) -> str:
    """序列化 frontmatter 块（含首尾 `---`）。**round-trip 不通过就抛错。**"""
    payload = _dumpable(meta)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    try:
        back = yaml.safe_load(text)
    except yaml.YAMLError as e:                      # pragma: no cover - 兜底
        raise ValueError(f"frontmatter 无法回读（YAML 解析失败）：{e}") from e
    if not isinstance(back, dict) or _strip_empty(back) != _strip_empty(payload):
        raise ValueError("frontmatter round-trip 不一致，拒绝写入")
    return f"---\n{text}---\n"


def _strip_empty(payload: dict) -> dict:
    """比较时忽略空容器/空串的形态差异（`None` / `""` / `[]` 视为等价）。

    只用于 round-trip 的相等判断，所以两侧都过同一个函数——变换只要是确定性的，
    就不会把"不一致"判成"一致"。
    """
    return {key: (value if value else None) for key, value in payload.items()}


def loads(text: str) -> dict | None:
    """用 YAML 解析 frontmatter 正文字符串；失败返回 None。"""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        logger.debug("frontmatter YAML 解析失败：%s", e)
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
