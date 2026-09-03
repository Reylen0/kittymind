"""WebSocket 服务器。

每个连接创建一个 RpcHandler，连接断开时自动取消该连接的所有任务。
"""

import asyncio
import json

import websockets
import websockets.exceptions

from kittymind.agent import KittyAgent
from .rpc_handler import RpcHandler


async def _handle(websocket, agent: KittyAgent) -> None:
    handler = RpcHandler(agent, websocket)
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


async def start_server(agent: KittyAgent, host: str, port: int) -> None:
    server = await websockets.serve(
        lambda ws: _handle(ws, agent), host, port
    )
    print(f"[ready] ws://{host}:{port}", flush=True)
    async with server:
        await asyncio.Future()  # 永久运行
