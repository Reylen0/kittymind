"""PermissionBridge 多连接隔离测试（P1-1 回归）。

旧实现里 bridge 是「全进程一份 (loop, push_fn, pending)」：

  - 连接 B 上线 → set_connection 直接覆盖 → A 触发的审批推到 B 的窗口，
    A 的用户永远等不到（等到超时被 deny）；
  - 任一连接断开 → clear_connection() 清空**全部** pending 并把 push_fn 置 None
    → 仍活跃的连接之后每次审批都被静默拒绝。

本文件把这两条都钉住：多连接各走各的通道，断开一个不影响另一个。

`ask()` 现在是纯 async（await 一个 Future，不跨线程），所以整份测试都在单个事件
循环上跑：并发场景靠 `asyncio.create_task`，不再需要真实的 `threading.Thread`
和后台事件循环。
"""

import asyncio

import pytest

from kittymind.agent.delegation import reset_root_session, set_root_session
from kittymind.config import cfg
from server.permission_bridge import PermissionBridge


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch):
    """把审批等待压到 5s，避免断言失败时整个测试卡 120s。"""
    monkeypatch.setattr(cfg, "PERMISSION_ASK_TIMEOUT", 5, raising=False)


class _FakeConn:
    """假连接：记录收到的推送。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[tuple[str, dict]] = []

    async def push(self, method: str, params: dict) -> None:
        self.events.append((method, params))

    async def wait_requests(self, count: int, timeout: float = 3.0) -> list[dict]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.events) >= count:
                return [e[1] for e in self.events[:count]]
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"{self.name} 未在 {timeout}s 内收到第 {count} 条审批请求"
            f"（实际 {len(self.events)} 条）"
        )


def _ask_task(bridge: PermissionBridge, conn_id: str) -> asyncio.Task:
    """起一个 task：bind + ask + unbind，模拟一次工具调用触发的审批。"""
    async def worker() -> bool:
        token = bridge.bind(conn_id)
        try:
            return await bridge.ask("bash", {"command": "rm -rf build"}, "命令包含删除操作")
        finally:
            bridge.unbind(token)

    return asyncio.create_task(worker())


# ── 推送目标不被劫持 ─────────────────────────────────────────────

async def test_two_connections_keep_separate_push_targets():
    """A 的审批请求只能到 A，不能因为 B 上线而改投 B。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(a.push)
    cid_b = bridge.set_connection(b.push)
    assert cid_a != cid_b, "两次注册必须拿到不同的连接 id"

    task = _ask_task(bridge, cid_a)
    req = (await a.wait_requests(1))[0]
    assert req["tool"] == "bash"
    assert not b.events, "A 的审批请求被推送给了 B（推送目标被覆盖）"

    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    assert await task is True


async def test_disconnect_does_not_kill_other_connections():
    """B 断开不得误杀 A：旧实现会在此把 A 的挂起请求一起 deny。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(a.push)
    cid_b = bridge.set_connection(b.push)

    task = _ask_task(bridge, cid_a)
    req = (await a.wait_requests(1))[0]

    bridge.clear_connection(cid_b)

    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    assert await task is True, "B 断开导致 A 的审批被误拒"

    # A 的通道必须仍然可用（旧实现此时 push_fn 已被清空，ask 直接返回 False）
    task2 = _ask_task(bridge, cid_a)
    req2 = (await a.wait_requests(2))[1]
    assert bridge.respond(req2["request_id"], True, conn_id=cid_a) is True
    assert await task2 is True, "B 断开后 A 的审批通道失效"


async def test_clear_connection_only_denies_its_own_pending():
    """清理 B 只 deny B 的挂起请求，A 的必须继续等待。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(a.push)
    cid_b = bridge.set_connection(b.push)

    task_a = _ask_task(bridge, cid_a)
    task_b = _ask_task(bridge, cid_b)
    req_a = (await a.wait_requests(1))[0]
    await b.wait_requests(1)

    bridge.clear_connection(cid_b)
    assert await task_b is False, "被清理连接的挂起请求应被 deny"
    assert task_a.done() is False, "A 的挂起请求被 B 的清理提前唤醒"

    assert bridge.respond(req_a["request_id"], True, conn_id=cid_a) is True
    assert await task_a is True


# ── 归属定位失败 / 跨连接作答 ────────────────────────────────────

