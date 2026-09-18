"""统一日志配置"""

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
