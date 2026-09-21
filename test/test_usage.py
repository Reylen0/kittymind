"""Phase 16 用量成本追踪：recorder / store / pricing。

覆盖三条关键决策：
  1. recorder 按 turn 聚合多次 usage 事件、按模型分组；
  2. model_usage 表 v6→v7 迁移、删会话保留成本记录（session_id 悬空）；
  3. 价目表精确/前缀匹配、settings 覆盖、查不到返回 None。
"""

import pytest

from kittymind.session.manager import SessionManager
from kittymind.usage import UsageRecorder, estimate_cost


# ── UsageRecorder ─────────────────────────────────────────────────

def test_recorder_aggregates_multiple_usage_events():
    r = UsageRecorder()
    r.record({"prompt_tokens": 100, "completion_tokens": 50}, "claude-sonnet-4-6")
    r.record({"prompt_tokens": 200, "completion_tokens": 30}, "claude-sonnet-4-6")
    assert r.prompt_tokens == 300
    assert r.completion_tokens == 80
    assert r.n_calls == 2
    rows = r.rows()
    assert len(rows) == 1
    assert rows[0]["model_id"] == "claude-sonnet-4-6"


def test_recorder_missing_fields_treated_as_zero():
    r = UsageRecorder()
    r.record({}, "m")
    r.record({"prompt_tokens": 10}, "m")  # 缺 completion
    assert r.prompt_tokens == 10
    assert r.completion_tokens == 0
    assert r.n_calls == 2


def test_recorder_merge_subagent():
    parent = UsageRecorder()
    parent.record({"prompt_tokens": 100, "completion_tokens": 50}, "claude-sonnet-4-6")

    sub = UsageRecorder()
    sub.record({"prompt_tokens": 300, "completion_tokens": 100}, "claude-sonnet-4-6")

    parent.merge(sub)
    assert parent.prompt_tokens == 400
    assert parent.completion_tokens == 150
    assert parent.n_calls == 2


def test_recorder_is_empty():
    assert UsageRecorder().is_empty
    r = UsageRecorder()
    r.record({"prompt_tokens": 1}, "m")
    assert not r.is_empty


# ── store: v6→v7 迁移 + 落库 + 聚合 ──────────────────────────────

@pytest.fixture
def mgr(tmp_path):
    return SessionManager(db_path=tmp_path / "usage.db")


def test_usage_table_created_and_writable(mgr):
    mgr.record_usage("s1", "claude-sonnet-4-6", 100, 50, 2)
    mgr.record_usage("s1", "claude-haiku-4-5", 20, 10, 1)
    agg = mgr.aggregate_usage("model")
    assert len(agg) == 2
    by_model = {a["key"]: a for a in agg}
    assert by_model["claude-sonnet-4-6"]["prompt_tokens"] == 100
    assert by_model["claude-haiku-4-5"]["n_calls"] == 1


def test_aggregate_by_day_and_session(mgr):
    mgr.record_usage("s1", "m1", 100, 10, 1)
    mgr.record_usage("s2", "m1", 200, 20, 1)
    by_session = mgr.aggregate_usage("session")
    keys = {a["key"] for a in by_session}
    assert keys == {"s1", "s2"}
    # 按 day 聚合：两条都落在同一天，合成一组
    by_day = mgr.aggregate_usage("day")
    assert len(by_day) == 1
    assert by_day[0]["prompt_tokens"] == 300


def test_aggregate_includes_by_model_breakdown(mgr):
    """day/session 分组带按模型明细，供 rpc 层分组计价。"""
    mgr.record_usage("s1", "claude-sonnet-4-6", 100, 10, 1)
    mgr.record_usage("s1", "claude-haiku-4-5", 200, 20, 1)
    by_day = mgr.aggregate_usage("day")
    assert len(by_day) == 1
    g = by_day[0]
    assert g["prompt_tokens"] == 300
    assert g["by_model"]["claude-sonnet-4-6"] == {"prompt_tokens": 100, "completion_tokens": 10}
    assert g["by_model"]["claude-haiku-4-5"] == {"prompt_tokens": 200, "completion_tokens": 20}


def test_delete_session_preserves_usage(mgr):
    """用户决策：删会话保留成本记录（session_id 悬空，按 model 仍计入）。"""
    mgr.create_session(session_id="s1")
    mgr.record_usage("s1", "m1", 100, 10, 1)
    assert mgr.delete_session("s1")

    # 按 model 聚合仍看得到这条用量（session_id 悬空不参与 session 分组）
    by_model = mgr.aggregate_usage("model")
    assert len(by_model) == 1
    assert by_model[0]["prompt_tokens"] == 100

    # 按 session 聚合：删会话后 session_id 保留原值（审计语义，能追溯成本来自
    # 哪个曾存在的会话），而非 NULL。model_usage 无外键，删会话不会清这条记录。
    by_session = mgr.aggregate_usage("session")
    assert any(a["key"] == "s1" for a in by_session)


def test_session_usage_totals(mgr):
    mgr.record_usage("s1", "m1", 100, 50, 1)
    mgr.record_usage("s1", "m2", 30, 20, 3)
    totals = mgr.session_usage("s1")
    assert totals["prompt_tokens"] == 130
    assert totals["completion_tokens"] == 70
    assert totals["n_calls"] == 4


def test_aggregate_respects_since_until(mgr):
    import time
    base = int(time.time())
    mgr.record_usage("s1", "m1", 100, 10, 1)
    # 手动改 ts 到更早，验证 since 过滤（绕过 record_usage 的当前时间戳）
    with mgr.store._lock:
        mgr.store._conn.execute("UPDATE model_usage SET ts=?", (base - 10000,))
    only_recent = mgr.aggregate_usage("model", since=base - 10)
    assert only_recent == []


# ── pricing ──────────────────────────────────────────────────────

def test_estimate_cost_exact_match():
    c = estimate_cost(1_000_000, 1_000_000, "claude-sonnet-4-6")
    assert c is not None
    assert abs(c.input_usd - 3.00) < 1e-6
    assert abs(c.output_usd - 15.00) < 1e-6


def test_estimate_cost_prefix_match():
    c = estimate_cost(1_000_000, 0, "claude-sonnet-4-20250101")
    assert c is not None
    assert abs(c.input_usd - 3.00) < 1e-6


def test_estimate_cost_unknown_returns_none():
    assert estimate_cost(100, 100, "some-unknown-model") is None


def test_estimate_cost_zero_for_local():
    c = estimate_cost(1_000_000, 1_000_000, "ollama:llama3")
    assert c is not None
    assert c.total_usd == 0.0


def test_cost_from_by_model_mixed_models():
    """day/session 组成本 = 组内各模型分别计价求和；部分未知不拖垮整组。"""
    from kittymind.usage import cost_from_by_model

    by_model = {
        # 1M in + 1M out → 3.00 + 15.00
        "claude-sonnet-4-6": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        # 1M in + 1M out → 1.00 + 5.00
        "claude-haiku-4-5": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        # 价目表查不到 → 贡献 0，但不清零整组
        "some-unknown-model": {"prompt_tokens": 999_999, "completion_tokens": 999_999},
    }
    cost = cost_from_by_model(by_model)
    assert cost is not None
    assert abs(cost - 24.0) < 1e-6

    # 全部未知 → None（不编造）
    assert cost_from_by_model({"mystery": {"prompt_tokens": 1, "completion_tokens": 1}}) is None
    assert cost_from_by_model({}) is None
