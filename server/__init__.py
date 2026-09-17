"""服务端包：对外只再导出 WebSocket RPC 处理器。

ws_server / permission_bridge 等内部模块直接从 .rpc_handler 导入，
这里的再导出是包的公开 API 约定，用 __all__ 显式声明（避免 F401 误报）。
"""

from .rpc_handler import RpcHandler

__all__ = ["RpcHandler"]
