"""辅助小模型（压缩摘要 / 记忆提取 / 记忆召回）注入测试（无需真实 LLM）。

覆盖三层：
  1. get_aux_llm() 的解析与回退（env 优先、凭据缺失返回 None、成功结果缓存、失败不缓存）
  2. KittyAgent 把辅助模型接到 compressor / memory recall / memory extract 三处
  3. 未配置 aux 时行为与旧版一致（全部走主模型）
"""

from unittest.mock import MagicMock

import pytest

from kittymind.config import cfg
from kittymind.core.llm import get_aux_llm, reset_aux_llm
from kittymind.agent.kitty_agent import KittyAgent

_AUX_ENV_KEYS = (
    "LLM_AUX_MODEL_ID", "LLM_AUX_API_KEY", "LLM_AUX_BASE_URL",
    "LLM_MODEL_ID", "LLM_API_KEY", "LLM_BASE_URL",
)


@pytest.fixture(autouse=True)
def clean_aux(monkeypatch):
    """每个用例前清空辅助模型缓存与相关环境变量 / 配置。"""
    reset_aux_llm()
    for key in _AUX_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cfg, "LLM_AUX_MODEL_ID", "", raising=False)
    yield
    reset_aux_llm()


def _set_env(monkeypatch, **kwargs):
    for key, value in kwargs.items():
        monkeypatch.setenv(key, value)


def make_agent(aux=..., **kwargs) -> tuple[KittyAgent, MagicMock]:
    """构造最小可用 KittyAgent；aux=... 表示走默认自动解析。"""
    main = MagicMock()
    main.model = "main-model"
    if aux is ...:
        agent = KittyAgent(name="kitty", llm=main, system_prompt="sp", tools=[], **kwargs)
    else:
        agent = KittyAgent(
            name="kitty", llm=main, system_prompt="sp", tools=[], aux_llm=aux, **kwargs
        )
    return agent, main


# ── get_aux_llm 解析 ──────────────────────────────────────────────

def test_aux_disabled_by_default():
    assert get_aux_llm() is None


def test_aux_built_from_env(monkeypatch):
    _set_env(monkeypatch, LLM_AUX_MODEL_ID="qwen-turbo", LLM_API_KEY="k", LLM_BASE_URL="http://x/v1")
    llm = get_aux_llm()
    assert llm is not None
    assert llm.model == "qwen-turbo"
    assert llm.api_key == "k"
    assert llm.temperature == cfg.LLM_AUX_TEMPERATURE
    assert llm.max_tokens == cfg.LLM_AUX_MAX_TOKENS


def test_aux_env_overrides_settings(monkeypatch):
    """环境变量优先级高于 settings.json。"""
    monkeypatch.setattr(cfg, "LLM_AUX_MODEL_ID", "from-settings", raising=False)
    _set_env(monkeypatch, LLM_AUX_MODEL_ID="from-env", LLM_API_KEY="k", LLM_BASE_URL="http://x/v1")
    assert get_aux_llm().model == "from-env"


