"""审查报告 §3.6 零散项的行为回归线（2026-09-18 修复批次）。

只收**行为可观察**的修复；纯死代码删除（AgentEvent / on_tool_error）与
等价改写（LLMResponse dataclass）由全量套件绿 + grep 取证，不在此列。

每条测试附旧行为，作为「真能拦住旧 bug」的对照锚点。
"""

import asyncio
import json

import pytest

from kittymind import config as config_mod
from kittymind.core.llm_response import LLMResponse
from kittymind.memory.recall import MemoryRecall
from kittymind.session.manager import SessionManager
from kittymind.session.store import SqliteSessionStore


# ── 公共 fixture（与 test_config_parse.py 同款，本地复制因 test/ 非 package）──

@pytest.fixture
def make_cfg(tmp_path, monkeypatch, capsys):
    """用临时 settings.json 构造一个独立的 _Config 实例。"""
    def _make(settings: dict) -> config_mod._Config:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        monkeypatch.setattr(config_mod, "_SETTINGS_FILE", path)
        return config_mod._Config()
    return _make


# ── #1 TOOL_MAX_OUTPUT 改名（旧名 BASH_MAX_OUTPUT 管全局，名不副实）──

def test_tool_max_output_overridable(make_cfg):
    assert make_cfg({"TOOL_MAX_OUTPUT": 123}).TOOL_MAX_OUTPUT == 123


def test_deprecated_bash_max_output_alias_still_works_but_warns(make_cfg, capsys):
    """settings.json 里的旧名仍生效（向后兼容），但必须告警提示改名。"""
    cfg = make_cfg({"BASH_MAX_OUTPUT": 456})
    assert cfg.TOOL_MAX_OUTPUT == 456
    err = capsys.readouterr().err
    assert "BASH_MAX_OUTPUT" in err and "TOOL_MAX_OUTPUT" in err


# ── #14 召回关键词兜底对中文无效 ─────────────────────────────────

class _NeverLLM:
    """_llm_select 恒失败 → 必走关键词兜底。"""

    def invoke(self, messages, **kwargs):
        raise RuntimeError("llm unavailable")


def make_recall():
    return MemoryRecall(memory_store=None, llm=_NeverLLM())


def test_chinese_input_matches_memory_via_keyword_fallback():
    """核心回归：旧实现按空格切词，中文整句一个 token、被 len>2 过滤后为空——
    LLM 选择失败时中文召回必然为空。现在 CJK 连续段切 2-gram 可命中。"""
    recall = make_recall()
    memories = [
        {"name": "数据库", "description": "", "body": "生产库使用 PostgreSQL，连接池上限 20"},
        {"name": "前端", "description": "", "body": "渲染层用 React + Vite"},
    ]
    picked = recall._keyword_select("数据库连接池怎么配置的", memories)
    assert picked == [0]


def test_latin_keywords_still_work():
    recall = make_recall()
    memories = [{"name": "deploy", "description": "", "body": "deploy script uses docker compose"}]
    assert recall._keyword_select("how does deploy work", memories) == [0]


def test_extract_keywords_shapes():
    kw = make_recall()._extract_keywords("用 Postgres 连接池")
    assert "postgres" in kw            # 拉丁整词
    assert "连接" in kw and "接池" in kw  # CJK 2-gram


# ── #8 rpc_handler 如实返回 ──────────────────────────────────────

def _bare_handler(mgr=None):
    """绕开 __init__ 造一个最小 RpcHandler（只测单个方法，不碰路由表）。"""
    from server.rpc_handler import RpcHandler

    handler = RpcHandler.__new__(RpcHandler)
    handler._tasks = {}
    handler._bridge = None
    handler.conn_id = None
    sent = []

    async def _send(data):
        sent.append(data)

    handler._send = _send
    if mgr is not None:
        handler._mgr = lambda: mgr
    return handler, sent


def test_session_delete_reports_missing_session_honestly(tmp_path):
    """旧实现删不存在的会话也返回 {"deleted": True}。"""
    mgr = SessionManager(db_path=tmp_path / "s.db")
    mgr.create_session(title="t", session_id="s1")
    handler, sent = _bare_handler(mgr)

    asyncio.run(handler._session_delete("r1", {"session_id": "s1"}))
    asyncio.run(handler._session_delete("r2", {"session_id": "ghost"}))
    assert sent[0]["result"] == {"deleted": True}
    assert sent[1]["result"] == {"deleted": False}


def test_permission_response_ok_false_without_bridge():
    """旧实现 bridge=None 也返回 ok:true，掩盖「审批没人接」。"""
    handler, sent = _bare_handler()

    asyncio.run(handler._permission_response("r1", {"request_id": "x", "approved": True}))
    assert sent[0]["result"] == {"ok": False, "matched": False}


# ── #11 WS 非环回绑定守卫 ────────────────────────────────────────

def test_loopback_guard_rejects_wildcard_host():
    from server import ws_server

    with pytest.raises(RuntimeError, match="非环回"):
        ws_server._ensure_loopback("0.0.0.0")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_guard_allows_loopback(host):
    from server import ws_server

    ws_server._ensure_loopback(host)  # 不抛即通过


def test_loopback_guard_explicit_opt_out(monkeypatch):
    """显式配置 WS_ALLOW_NON_LOOPBACK=true 才放行（用户自担风险）。"""
    from server import ws_server

    monkeypatch.setattr(config_mod.cfg, "WS_ALLOW_NON_LOOPBACK", True)
    ws_server._ensure_loopback("0.0.0.0")  # 不抛即通过


# ── #13 session/store 关闭 ───────────────────────────────────────

def test_session_store_close_is_idempotent(tmp_path):
    store = SqliteSessionStore(tmp_path / "s.db")
    store.close()
    store.close()  # 二次 close 不抛
    assert store._conn is None


def test_session_manager_close_delegates(tmp_path):
    mgr = SessionManager(db_path=tmp_path / "s.db")
    mgr.close()
    assert mgr.store._conn is None


# ── #5 _base_kwargs 不再原地修改调用方 dict ─────────────────────

def test_base_kwargs_does_not_mutate_caller_dict():
    from types import SimpleNamespace
    from kittymind.core.llm import BaseAgentLLM

    fake_self = SimpleNamespace(temperature=0.5, max_tokens=100)
    caller = {"temperature": 0.9, "custom": "x"}
    out = BaseAgentLLM._base_kwargs(fake_self, caller)
    assert out["temperature"] == 0.9 and out["custom"] == "x"
    assert caller == {"temperature": 0.9, "custom": "x"}  # 旧实现会被 pop 成 {}


# ── #15 LLMResponse dataclass 等价 ──────────────────────────────

def test_llm_response_dataclass_contract():
    empty = LLMResponse()
    assert empty.content is None and empty.tool_calls == []
    assert empty.is_tool_call() is False

    with_tools = LLMResponse(content=None, tool_calls=[{"id": "1"}])
    assert with_tools.is_tool_call() is True


# ── #2 guardrails 两集合互斥（恒真判断删除的前提）────────────────

def test_idempotent_and_mutating_tool_sets_are_disjoint():
    """若有人把同一工具同时放进两个集合，说明分类错了——该修分类，
    而不是给判断加回 `and name not in MUTATING_TOOLS`（恒真冗余）。"""
    from kittymind.tools.guardrails import IDEMPOTENT_TOOLS, MUTATING_TOOLS

    assert IDEMPOTENT_TOOLS.isdisjoint(MUTATING_TOOLS)
