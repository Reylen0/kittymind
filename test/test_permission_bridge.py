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
