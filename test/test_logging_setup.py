"""日志通道测试（报告 §3.4 回归）。

`print` → `logging` 的迁移有个容易踩的坑：**stdout 是外部程序的契约通道**
（`electron/main.js` 的 `waitForReady()` 扫描 stdout 里的 `[ready] ws://...`）。
如果日志配置不当把业务日志写进 stdout，或把就绪行挪进日志，Electron 就会永远
等不到服务端就绪，表现是"界面一直转圈"而不是报错。

本文件钉住两件事：日志去 stderr、就绪行留在 stdout。
"""

import asyncio
import contextlib
import logging

import pytest

from kittymind.config import cfg
from kittymind.logging_setup import _StderrHandler, _resolve_level, setup_logging


@pytest.fixture
def fresh_logging():
    """清掉本模块的 handler，让 setup_logging 在干净状态下重建。

    不能清空「全部」handler——pytest 的日志插件也会往根 logger 挂 handler，
    这里只移除我们自己那类，既保证干净又不破坏 pytest 的捕获。
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for h in [h for h in root.handlers if isinstance(h, _StderrHandler)]:
        root.removeHandler(h)
    try:
        yield root
    finally:
        for h in [h for h in root.handlers if isinstance(h, _StderrHandler)]:
            root.removeHandler(h)
        # 精确还原，且不留任何绑定了已失效捕获流的旧 handler
        root.handlers[:] = [h for h in saved_handlers if not isinstance(h, _StderrHandler)]
        root.setLevel(saved_level)


def _our_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if isinstance(h, _StderrHandler)]


# ── 级别解析 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("debug", logging.DEBUG),
    ("DEBUG", logging.DEBUG),
    ("info", logging.INFO),
    ("WARNING", logging.WARNING),
    (" Error ", logging.ERROR),
    (logging.CRITICAL, logging.CRITICAL),
])
def test_resolve_level_accepts_valid_values(value, expected):
    assert _resolve_level(value) == expected


@pytest.mark.parametrize("value", ["VERBOSE", "", None, "10x", object()])
def test_resolve_level_falls_back_to_info(value):
    """非法级别回退 INFO，而不是静默把日志全关掉。"""
    assert _resolve_level(value) == logging.INFO


def test_level_comes_from_config(fresh_logging, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_LEVEL", "DEBUG")
    setup_logging()
    assert fresh_logging.level == logging.DEBUG


def test_explicit_level_overrides_config(fresh_logging, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_LEVEL", "DEBUG")
    setup_logging("ERROR")
    assert fresh_logging.level == logging.ERROR


# ── handler 管理 ─────────────────────────────────────────────────

def test_setup_logging_is_idempotent(fresh_logging):
    """重复调用（多个入口各自调用一次）不能叠加 handler，否则日志重复输出。"""
    setup_logging("INFO")
    assert len(_our_handlers(fresh_logging)) == 1
    setup_logging("DEBUG")
    assert len(_our_handlers(fresh_logging)) == 1
    assert fresh_logging.level == logging.DEBUG


def test_handler_installed_even_if_root_already_has_handlers(fresh_logging):
    """根 logger 上已有别人的 handler 时，我们自己的 handler 仍要装上。

    反例（旧实现）：以「根 logger 非空就跳过」判断幂等 —— 只要别的库先挂了
    handler，业务日志就永远不输出，且完全无声。
    """
    foreign = logging.NullHandler()
    fresh_logging.addHandler(foreign)
    try:
        setup_logging("INFO")
        assert len(_our_handlers(fresh_logging)) == 1
        assert foreign in fresh_logging.handlers
    finally:
        fresh_logging.removeHandler(foreign)


# ── 通道划分：日志走 stderr，不碰 stdout ─────────────────────────

def test_business_logs_go_to_stderr_not_stdout(fresh_logging, capsys):
    setup_logging("INFO")
    logging.getLogger("kittymind.probe").warning("probe-message")

    captured = capsys.readouterr()
    assert "probe-message" in captured.err, "业务日志未写入 stderr"
    assert "probe-message" not in captured.out, "业务日志闯进了 stdout（会污染就绪判断通道）"


def test_log_line_has_timestamp_and_level(fresh_logging, capsys):
    setup_logging("INFO")
    logging.getLogger("kittymind.probe").warning("probe-message")
    err = capsys.readouterr().err

    assert "WARNING" in err
    assert "kittymind.probe" in err
    assert ":" in err  # 时间戳分隔


# ── stdout 契约：[ready] 必须留在 stdout ─────────────────────────

async def test_ready_banner_stays_on_stdout(capsys):
    """端到端：真的起一次服务端，确认 [ready] 行出现在 stdout 而非 stderr。

    electron/main.js 的 waitForReady() 就靠这一行；它一旦挪进 logging（→ stderr），
    Electron 会一直等不到就绪。
    """
    from server.ws_server import start_server

    task = asyncio.create_task(start_server(None, "127.0.0.1", 0))
    seen_out = ""
    try:
        for _ in range(300):
            captured = capsys.readouterr()
            seen_out += captured.out
            if "[ready]" in seen_out:
                assert "[ready]" not in captured.err
                assert "ws://127.0.0.1:" in seen_out
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"stdout 未出现 [ready] 行，实际输出: {seen_out!r}")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