def test_aux_reads_settings_when_env_absent(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_AUX_MODEL_ID", "from-settings", raising=False)
    _set_env(monkeypatch, LLM_API_KEY="k", LLM_BASE_URL="http://x/v1")
    assert get_aux_llm().model == "from-settings"


def test_aux_dedicated_credentials(monkeypatch):
    """LLM_AUX_* 可单独指定另一家供应商的凭据。"""
    _set_env(
        monkeypatch,
        LLM_AUX_MODEL_ID="qwen-turbo", LLM_AUX_API_KEY="aux-k", LLM_AUX_BASE_URL="http://aux/v1",
        LLM_API_KEY="main-k", LLM_BASE_URL="http://main/v1",
    )
    llm = get_aux_llm()
    assert (llm.api_key, llm.base_url) == ("aux-k", "http://aux/v1")


def test_aux_missing_credentials_returns_none(monkeypatch):
    """只有模型名、没有 key/base_url → 返回 None（由调用方回退主模型）。"""
    _set_env(monkeypatch, LLM_AUX_MODEL_ID="qwen-turbo")
    assert get_aux_llm() is None


def test_aux_failure_not_cached(monkeypatch):
    """失败结果不入缓存：补齐凭据后应能立刻生效，避免 None 被钉死。"""
    _set_env(monkeypatch, LLM_AUX_MODEL_ID="qwen-turbo")
    assert get_aux_llm() is None
    _set_env(monkeypatch, LLM_API_KEY="k", LLM_BASE_URL="http://x/v1")
    assert get_aux_llm() is not None


def test_aux_success_cached(monkeypatch):
    _set_env(monkeypatch, LLM_AUX_MODEL_ID="qwen-turbo", LLM_API_KEY="k", LLM_BASE_URL="http://x/v1")
    assert get_aux_llm() is get_aux_llm()


# ── KittyAgent 注入 ───────────────────────────────────────────────

def test_compressor_uses_aux_model():
    aux = MagicMock()
    agent, _main = make_agent(aux=aux)
    assert agent.aux_model is aux
    _, compressor, _ = agent._new_turn(None)
    assert compressor._llm is aux


def test_memory_recall_uses_aux_model():
    aux = MagicMock()
    agent, _ = make_agent(aux=aux, memory=MagicMock())
    assert agent._memory_recall._llm is aux


async def test_memory_extraction_uses_aux_model(monkeypatch):
    from kittymind.agent import kitty_agent as module

    captured = {}

    def fake_extract(messages, llm, store):
        captured["llm"] = llm
        return False

    monkeypatch.setattr(module, "extract_memories", fake_extract)
    aux = MagicMock()
    agent, _ = make_agent(aux=aux, memory=MagicMock())
    await agent._extract_memories_bg([{"role": "user", "content": "hi"}])
    assert captured["llm"] is aux


def test_fallback_to_main_when_aux_unset():
    """未配置 LLM_AUX_MODEL_ID：显式传 None 时全部回退主模型（旧行为）。"""
    agent, main = make_agent(aux=None)
    assert agent.aux_model is main
    _, compressor, _ = agent._new_turn(None)
    assert compressor._llm is main


def test_fallback_to_main_when_aux_not_configured():
    """默认自动解析 + 未配置 → 同样回退主模型，不报错。"""
    agent, main = make_agent()
    assert agent.aux_model is main


def test_auto_resolution_picks_env_model(monkeypatch):
    """默认自动解析路径：配置了 aux 就应真正用上。"""
    _set_env(monkeypatch, LLM_AUX_MODEL_ID="qwen-turbo", LLM_API_KEY="k", LLM_BASE_URL="http://x/v1")
    agent, _main = make_agent()
    assert agent.aux_llm is not None
    assert agent.aux_llm.model == "qwen-turbo"
    _, compressor, _ = agent._new_turn(None)
    assert compressor._llm is agent.aux_llm


# ── 端到端：压缩摘要实际调用落在辅助模型上 ────────────────────────

class FakeLLM:
    """记录调用的假模型。"""

    def __init__(self, model: str, reply: str):
        self.model = model
        self.reply = reply
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        return MagicMock(content=self.reply)


def test_compression_summary_hits_aux_model_only():
    main = FakeLLM("main-model", "MAIN-REPLY")
    aux = FakeLLM("aux-model", "AUX-REPLY")
    agent = KittyAgent(
        name="kitty", llm=main, system_prompt="sp", tools=[], aux_llm=aux
    )
    _, compressor, _ = agent._new_turn(None)

    summary = compressor._make_summary(
        [{"role": "system", "content": "sp"}],
        [
            {"role": "user", "content": "帮我改一下配置"},
            {"role": "assistant", "content": "好的，我来处理"},
        ],
    )

    assert summary is not None
    assert "AUX-REPLY" in summary["content"]
    assert len(aux.calls) == 1
    assert main.calls == [], "压缩摘要不应消耗主模型额度"