async def test_ask_without_resolvable_connection_is_denied():
    """多连接活跃且未 bind → 拒绝（fail-closed），绝不猜一个连接推过去。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    bridge.set_connection(a.push)
    bridge.set_connection(b.push)

    assert await bridge.ask("bash", {"command": "rm x"}, "命令包含删除操作") is False
    assert not a.events and not b.events


async def test_respond_does_not_cross_connections():
    """B 拿 A 的 request_id 作答不得命中。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(a.push)
    cid_b = bridge.set_connection(b.push)

    task = _ask_task(bridge, cid_a)
    req = (await a.wait_requests(1))[0]

    assert bridge.respond(req["request_id"], True, conn_id=cid_b) is False
    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    assert await task is True


async def test_single_connection_resolves_without_bind():
    """单连接场景（单窗口）无需 bind 也能路由，保持既有易用性。"""
    bridge = PermissionBridge()
    a = _FakeConn("A")
    cid_a = bridge.set_connection(a.push)

    task = asyncio.create_task(bridge.ask("bash", {"command": "rm x"}, "删除"))
    req = (await a.wait_requests(1))[0]
    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    assert await task is True


# ── 端到端：contextvar 穿过并发 task ──────────────────────────────

async def test_contextvar_routes_ask_through_concurrent_tasks():
    """两个连接并发跑 turn，各自只该等到自己的审批结果。

    这条走的是真实链路：bind 在 turn 任务里 → 同一个 task 内 await bridge.ask()
    读到同一个 contextvar；两个 task 并发跑，contextvar 互不干扰。
    """
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(a.push)
    cid_b = bridge.set_connection(b.push)

    async def turn(conn_id: str) -> bool:
        token = bridge.bind(conn_id)
        try:
            return await bridge.ask("bash", {"command": "rm -rf build"}, "命令包含删除操作")
        finally:
            bridge.unbind(token)

    task_a = asyncio.create_task(turn(cid_a))
    task_b = asyncio.create_task(turn(cid_b))

    req_a = (await a.wait_requests(1))[0]
    req_b = (await b.wait_requests(1))[0]

    assert bridge.respond(req_a["request_id"], True, conn_id=cid_a) is True
    assert bridge.respond(req_b["request_id"], False, conn_id=cid_b) is True

    assert await task_a is True
    assert await task_b is False, "两个连接的结果串了（contextvar 未正确隔离）"


# ── 超时撤回推送（permission_expired）──────────────────────────

async def test_timeout_pushes_permission_expired(monkeypatch):
    """ask 超时 → 返回 False，且向归属连接推送 tool.permission_expired。

    没有这条推送时前端弹窗会悬挂：用户点「允许」→ respond 返回 matched=false，
    观感是「批准了却没执行」。
    """
    monkeypatch.setattr(cfg, "PERMISSION_ASK_TIMEOUT", 0.05, raising=False)
    bridge = PermissionBridge()
    a = _FakeConn("A")
    cid_a = bridge.set_connection(a.push)

    task = _ask_task(bridge, cid_a)
    req = (await a.wait_requests(1))[0]

    assert await task is False, "超时应按拒绝处理"
    expired = [e for e in a.events if e[0] == "tool.permission_expired"]
    assert len(expired) == 1, "超时后必须推送 permission_expired 撤回弹窗"
    assert expired[0][1]["request_id"] == req["request_id"]

    # 超时后迟到的批准不再命中任何挂起请求
    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is False


async def test_expired_push_failure_is_suppressed(monkeypatch):
    """推送 permission_expired 时连接已断 → 静默放弃，不影响超时拒绝语义。"""
    monkeypatch.setattr(cfg, "PERMISSION_ASK_TIMEOUT", 0.05, raising=False)

    class _BrokenConn(_FakeConn):
        async def push(self, method: str, params: dict) -> None:
            if method == "tool.permission_expired":
                raise RuntimeError("connection closed")
            await super().push(method, params)

    bridge = PermissionBridge()
    a = _BrokenConn("A")
    cid_a = bridge.set_connection(a.push)

    task = _ask_task(bridge, cid_a)
    await a.wait_requests(1)
    assert await task is False, "推送失败不得改变超时拒绝的返回值"


