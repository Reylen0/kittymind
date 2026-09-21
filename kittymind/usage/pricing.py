"""用量成本估算：内置价目表 + settings.json 覆盖。

价格单位统一为「每百万 token 的美元价」（USD / 1M tokens）。估算值是给用户
一个量级参考，不是账单；价目表查不到对应模型时返回 None（不编造）。

覆盖优先级：settings.json 的 `USAGE_PRICING`（dict: model_id -> {input, output}）
> 内置默认价目表。匹配规则：先精确匹配 model_id，再按前缀（如 "claude-"）回退。
"""

from __future__ import annotations

from dataclasses import dataclass

# 内置默认价（USD / 1M tokens）。数值为公开牌价的近似，仅供量级参考。
_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Claude 系列（Anthropic 公开牌价近似）
    "claude-sonnet-4":   {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4":    {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00},
    "claude-opus-4":     {"input": 15.00, "output": 75.00},
    "claude-3":          {"input": 3.00, "output": 15.00},
    # DeepSeek 系列
    "deepseek-chat":     {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    # Qwen 系列
    "qwen":              {"input": 0.30, "output": 0.80},
    # 本地 / Ollama：成本记 0（可被 settings.json 覆盖）
    "ollama":            {"input": 0.0, "output": 0.0},
    "gpt-5.2":           {"input": 0.18, "output": 1.5},
    "gpt-5.4":           {"input": 0.63, "output": 3.78}
}

# 前缀匹配的兜底顺序：越具体越靠前
_PREFIX_ORDER = (
    "claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4",
    "claude-haiku-4", "claude-opus-4", "claude-3",
    "deepseek-reasoner", "deepseek-chat", "qwen", "ollama",
)


@dataclass(frozen=True)
class Cost:
    """一次用量对应的估算成本。"""

    input_usd: float
    output_usd: float

    @property
    def total_usd(self) -> float:
        return self.input_usd + self.output_usd


def _overrides() -> dict[str, dict[str, float]]:
    """从 settings.json 读 USAGE_PRICING 覆盖（结构非法时静默降级为空）。"""
    from ..config import cfg

    raw = getattr(cfg, "USAGE_PRICING", None)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            entry = {
                "input": float(v.get("input", 0.0)),
                "output": float(v.get("output", 0.0)),
            }
        except (TypeError, ValueError):
            continue
        result[str(k)] = entry
    return result


def _lookup(model_id: str) -> dict[str, float] | None:
    """精确 + 前缀匹配，settings 覆盖优先。"""
    over = _overrides()
    if model_id in over:
        return over[model_id]
    if model_id in _DEFAULT_PRICING:
        return _DEFAULT_PRICING[model_id]
    for prefix in _PREFIX_ORDER:
        if model_id.startswith(prefix):
            return _DEFAULT_PRICING.get(prefix)
    return None


def estimate_cost(prompt_tokens: int, completion_tokens: int, model_id: str) -> Cost | None:
    """按 token 数估算成本；查不到价目表返回 None。"""
    price = _lookup(model_id)
    if price is None:
        return None
    input_usd = prompt_tokens / 1_000_000 * price["input"]
    output_usd = completion_tokens / 1_000_000 * price["output"]
    return Cost(input_usd=input_usd, output_usd=output_usd)


def cost_from_by_model(by_model: dict[str, dict]) -> float | None:
    """对一个聚合组的「按模型 token 明细」整体计价（day/session 分组用）。

    每个模型分别按自己的价目算再求和——day/session 组不是单一模型，直接拿
    组总 token 配任何一个价目都是错的。全部模型都查不到价目时返回 None
    （不编造）；部分查得到时按已知部分求和（未知模型贡献 0，不把整组标成
    未知，否则混入一个冷门模型就整组没金额，反而更看不懂）。
    """
    total = 0.0
    known = False
    for model_id, tok in by_model.items():
        c = estimate_cost(
            int(tok.get("prompt_tokens", 0)), int(tok.get("completion_tokens", 0)), model_id
        )
        if c is not None:
            total += c.total_usd
            known = True
    return total if known else None
