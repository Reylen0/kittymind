"""字节解码策略（kittymind.encoding）的特征测试。

动机：file_read/file_edit/grep/git/bash 等工具过去各自固定一种编码假设
（硬编码 utf-8，或固定 Windows OEM 代码页），在遇到非 UTF-8 源文件/子进程
输出时产生"乱码"而不是报错。这里只测解码策略本身（纯函数，不碰真实文件
系统/子进程）；具体工具的集成行为已经被 test_tool_paths.py 等覆盖。
"""

import pytest

import kittymind.encoding as encoding_mod
from kittymind.encoding import decode_bytes, decode_subprocess_output, split_lines


def test_utf8_bytes_decode_as_is():
    text, used = decode_bytes("你好".encode())
    assert text == "你好"
    assert used == "utf-8"


def test_default_utf8_falls_back_to_system_encoding_on_decode_failure(monkeypatch):
    """默认 utf-8 解不出来时，自动退一步试本机编码（这里模拟成 gbk）。"""
    monkeypatch.setattr(encoding_mod, "SYSTEM_ENCODING", "gbk")
    raw = "你好".encode("gbk")

    text, used = decode_bytes(raw)  # encoding 留默认 "utf-8"

    assert text == "你好"
    assert used == "gbk"


def test_explicit_non_utf8_encoding_does_not_auto_fallback(monkeypatch):
    """调用方显式指定了非 utf-8 编码时，解不出来直接报错，不静默换编码。"""
    monkeypatch.setattr(encoding_mod, "SYSTEM_ENCODING", "gbk")
    raw = "你好".encode()

    with pytest.raises(UnicodeDecodeError):
        decode_bytes(raw, encoding="ascii")


def test_universal_newlines_are_normalized():
    """手动解码绕开了文本模式的换行翻译，这里要补回来，否则按行处理会变样。"""
    text, _ = decode_bytes(b"a\r\nb\rc\n")
    assert text == "a\nb\nc\n"


def test_both_encodings_fail_falls_back_to_replace_without_raising(monkeypatch):
    monkeypatch.setattr(encoding_mod, "SYSTEM_ENCODING", "ascii")
    raw = "你好".encode("gbk")

    text, used = decode_bytes(raw)

    assert used == "utf-8"
    assert "�" in text


def test_subprocess_output_prefers_utf8():
    assert decode_subprocess_output("你好".encode()) == "你好"


def test_subprocess_output_falls_back_to_system_encoding_when_not_utf8(monkeypatch):
    """子进程若按系统代码页输出（如 cmd 原生命令），UTF-8 解码失败后要能读对。"""
    monkeypatch.setattr(encoding_mod, "SYSTEM_ENCODING", "gbk")
    raw = "你好".encode("gbk")

    assert decode_subprocess_output(raw) == "你好"


def test_split_lines_matches_readlines_not_splitlines():
    """`str.splitlines()` 还认 \\x0c(FF)/\\x0b(VT) 等 Unicode 分段符，会把它们也
    当成行尾切出额外的行，跟文本模式 readlines() 的行号语义不一致——老代码/
    排版文本里常见的分页符 \\f 会让 file_read 的行号跟编辑器错位。
    """
    text = "a\x0cb\nc\nv\x0bf\n"

    assert text.splitlines(keepends=True) != ["a\x0cb\n", "c\n", "v\x0bf\n"]  # 反面教材：会被切成 5 行
    assert split_lines(text) == ["a\x0cb\n", "c\n", "v\x0bf\n"]


def test_split_lines_handles_no_trailing_newline_and_empty_text():
    assert split_lines("") == []
    assert split_lines("abc") == ["abc"]
    assert split_lines("a\nb") == ["a\n", "b"]
    assert split_lines("a\n") == ["a\n"]

