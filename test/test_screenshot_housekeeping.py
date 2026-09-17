"""截图目录维护测试。

截图是唯一会持续落盘的内置工具产物：旧实现用 f"screenshot_{int(time.time())}.png"
命名，同一秒连拍两张直接覆盖；且从不清理，长时间使用后目录无限增长。
"""

import os
import time

from kittymind.tools.builtin.screenshot_tool import _prune, _unique_path


def test_unique_path_avoids_same_second_collision(tmp_path):
    """同一秒内多次截图必须落到不同文件，否则模型拿到「已保存」却内容不符。"""
    first = _unique_path(tmp_path)
    first.write_bytes(b"a")
    second = _unique_path(tmp_path)
    second.write_bytes(b"b")
    third = _unique_path(tmp_path)

    assert len({first, second, third}) == 3
    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"


def test_prune_keeps_newest(tmp_path):
    for i in range(5):
        p = tmp_path / f"screenshot_2026010{i}_120000.png"
        p.write_bytes(b"x")
        os.utime(p, (time.time() + i, time.time() + i))  # 越后越新

    removed = _prune(tmp_path, keep=2)

    assert removed == 3
    left = sorted(p.name for p in tmp_path.glob("screenshot_*.png"))
    assert left == ["screenshot_20260103_120000.png", "screenshot_20260104_120000.png"]


def test_prune_touches_only_its_own_files(tmp_path):
    """用户在截图目录里放的文件绝不能被误删。"""
    for i in range(3):
        (tmp_path / f"screenshot_2026{i}.png").write_bytes(b"x")
    keep_me = tmp_path / "important.png"
    keep_me.write_bytes(b"user data")
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")

    _prune(tmp_path, keep=1)

    assert keep_me.exists(), "非 screenshot_ 前缀的文件被误删"
    assert note.exists()
    assert len(list(tmp_path.glob("screenshot_*.png"))) == 1


def test_prune_disabled_with_zero(tmp_path):
    for i in range(3):
        (tmp_path / f"screenshot_{i}.png").write_bytes(b"x")
    assert _prune(tmp_path, keep=0) == 0
    assert len(list(tmp_path.glob("screenshot_*.png"))) == 3
