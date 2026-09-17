"""PermissionBridge 多连接隔离测试（P1-1 回归）。

旧实现里 bridge 是「全进程一份 (loop, push_fn, pending)」：

  - 连接 B 上线 → set_connection 直接覆盖 → A 触发的审批推到 B 的窗口，
    A 的用户永远等不到（等到超时被 deny）；
  - 任一连接断开 → clear_connection() 清空**全部** pending 并把 push_fn 置 None
    → 仍活跃的连接之后每次审批都被静默拒绝。

本文件把这两条都钉住：多连接各走各的通道，断开一个不影响另一个。
"""

import asyncio
import threading
import time

import pytest

from kittymind.config import cfg
from server.permission_bridge import PermissionBridge


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch):
    """把审批等待压到 5s，避免断言失败时整个测试卡 120s。

    raising=False：在旧代码上跑回滚对照时该配置项还不存在。
    """
    monkeypatch.setattr(cfg, "PERMISSION_ASK_TIMEOUT", 5, raising=False)


class _FakeConn:
    """假连接：记录收到的推送。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[tuple[str, dict]] = []

    async def push(self, method: str, params: dict) -> None:
        self.events.append((method, params))

    def wait_requests(self, count: int, timeout: float = 3.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.events) >= count:
                return [e[1] for e in self.events[:count]]
            time.sleep(0.01)
        raise AssertionError(
            f"{self.name} 未在 {timeout}s 内收到第 {count} 条审批请求"
            f"（实际 {len(self.events)} 条）"
        )


@pytest.fixture
def loop():
    """后台线程里跑一个真事件循环——run_coroutine_threadsafe 需要一个真 loop。"""
    lp = asyncio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    try:
        yield lp
    finally:
        lp.call_soon_threadsafe(lp.stop)
        th.join(timeout=5)
        lp.close()


def _ask_in_thread(bridge: PermissionBridge, conn_id: str, out: dict) -> threading.Thread:
    """在独立线程里 bind + ask，模拟 worker 线程中执行的工具调用。"""
    def worker() -> None:
        token = bridge.bind(conn_id)
        try:
            out["result"] = bridge.ask(
                "bash", {"command": "rm -rf build"}, "命令包含删除操作"
            )
        finally:
            bridge.unbind(token)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    return th


# ── 推送目标不被劫持 ─────────────────────────────────────────────

def test_two_connections_keep_separate_push_targets(loop):
    """A 的审批请求只能到 A，不能因为 B 上线而改投 B。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(loop, a.push)
    cid_b = bridge.set_connection(loop, b.push)
    assert cid_a != cid_b, "两次注册必须拿到不同的连接 id"

    out: dict = {}
    th = _ask_in_thread(bridge, cid_a, out)
    req = a.wait_requests(1)[0]
    assert req["tool"] == "bash"
    assert not b.events, "A 的审批请求被推送给了 B（推送目标被覆盖）"

    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    th.join(timeout=5)
    assert out.get("result") is True


def test_disconnect_does_not_kill_other_connections(loop):
    """B 断开不得误杀 A：旧实现会在此把 A 的挂起请求一起 deny。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(loop, a.push)
    cid_b = bridge.set_connection(loop, b.push)

    out: dict = {}
    th = _ask_in_thread(bridge, cid_a, out)
    req = a.wait_requests(1)[0]

    bridge.clear_connection(cid_b)

    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    th.join(timeout=5)
    assert out.get("result") is True, "B 断开导致 A 的审批被误拒"

    # A 的通道必须仍然可用（旧实现此时 push_fn 已被清空，ask 直接返回 False）
    out2: dict = {}
    th2 = _ask_in_thread(bridge, cid_a, out2)
    req2 = a.wait_requests(2)[1]
    assert bridge.respond(req2["request_id"], True, conn_id=cid_a) is True
    th2.join(timeout=5)
    assert out2.get("result") is True, "B 断开后 A 的审批通道失效"


def test_clear_connection_only_denies_its_own_pending(loop):
    """清理 B 只 deny B 的挂起请求，A 的必须继续等待。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(loop, a.push)
    cid_b = bridge.set_connection(loop, b.push)

    out_a: dict = {}
    out_b: dict = {}
    th_a = _ask_in_thread(bridge, cid_a, out_a)
    th_b = _ask_in_thread(bridge, cid_b, out_b)
    req_a = a.wait_requests(1)[0]
    b.wait_requests(1)

    bridge.clear_connection(cid_b)
    th_b.join(timeout=5)
    assert out_b.get("result") is False, "被清理连接的挂起请求应被 deny"
    assert th_a.is_alive(), "A 的挂起请求被 B 的清理提前唤醒"

    assert bridge.respond(req_a["request_id"], True, conn_id=cid_a) is True
    th_a.join(timeout=5)
    assert out_a.get("result") is True


