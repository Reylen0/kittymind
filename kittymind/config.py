"""
KittyMind 统一配置模块。

使用方式：
    from kittymind.config import cfg
    print(cfg.BASH_TIMEOUT)

默认值在本文件中定义。用户可在 ~/.kittymind/settings.json 中覆盖任意配置项。

settings.json 示例：
{
  "BASH_TIMEOUT": 60,
  "AGENT_MAX_ITERATIONS": 50,
  "WS_PORT": 8766
}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# ── 数据目录 ─────────────────────────────────────────────────────
KITTYMIND_DIR   = Path.home() / ".kittymind"
SESSIONS_DB     = KITTYMIND_DIR / "sessions.db"
MEMORY_DIR      = KITTYMIND_DIR / "memory"
WORKSPACES_FILE  = KITTYMIND_DIR / "workspaces.json"
SCREENSHOTS_DIR  = KITTYMIND_DIR / "screenshots"
TOOL_AUDIT_DB    = KITTYMIND_DIR / "tool_audit.db"      # 工具审计 SQLite
TOOL_AUDIT_JSONL = KITTYMIND_DIR / "tool_audit.jsonl"   # 工具审计 JSONL（同内容双写）
LLM_TRACE_JSONL  = KITTYMIND_DIR / "llm_trace.jsonl"    # LLM 调用留档 JSONL（排查用，默认关闭）
DEFAULT_WORKSPACE_DIR = Path.home()   # 未选工作区时的默认 cwd

# ── 超时（秒） ───────────────────────────────────────────────────
BASH_TIMEOUT     = 30
BASH_MAX_TIMEOUT = 300   # bash 单次超时硬上限（模型传入的 timeout 会被夹到此值）
GIT_TIMEOUT      = 30

# ── 文件操作限制 ─────────────────────────────────────────────────
FILE_READ_MAX_LINES  = 2_000
FILE_READ_MAX_BYTES  = 800_000
FILE_EDIT_MAX_SIZE   = 2_000_000
FILE_WRITE_MAX_BYTES = 1_000_000
# 工具输出统一截断上限（executor 全局截断 + bash 流预截断共用）。
TOOL_MAX_OUTPUT = 50_000

# ── 搜索/列表限制 ────────────────────────────────────────────────
GREP_MAX_RESULTS    = 100
GREP_MAX_FILE_SIZE  = 1_000_000
LS_MAX_ENTRIES      = 500

# ── 跨工具共享：跳过目录 & 二进制扩展名 ────────────────────────
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", "dist", "build", ".pytest_cache", ".tox",
})

BINARY_EXTS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".whl", ".egg",
    ".db", ".sqlite", ".lock",
})

# ── 工具默认参数 ─────────────────────────────────────────────────
GREP_CONTEXT_LINES = 2
LS_DEPTH           = 2
GIT_LOG_COUNT      = 10
SCREENSHOT_MONITOR = 1
# 截图目录保留上限（按修改时间留最新 N 张，0 = 不清理）。
SCREENSHOT_KEEP_MAX = 200

# ── 记忆系统 ─────────────────────────────────────────────────────
MEMORY_CONSOLIDATE_THRESHOLD = 50
MEMORY_MAX_HISTORY_CHARS     = 8_000
MEMORY_RECALL_MAX_RELEVANT   = 5
MEMORY_RECALL_MAX_BODY_CHARS = 800

# ── 记忆向量检索 ────────────────────────────────────
# 端点配置。留空 = 关闭向量路，召回退回「LLM 选序号 → 2-gram 关键词。
# 密钥不进本文件，只从环境变量 EMBEDDING_API_KEY 读。
EMBEDDING_MODEL_ID = ""
EMBEDDING_BASE_URL = ""
# 0 = 用服务端默认维度。
# 服务端不支持该参数时会自动降级重试并学习实际维度（见 embeddings/openai_compat.py）。
EMBEDDING_DIM     = 512
EMBEDDING_BATCH   = 16   # 单次请求编码的文本条数
EMBEDDING_TIMEOUT = 30

# 召回模式：
#   auto    —— 配了 embedding 就用「向量+关键词融合」，否则自动退回 LLM 选序号
#   vector  —— 只用向量+关键词融合（不调 LLM），向量不可用时退回关键词
#   llm     —— 强制用 LLM 选序号路径
#   keyword —— 只用 2-gram 关键词（**零外发的隐私模式**：记忆正文不出本机）
MEMORY_RECALL_MODE = "auto"
MEMORY_VECTOR_TOP_K = 12            # 进入融合的向量候选数（最终仍取 MAX_RELEVANT 条）
MEMORY_RECALL_KEYWORD_WEIGHT = 0.3  # 关键词分在融合里的权重
MEMORY_RECALL_TIMEOUT = 3.0         # 召回超时（秒）；超时降级为关键词结果

# 门控（默认全关）。
MEMORY_RECALL_MIN_SCORE  = 0.0   # <=0 关闭绝对阈值
MEMORY_RECALL_MIN_MARGIN = 0.0   # <=0 关闭相对门（top1 - median 低于它视为"无区分"）

# 索引文本取正文前 N 字
MEMORY_INDEX_TEXT_BODY_CHARS = 300

# 提取去重视图
MEMORY_EXTRACT_CATALOG_TOP_N = 8
MEMORY_EXTRACT_CATALOG_TOKENS = 1_500

# 整合预筛：只把「近重复候选对」交给模型判断 keep/merge/drop
MEMORY_CONSOLIDATE_MIN_SIM  = 0.86  # 两两余弦达到此值才视为近重复候选
MEMORY_CONSOLIDATE_FULL_MAX = 40    # 记忆数低于此值时保留全量行为（小库上全量更准）

# 记忆召回日志保留天数
MEMORY_RECALL_LOG_RETENTION_DAYS = 30   # <=0 = 永久保留
# 软删/被合并的条目先落在这里，可 restore；真删只有两个入口（保留期 GC / 用户显式）。
MEMORY_TRASH_RETENTION_DAYS = 90        # <=0 = 永久保留墓碑

# ── 联网搜索 ──────────────────────────────────────────
# 后端选择。留空 = 关闭，web_search 工具返回"未配置"提示而不是报错中断主流程。
# 密钥只从环境变量 WEB_SEARCH_API_KEY 读。
WEB_SEARCH_BACKEND = "tavily"        # "" | "tavily" | "bing" | "searxng"
# SearXNG 必填（指向自建实例）；Tavily/Bing 留空用各自官方默认端点。
WEB_SEARCH_BASE_URL = ""
WEB_SEARCH_MAX_RESULTS = 5     # 硬上限：防止模型传一个离谱的 top_k 把结果塞爆上下文
WEB_SEARCH_TIMEOUT = 10

# ── Agent ────────────────────────────────────────────────────────
AGENT_MAX_ITERATIONS = 50

# ── 子 Agent 委派 ─────────────────────────────────────────────────
SUBAGENT_MAX_DEPTH      = 3   # 子 Agent 嵌套层数（不含 root）
SUBAGENT_MAX_TOTAL      = 8   # 单个 root turn 全树委派总数上限
SUBAGENT_MAX_ITERATIONS = 50  # 子 Agent ReAct 循环上限

# ── WebSocket 服务器 ─────────────────────────────────────────────
WS_HOST          = "127.0.0.1"
WS_PORT          = 8765
PORT_RETRY_COUNT = 20
# 服务器无鉴权、无连接数上限——绑定环回地址是唯一防线。WS_HOST 可被环境变量 /
# settings.json 改成 0.0.0.0，那样同网段任何进程都能远程驱动 bash 工具。
# 因此启动时默认拒绝非环回绑定；确有局域网使用需求（自担风险）才显式打开此项。
WS_ALLOW_NON_LOOPBACK = False

# ── 日志 ─────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"

# ── 工具权限审批 ─────────────────────────────────────────────────
# 用户点击允许/拒绝的等待上限；超时按「拒绝」处理并向前端推 permission_expired 撤回弹窗。
PERMISSION_ASK_TIMEOUT = 300

# ── LLM ─────────────────────────────────────────────────────────
LLM_TEMPERATURE  = 0.7
LLM_MAX_TOKENS   = 4096   # 单次响应最大 token 数（Anthropic 必填，OpenAI 可选）

# LLM 调用留档
LLM_TRACE_ENABLED = False

# ── 辅助小模型（压缩摘要 / 记忆提取 / 记忆召回筛选）──────────────
# 留空 = 不启用，上述辅助任务回退主模型（与旧行为一致）。
# 环境变量优先级高于 settings.json：LLM_AUX_MODEL_ID / LLM_AUX_API_KEY / LLM_AUX_BASE_URL
LLM_AUX_MODEL_ID    = ""
LLM_AUX_TEMPERATURE = 0.3    # 摘要/抽取类任务需要稳定输出，温度低于主模型
LLM_AUX_MAX_TOKENS  = 2048   # 摘要长度上限，防止小模型跑飞

# ── 上下文压缩 ────────────────────────────────────────
LLM_CONTEXT_WINDOW         = 1024_000  # 模型上下文窗口 token 数，settings.json 可覆盖
LLM_RESERVED_OUTPUT_TOKENS = 2048    # 预留给输出的 token，effective = window - reserved
COMPRESS_THRESHOLD_RATIO   = 0.70     # 占用比超过此值则触发压缩
COMPRESS_TARGET_RATIO      = 0.20     # 压缩后目标占用比（留足腾挪空间防抖）
COMPRESS_PROTECT_FIRST_N   = 2        # 头部历史保护条数（首次压缩后衰减为 0）
COMPRESS_TAIL_MIN_MSGS     = 6        # 尾部最少保留消息条数
COMPRESS_MICRO_TOOL_CHARS  = 5_000    # 单条 tool 结果超此字符数则微压缩修剪
COMPRESS_COOLDOWN_SECONDS  = 300      # 反抖动冷却秒数（内存态 monotonic）

# ── 工具守护栏 ────────────────────────────────────────
TOOL_GUARDRAIL_ENABLED       = True   # 是否启用守护栏（False 则完全跳过）
TOOL_GUARDRAIL_WARN_ENABLED  = True   # 是否输出 warn 提示（False 则仅 block 不提示）
TOOL_GUARDRAIL_HARD_STOP     = False  # 交互态默认不硬停；非交互态由 interactive=False 强制开
GUARD_EXACT_FAIL_WARN        = 2      # 同工具同参失败：warn 阈值
GUARD_EXACT_FAIL_BLOCK       = 5      # 同工具同参失败：block 阈值（hard_stop 态）
GUARD_SAME_TOOL_FAIL_WARN    = 3      # 同工具不同参失败：warn 阈值
GUARD_SAME_TOOL_FAIL_BLOCK   = 8      # 同工具不同参失败：block 阈值（hard_stop 态）
GUARD_NO_PROGRESS_WARN       = 2      # 幂等工具无进展：warn 阈值
GUARD_NO_PROGRESS_BLOCK      = 5      # 幂等工具无进展：block 阈值（hard_stop 态）
GUARD_IDENTICAL_STREAK_BLOCK = 3      # 连续完全相同调用：block 阈值（warn/hard_stop 两态均生效）

# ── 工具审计与脱敏 ────────────────────────────────────
TOOL_AUDIT_ENABLED        = True   # 是否记录工具调用审计（SQLite + JSONL 双写）
TOOL_REDACT_ENABLED       = True   # 是否对工具输出/审计参数做敏感信息脱敏
TOOL_AUDIT_ARGS_MAX_CHARS = 500    # 审计中参数串的最大长度（超出截断）

# ── 用量与成本追踪 ───────────────────────
# 价目表覆盖：{ "model_id": {"input": 美元/百万token, "output": 美元/百万token} }
# 空 dict = 只用内置默认价目表。查不到的模型前端显示「无价目」
USAGE_PRICING = {}

# ── 自我验证（verify 工具） ──────────────────────────
VERIFY_TIMEOUT        = 120     # command 验证器默认超时（测试/build 比普通命令更久）
VERIFY_MAX_TIMEOUT    = 600     # command 验证器超时硬上限
VERIFY_PROBE_RETRIES  = 5       # probe 验证器默认重试次数
VERIFY_PROBE_INTERVAL = 1.0     # probe 验证器重试间隔（秒）
VERIFY_MAX_OUTPUT     = 20_000  # command 验证器证据截断长度

# ── 数据维护与健壮性 ─────────────────────
# 总开关：关掉则不做启动自检、自动备份、checkpoint 与空间回收。
DB_MAINTENANCE_ENABLED = True
# 启动自检（PRAGMA quick_check）。关掉可省下大库的启动扫描时间。
DB_SELF_CHECK = True
# 损坏时是否把坏库隔离（改名留档）并重建空库继续启动。
# False = 直接抛错停止启动，交给用户手工处理。
DB_RECOVER_ON_CORRUPT = True

DB_BACKUP_DIRNAME       = "backups"   # 备份目录名（落在库文件同级的这个子目录里）
DB_BACKUP_KEEP          = 5      # 备份保留份数（0 = 不清理，无限累积）
DB_BACKUP_INTERVAL_HOURS = 24    # 两次自动备份的最小间隔（小时）；到期才在启动时备份
DB_BACKUP_MAX_BYTES     = 64 * 1024 * 1024  # 超过此体积不在启动路径同步备份，改由后台做

DB_VACUUM_MIN_FREELIST_RATIO = 0.40        # 空闲页占比达到此值才考虑 VACUUM
DB_VACUUM_MIN_BYTES          = 32 * 1024 * 1024  # 且库至少这么大（小库不值得重写）
# WAL 自动 checkpoint 阈值（页）。SQLite 默认 1000 页（4K 页 ≈ 4 MB），
# 意味着崩溃后最多要重放 4 MB 日志；调小可缩短恢复时间，代价是 checkpoint 更频繁。
DB_WAL_AUTOCHECKPOINT_PAGES  = 512

LLM_TRACE_MAX_BYTES = 20 * 1024 * 1024     # 留档 JSONL 超过此体积轮转（0 = 不轮转）

USAGE_RETENTION_DAYS = 90      # model_usage 明细保留天数
AUDIT_RETENTION_DAYS = 30      # tool_audit 明细保留天数（SQLite + JSONL 同步）

# ── 运行时配置对象（支持 settings.json 覆盖） ──────────────────────

_SETTINGS_FILE = KITTYMIND_DIR / "settings.json"

# 哪些 key 可从 settings.json 覆盖（Path 类型的 key 不允许，避免意外破坏目录结构）
_OVERRIDABLE = {
    "BASH_TIMEOUT", "BASH_MAX_TIMEOUT", "GIT_TIMEOUT",
    "FILE_READ_MAX_LINES", "FILE_READ_MAX_BYTES",
    "FILE_EDIT_MAX_SIZE", "FILE_WRITE_MAX_BYTES", "TOOL_MAX_OUTPUT",
    "GREP_MAX_RESULTS", "GREP_MAX_FILE_SIZE", "LS_MAX_ENTRIES",
    "GREP_CONTEXT_LINES", "LS_DEPTH", "GIT_LOG_COUNT", "SCREENSHOT_MONITOR",
    "SCREENSHOT_KEEP_MAX",
    "MEMORY_CONSOLIDATE_THRESHOLD", "MEMORY_MAX_HISTORY_CHARS",
    "MEMORY_RECALL_MAX_RELEVANT", "MEMORY_RECALL_MAX_BODY_CHARS",
    "EMBEDDING_MODEL_ID", "EMBEDDING_BASE_URL", "EMBEDDING_DIM",
    "EMBEDDING_BATCH", "EMBEDDING_TIMEOUT",
    "WEB_SEARCH_BACKEND", "WEB_SEARCH_BASE_URL", "WEB_SEARCH_MAX_RESULTS", "WEB_SEARCH_TIMEOUT",
    "MEMORY_RECALL_MODE", "MEMORY_VECTOR_TOP_K", "MEMORY_RECALL_KEYWORD_WEIGHT",
    "MEMORY_RECALL_TIMEOUT", "MEMORY_RECALL_MIN_SCORE", "MEMORY_RECALL_MIN_MARGIN",
    "MEMORY_INDEX_TEXT_BODY_CHARS",
    "MEMORY_EXTRACT_CATALOG_TOP_N", "MEMORY_EXTRACT_CATALOG_TOKENS",
    "MEMORY_CONSOLIDATE_MIN_SIM", "MEMORY_CONSOLIDATE_FULL_MAX",
    "MEMORY_RECALL_LOG_RETENTION_DAYS", "MEMORY_TRASH_RETENTION_DAYS",
    "AGENT_MAX_ITERATIONS",
    "SUBAGENT_MAX_DEPTH", "SUBAGENT_MAX_TOTAL", "SUBAGENT_MAX_ITERATIONS",
    "WS_HOST", "WS_PORT", "PORT_RETRY_COUNT", "WS_ALLOW_NON_LOOPBACK",
    "PERMISSION_ASK_TIMEOUT",
    "LOG_LEVEL",
    "LLM_TEMPERATURE", "LLM_MAX_TOKENS",
    "LLM_TRACE_ENABLED",
    "LLM_AUX_MODEL_ID", "LLM_AUX_TEMPERATURE", "LLM_AUX_MAX_TOKENS",
    "LLM_CONTEXT_WINDOW", "LLM_RESERVED_OUTPUT_TOKENS",
    "COMPRESS_THRESHOLD_RATIO", "COMPRESS_TARGET_RATIO",
    "COMPRESS_PROTECT_FIRST_N", "COMPRESS_TAIL_MIN_MSGS",
    "COMPRESS_MICRO_TOOL_CHARS", "COMPRESS_COOLDOWN_SECONDS",
    "TOOL_GUARDRAIL_ENABLED", "TOOL_GUARDRAIL_WARN_ENABLED", "TOOL_GUARDRAIL_HARD_STOP",
    "GUARD_EXACT_FAIL_WARN", "GUARD_EXACT_FAIL_BLOCK",
    "GUARD_SAME_TOOL_FAIL_WARN", "GUARD_SAME_TOOL_FAIL_BLOCK",
    "GUARD_NO_PROGRESS_WARN", "GUARD_NO_PROGRESS_BLOCK",
    "GUARD_IDENTICAL_STREAK_BLOCK",
    "TOOL_AUDIT_ENABLED", "TOOL_REDACT_ENABLED", "TOOL_AUDIT_ARGS_MAX_CHARS",
    "VERIFY_TIMEOUT", "VERIFY_MAX_TIMEOUT", "VERIFY_PROBE_RETRIES",
    "VERIFY_PROBE_INTERVAL", "VERIFY_MAX_OUTPUT",
    "USAGE_PRICING",
    "DB_MAINTENANCE_ENABLED", "DB_SELF_CHECK", "DB_RECOVER_ON_CORRUPT",
    "DB_BACKUP_DIRNAME", "DB_BACKUP_KEEP", "DB_BACKUP_INTERVAL_HOURS", "DB_BACKUP_MAX_BYTES",
    "DB_VACUUM_MIN_FREELIST_RATIO", "DB_VACUUM_MIN_BYTES",
    "DB_WAL_AUTOCHECKPOINT_PAGES",
    "LLM_TRACE_MAX_BYTES",
    "USAGE_RETENTION_DAYS", "AUDIT_RETENTION_DAYS",
}


# settings.json 里布尔值的可接受字面量（用户经常把 false 写成 "false" 这种带引号的串）
_truthy_tokens = frozenset({"true", "1", "yes", "y", "on", "enable", "enabled"})
_falsy_tokens = frozenset({"false", "0", "no", "n", "off", "disable", "disabled"})

# 配置项改名后的旧名映射：旧名仍可写进 settings.json（向后兼容），但会告警提示更新
_DEPRECATED_ALIASES = {

}


def _warn(message: str) -> None:
    """配置层问题一律显式告警（stderr）。

    静默丢弃是配置类 bug 的头号来源：旧实现里键名写错、JSON 语法错误、
    类型转换失败全是无声的「配了但不生效」。这里全部改成可见告警。
    """
    print(f"[warn] config: {message}", file=sys.stderr, flush=True)


def _coerce(default: Any, value: Any) -> tuple[Any, str | None]:
    """按默认值类型转换 settings.json 的值，返回 (值, 错误说明)。

    **布尔必须单独处理**：旧实现是 `type(default)(value)`，默认值为 True 时
    `bool("false")` → True（非空字符串恒为真），于是 settings.json 里写
    "TOOL_GUARDRAIL_ENABLED": "false" 反而把守护栏打开了。这里显式识别
    false/0/no/off 这些字面量，识别不了就告警并沿用默认值。
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)):
            return bool(value), None
        if isinstance(value, str):
            token = value.strip().lower()
            if token in _truthy_tokens:
                return True, None
            if token in _falsy_tokens:
                return False, None
        return default, "期望布尔值（true / false）"

    if isinstance(default, int):
        if isinstance(value, bool):
            return default, "期望整数"
        try:
            return int(value), None
        except (TypeError, ValueError):
            return default, "期望整数"

    if isinstance(default, float):
        if isinstance(value, bool):
            return default, "期望数字"
        try:
            return float(value), None
        except (TypeError, ValueError):
            return default, "期望数字"

    if isinstance(default, str):
        return str(value), None

    return value, None


