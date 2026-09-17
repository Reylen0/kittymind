"""统一日志配置（报告 §3.4）。

**为什么要从 `print` 换成 `logging`**：`print` 没有级别（打包后无法只看 warn 以上）、
没有时间戳（排查时序问题没有锚点）、无法重定向、也无法统一脱敏。而日志在本项目里
是排查"模型为什么这么干"的主要手段，值得一个正经通道。

**两条通道的划分（重要，别混用）**
  - **stdout**：给人和"外部程序契约"看的。
    · `server/ws_server.py` 的 `[ready] ws://host:port` —— Electron 主进程靠监听
      stdout 这一行判断服务端就绪，**这是契约，不能挪到日志里**。
    · `server/app.py` 的 `[aux] ...` 自检行与端口重试告警 —— 启动横幅，用户要"一眼看到"。
    · `chat.py` / `chat_async.py` 的对话输出与提示符。
    · `tools/permission.py` 的 `_cli_ask()` 交互提示（要和随后的 input() 贴在一起）。
  - **stderr（logging）**：诊断与业务日志（压缩、记忆提取、后台任务异常等）。

**例外**：`config.py` 的 `_warn()` 刻意保留 stderr 直写，不走 logging —— 配置在
`setup_logging()` 之前就被 import 并解析，此时日志尚未配置；更重要的是「你的配置被
忽略了」这类信息**不应该被日志级别过滤掉**，必须无条件可见。
"""

import logging
import sys

from .config import cfg

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


class _StderrHandler(logging.StreamHandler):
    """带类型标记的 stderr handler。

    用类型而不是「根 logger 有多少 handler」来判断幂等：别的库（或 pytest）
    可能先往根 logger 挂了自己的 handler，若以「根 logger 非空就跳过」为准，
    我们自己的 handler 会永远装不上，业务日志静默消失。
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))


def setup_logging(level: str | int | None = None) -> None:
    """配置根 logger。幂等：重复调用只更新级别，不会叠加本模块的 handler。

    级别取 `cfg.LOG_LEVEL`（settings.json 可覆盖），非法值回退 INFO 并发一条告警。
    """
    if level is None:
        level = _resolve_level(cfg.LOG_LEVEL)

    root = logging.getLogger()
    if not any(isinstance(h, _StderrHandler) for h in root.handlers):
        root.addHandler(_StderrHandler())
    root.setLevel(level)


def _resolve_level(value: object) -> int:
    if isinstance(value, int):
        return value
    name = str(value or "").strip().upper()
    resolved = logging.getLevelName(name)
    if isinstance(resolved, int):
        return resolved
    # 配置写错时不能静默——否则日志整段消失，排查时最先怀疑的就是日志本身
    logging.getLogger("kittymind.logging").warning(
        "LOG_LEVEL=%r 不是合法级别（可用 DEBUG/INFO/WARNING/ERROR），回退 INFO", value
    )
    return logging.INFO
