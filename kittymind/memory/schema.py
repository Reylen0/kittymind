"""记忆条目的 frontmatter 模型（v2）—— 字段定义、生成、序列化、解析。

## 为什么写入用 YAML、读取却以正则为主

直觉是"换成标准 YAML 就安全了"，但实测发现**读写两侧风险完全不对称**：

| 场景 | 实测结果 |
|---|---|
| `safe_dump` 写时间串 / 含 `#` `[` 的文本 | ✅ 自动加引号，安全 |
| `safe_load` 读旧文件 `updated_at: 2026-09-21 17:19:49` | ❌ 变成 `datetime`（**类型变了**） |
| `safe_load` 读 `description: 见 https://a.com/x # 备注` | ❌ `# 备注` **被静默吃掉** |
| `safe_load` 读 `description: [草稿] 偏好简短` | ❌ `ParserError`，**整条读不出来** |

所以 `safe_load` 直接读旧文件比原来的正则还糟。定下的规矩是：

  - **写入侧**：一律 `yaml.safe_dump`，写后立刻 round-trip 断言
    （`safe_load(safe_dump(d)) == d`）。不通过就抛错、拒绝写盘——
    **绝不产生一条"能写进去但读不出来"的记忆**。
  - **读取侧**：按 `schema` 标记分流——v2 文件走 YAML，v1（旧格式）走正则。
    正则对旧格式天然安全，且不会做 YAML 类型推断。

## 为什么用 `schema: 2` 做分流标记而不是"猜"

旧文件经正则解析后**字段也是齐的**（每个键都在一行上），靠"字段缺不缺"判断不出
格式。而 v2 的 `source: {via: extract, ...}`、`related: [a, b]` 用正则会读成
**字符串**（类型错），带引号的值还会把引号一起吃进去。所以必须有一个显式标记：
v2 写入时把 `schema` 作为第一个键写出，读取时先看它。
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

# 新建条目的默认值。**只增不删**：v1 的 name/type/description/updated_at
# 原样保留，读出来仍是同样的字符串，现有调用点零改动。
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
_KV_RE = re.compile(r"^(\w[\w-]*)\s*:\s*(.+)$", re.MULTILINE)
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
    """人类可读的本地时间戳（与 v1 的 `updated_at` 完全同格式，保证逐字节兼容）。

    刻意不用 ISO8601：旧文件的 `updated_at` 是 `2026-09-21 17:19:49`，
    换格式会让"迁移前后原字段逐字节一致"这条验收标准失效。
    """
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
    """描述归一为**单行**。

    旧实现把 description 直接拼进 frontmatter，且 `_KV_RE` 只认单行——含换行的
    描述第二行会被静默丢弃（实测写回只剩第一行）。这里换成空格：内容一个字不少，
    只是不再跨行。超长则截断（frontmatter 不是正文该待的地方）。
    """
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

    **对旧值只做类型归一，不改语义**：`updated_at` 保持原字符串不重新格式化，
    因为"迁移前后原字段逐字节一致"是验收标准。
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


def _loads_regex(text: str) -> dict:
    """旧格式（v1）解析：只认单行 `key: value`，不做任何类型推断。"""
    return normalize({k.strip(): v.strip() for k, v in _KV_RE.findall(text)})


def parse(content: str) -> tuple[dict | None, str]:
    """把整份文件拆成 (元数据, 正文)。元数据解析不出来时返回 (None, 剩余文本)。

    分流规则见模块 docstring：带 `schema: 2` 走 YAML，否则走旧正则。
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None, content
    block = m.group(1)
    body = content[m.end():].strip()

    if _declares_schema2(block):
        meta = loads(block)
        if meta is not None:
            return meta, body
        logger.warning("v2 frontmatter 用 YAML 解析失败，退回旧正则")
    return _loads_regex(block), body


def _declares_schema2(block: str) -> bool:
    for key, value in _KV_RE.findall(block):
        if key.strip() == "schema":
            return str(value).strip().strip("'\"") == str(SCHEMA_VERSION)
    return False


def document(meta: dict, body: str) -> str:
    """拼出完整文件内容：frontmatter + 空行 + 正文。"""
    return f"{dumps(meta)}\n{str(body or '').strip()}\n"