class _Config:
    """运行时配置对象，初始化时从 settings.json 加载覆盖值。"""

    def __init__(self) -> None:
        # 反射本模块的公开常量。用 globals() 而不是 `import kittymind.config as _mod`：
        # 后者是模块自我导入（ruff PLW0406），且会把 _OVERRIDABLE / _SETTINGS_FILE
        # 这类私有项也一并复制成实例属性（"_X".isupper() 为 True，会被误判为常量）。
        for key, value in list(globals().items()):
            if key.isupper() and not key.startswith("_"):
                setattr(self, key, value)
        # 再用 settings.json 覆盖允许的 key
        overrides, load_error = self._load_settings()
        if load_error:
            _warn(load_error)
        for raw_key, value in overrides.items():
            # 弃用别名：旧名仍生效但必须告警
            key = _DEPRECATED_ALIASES.get(raw_key, raw_key)
            if raw_key in _DEPRECATED_ALIASES:
                _warn(
                    f"settings.json 中的 {raw_key!r} 已改名为 "
                    f"{_DEPRECATED_ALIASES[raw_key]!r}，本次仍生效，请更新配置文件"
                )
            if key not in _OVERRIDABLE:
                _warn(
                    f"settings.json 中的 {key!r} 不是可覆盖配置项，已忽略"
                    f"（可覆盖清单见 config.py:_OVERRIDABLE）"
                )
                continue
            default = getattr(self, key, None)
            if default is None:
                continue
            coerced, why = _coerce(default, value)
            if why is not None:
                _warn(
                    f"settings.json 中 {key} = {value!r} 无法解析（{why}），"
                    f"已沿用默认值 {default!r}"
                )
                continue
            setattr(self, key, coerced)

    @staticmethod
    def _load_settings() -> tuple[dict[str, Any], str | None]:
        """读取 settings.json，返回 (覆盖项, 错误说明)。

        错误一律带回给调用方显式告警，不再静默返回空字典——JSON 语法写错、
        顶层写成数组这类问题必须让用户看见，否则「明明配了却不生效」无从排查。
        """
        if not _SETTINGS_FILE.is_file():
            return {}, None
        try:
            raw = _SETTINGS_FILE.read_text(encoding="utf-8")
        except OSError as e:
            return {}, f"无法读取 {_SETTINGS_FILE}（{e}），全部配置沿用默认值"
        # 简单 JSONC 支持：去掉以 // 开头的行（允许注释掉配置项）
        lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("//")]
        try:
            data = json.loads("\n".join(lines))
        except json.JSONDecodeError as e:
            return {}, (
                f"{_SETTINGS_FILE} 不是合法 JSON"
                f"（第 {e.lineno} 行第 {e.colno} 列：{e.msg}），全部配置沿用默认值"
            )
        if not isinstance(data, dict):
            return {}, f"{_SETTINGS_FILE} 顶层必须是对象，全部配置沿用默认值"
        return data, None

    def reload(self) -> None:
        """重新从 settings.json 加载覆盖值（设置面板保存后调用）。"""
        self.__init__()


# 模块级单例，各模块通过 `from kittymind.config import cfg` 使用
cfg = _Config()