# ── 归属定位失败 / 跨连接作答 ────────────────────────────────────

def test_ask_without_resolvable_connection_is_denied(loop):
    """多连接活跃且未 bind → 拒绝（fail-closed），绝不猜一个连接推过去。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    bridge.set_connection(loop, a.push)
    bridge.set_connection(loop, b.push)

    assert bridge.ask("bash", {"command": "rm x"}, "命令包含删除操作") is False
    assert not a.events and not b.events


def test_respond_does_not_cross_connections(loop):
    """B 拿 A 的 request_id 作答不得命中。"""
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(loop, a.push)
    cid_b = bridge.set_connection(loop, b.push)

    out: dict = {}
    th = _ask_in_thread(bridge, cid_a, out)
    req = a.wait_requests(1)[0]

    assert bridge.respond(req["request_id"], True, conn_id=cid_b) is False
    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    th.join(timeout=5)
    assert out.get("result") is True


def test_single_connection_resolves_without_bind(loop):
    """单连接场景（单窗口）无需 bind 也能路由，保持既有易用性。"""
    bridge = PermissionBridge()
    a = _FakeConn("A")
    cid_a = bridge.set_connection(loop, a.push)

    out: dict = {}
    th = threading.Thread(
        target=lambda: out.update(result=bridge.ask("bash", {"command": "rm x"}, "删除")),
        daemon=True,
    )
    th.start()
    req = a.wait_requests(1)[0]
    assert bridge.respond(req["request_id"], True, conn_id=cid_a) is True
    th.join(timeout=5)
    assert out.get("result") is True


# ── 端到端：contextvar 穿过 asyncio.to_thread ────────────────────

async def test_contextvar_routes_ask_through_to_thread(loop):
    """两个连接并发跑 turn，各自只该等到自己的审批结果。

    这条走的是真实链路：bind 在 turn 任务里 → asyncio.to_thread 复制 context
    → worker 线程里的 ask() 读同一个 contextvar。
    """
    bridge = PermissionBridge()
    a, b = _FakeConn("A"), _FakeConn("B")
    cid_a = bridge.set_connection(loop, a.push)
    cid_b = bridge.set_connection(loop, b.push)

    async def turn(conn_id: str) -> bool:
        token = bridge.bind(conn_id)
        try:
            return await asyncio.to_thread(
                bridge.ask, "bash", {"command": "rm -rf build"}, "命令包含删除操作"
            )
        finally:
            bridge.unbind(token)

    async def wait_for(conn: _FakeConn, count: int) -> dict:
        for _ in range(300):
            if len(conn.events) >= count:
                return conn.events[count - 1][1]
            await asyncio.sleep(0.01)
        raise AssertionError(f"{conn.name} 未收到审批请求")

    task_a = asyncio.create_task(turn(cid_a))
    task_b = asyncio.create_task(turn(cid_b))

    req_a = await wait_for(a, 1)
    req_b = await wait_for(b, 1)

    assert bridge.respond(req_a["request_id"], True, conn_id=cid_a) is True
    assert bridge.respond(req_b["request_id"], False, conn_id=cid_b) is True

    assert await task_a is True
    assert await task_b is False, "两个连接的结果串了（contextvar 未正确隔离）"
