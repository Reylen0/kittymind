"""WebSocket 服务器。

每个连接创建一个 RpcHandler，连接断开时自动取消该连接的所有任务。
"""

import asyncio
import json
import logging

import websockets
import websockets.exceptions

from kittymind.agent import KittyAgent
from kittymind.config import cfg
from .rpc_handler import RpcHandler

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _ensure_loopback(host: str) -> None:
    """拒绝非环回绑定——本服务无鉴权、无连接数上限，绑定环回是唯一防线。

    WS_HOST 可被环境变量 / settings.json 改成 0.0.0.0，那样同网段任何进程都能
    远程驱动 bash 工具。确有局域网使用需求（自担风险）才配置
    WS_ALLOW_NON_LOOPBACK = true 显式放行。
    """
    if host in _LOOPBACK_HOSTS:
        return
    if cfg.WS_ALLOW_NON_LOOPBACK:
        return  # 用户显式放行；启动日志会让风险可见
    raise RuntimeError(
        f"拒绝绑定非环回地址 {host!r}：WebSocket 服务无鉴权，"
        f"对外暴露等于把 bash 等工具交给同网段所有进程。"
        f"如确有需要，请在 settings.json 配置 WS_ALLOW_NON_LOOPBACK=true（自担风险）。"
    )


async def _handle(websocket, agent: KittyAgent, bridge=None) -> None:
    loop = asyncio.get_running_loop()
    handler = RpcHandler(agent, websocket, bridge=bridge, loop=loop)
    try:
        async for raw in websocket:
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"error": "invalid json"}))
                continue
            await handler.dispatch(request)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await handler.cancel_all()
        if bridge:
            # 只注销本连接：bridge 是进程级单例，清理全部会误伤其他活跃连接
            bridge.clear_connection(handler.conn_id)


async def start_server(agent: KittyAgent, host: str, port: int, bridge=None) -> None:
    _ensure_loopback(host)
    server = await websockets.serve(
        lambda ws: _handle(ws, agent, bridge=bridge), host, port
    )
    if host not in _LOOPBACK_HOSTS:
        # 只在用户显式放行时才会走到这里；风险必须可见
        logging.getLogger(__name__).warning(
            "WebSocket 绑定在非环回地址 %s（WS_ALLOW_NON_LOOPBACK=true），"
            "同网段进程均可访问本服务！", host
        )
    # 这一行走 stdout 的 print 而非 logging，是**对外契约**：
    # electron/main.js 的 waitForReady() 靠扫描 stdout 里的 "[ready] ws://..." 判断
    # 服务端就绪。改成 logging（→ stderr）会让 Electron 永远等不到就绪。
    print(f"[ready] ws://{host}:{port}", flush=True)
    async with server:
        await asyncio.Future()  # 永久运行