# ── 会话归属与待审批补拉（切会话弹窗消失的回归）──────────────────
#
# 症状：会话 A 弹出审批 → 切到 B 再切回 A → 弹窗消失、界面像已结束。
# 两个成因都在这里钉住：
#   1) payload 缺 session_id → 前端无法过滤，B 的弹窗会串到 A 的界面上；
#   2) 只有「推一次」没有可查询的待审批状态 → 错过就再也拿不回来。


def _ask_task_in_session(
    bridge: PermissionBridge, conn_id: str, session_id: str
) -> asyncio.Task:
    """起一个 task：set_root_session + bind + ask，模拟某会话里工具触发的审批。"""
    async def worker() -> bool:
        session_token = set_root_session(session_id)
        conn_token = bridge.bind(conn_id)
        try:
            return await bridge.ask("bash", {"command": "rm -rf build"}, "命令包含删除操作")
        finally:
            bridge.unbind(conn_token)
            reset_root_session(session_token)

    return asyncio.create_task(worker())


async def test_permission_payload_carries_session_id():
    """审批请求必须带 session_id —— 前端靠它把弹窗归属到发起它的会话。"""
    bridge = PermissionBridge()
    a = _FakeConn("A")
    cid_a = bridge.set_connection(a.push)

    task = _ask_task_in_session(bridge, cid_a, "sess-1")
    req = (await a.wait_requests(1))[0]
    assert req["session_id"] == "sess-1"

    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    assert await task is True


async def test_pending_for_returns_only_matching_session():
    """补拉按会话过滤：查 sess-2 不得把 sess-1 的待审批一并带回来。

    反例就是「切回会话看到别的会话的弹窗」——比弹窗消失更难排查。
    """
    bridge = PermissionBridge()
    a = _FakeConn("A")
    cid_a = bridge.set_connection(a.push)

    task1 = _ask_task_in_session(bridge, cid_a, "sess-1")
    task2 = _ask_task_in_session(bridge, cid_a, "sess-2")
    await a.wait_requests(2)

    only2 = bridge.pending_for("sess-2", conn_id=cid_a)
    assert len(only2) == 1
    assert only2[0]["session_id"] == "sess-2"
    # 快照字段必须完整，否则前端拿到也重放不出弹窗
    assert only2[0]["tool"] == "bash"
    assert only2[0]["args"] == {"command": "rm -rf build"}
    assert only2[0]["reason"] == "命令包含删除操作"
    assert only2[0]["request_id"]

    assert bridge.respond(only2[0]["request_id"], True, conn_id=cid_a) is True
    assert await task2 is True
    assert bridge.pending_for("sess-2", conn_id=cid_a) == [], "已作答的审批不该还在列表里"

    still = bridge.pending_for("sess-1", conn_id=cid_a)
    assert len(still) == 1, "sess-1 未被作答，其待审批必须保留"

    assert bridge.respond(still[0]["request_id"], False, conn_id=cid_a) is True
    assert await task1 is False


async def test_pending_for_does_not_cross_connections():
    """A 的待审批不能被 B 查到：bridge 是进程级单例，必须按 conn_id 限定。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(a.push)
    cid_b = bridge.set_connection(b.push)

    task = _ask_task_in_session(bridge, cid_a, "sess-1")
    await a.wait_requests(1)

    assert bridge.pending_for("sess-1", conn_id=cid_b) == []
    req = bridge.pending_for("sess-1", conn_id=cid_a)
    assert len(req) == 1

    assert bridge.respond(req[0]["request_id"], True, conn_id=cid_a) is True
    assert await task is True


async def test_timeout_clears_pending_and_expired_carries_session(monkeypatch):
    """超时后待审批列表必须清空，且撤回推送同样带会话归属。

    不清空的话，切回会话会补拉出一个早已作废的弹窗，用户点了却没反应。
    """
    monkeypatch.setattr(cfg, "PERMISSION_ASK_TIMEOUT", 0.05, raising=False)
    bridge = PermissionBridge()
    a = _FakeConn("A")
    cid_a = bridge.set_connection(a.push)

    task = _ask_task_in_session(bridge, cid_a, "sess-9")
    await a.wait_requests(1)
    assert await task is False, "超时应按拒绝处理"

    expired = [e for e in a.events if e[0] == "tool.permission_expired"]
    assert len(expired) == 1
    assert expired[0][1]["session_id"] == "sess-9", "撤回推送也必须带会话归属"
    assert bridge.pending_for("sess-9", conn_id=cid_a) == []
