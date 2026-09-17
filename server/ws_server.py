"""WebSocket 服务器。

每个连接创建一个 RpcHandler，连接断开时自动取消该连接的所有任务。
"""

import asyncio
import json

import websockets
import websockets.exceptions

from kittymind.agent import KittyAgent
from .rpc_handler import RpcHandler


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
    server = await websockets.serve(
        lambda ws: _handle(ws, agent, bridge=bridge), host, port
    )
    # 这一行走 stdout 的 print 而非 logging，是**对外契约**：
    # electron/main.js 的 waitForReady() 靠扫描 stdout 里的 "[ready] ws://..." 判断
    # 服务端就绪。改成 logging（→ stderr）会让 Electron 永远等不到就绪。
    print(f"[ready] ws://{host}:{port}", flush=True)
    async with server:
        await asyncio.Future()  # 永久运行
