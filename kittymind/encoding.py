"""文件/子进程字节解码的唯一入口 —— 不猜编码，但也不无条件假定 UTF-8。

过去的坑：`file_read`/`file_edit`/`grep`/`git`/验证器等多处各自固定
`open(..., encoding="utf-8", errors="replace")` 或 `subprocess.run(encoding=OEM, errors="replace")`。
Windows 上大量旧文件（记事本旧版、Excel 导出、日志）实际是 GBK（cp936），按 UTF-8
硬解会把中文字节序列解成一串 U+FFFD；反过来，现代子进程（尤其继承了
`PYTHONUTF8=1` 的嵌套 Python/CLI）输出的是 UTF-8，固定用 OEM 代码页解又会把
UTF-8 字节错读成另一种乱码（"假汉字"）。`errors="replace"` 把这些失败都悄悄
吞掉，用户看到的就是乱码而不是报错——多处各写一份还导致策略互相矛盾。

本模块不引入 chardet 之类的重量检测依赖：调用方显式传了编码就尊重该选择（解不出来
直接报错，不做二次猜测）；留空／用默认值 utf-8 时才自动退一步试本机编码
（Windows 上是 `GetOEMCP()` 对应的 cp936 一类，非 Windows 是 `locale` 偏好编码）。
两次都失败才最终退回 errors="replace" 兜底，保证工具不会因编码问题直接崩掉。
"""

import locale
import sys

if sys.platform == "win32":
    import ctypes
    SYSTEM_ENCODING = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
else:
    SYSTEM_ENCODING = locale.getpreferredencoding(False) or "utf-8"

_UTF8_ALIASES = ("utf8", "utf-8", "u8")


def decode_bytes(raw: bytes, encoding: str = "utf-8") -> tuple[str, str]:
    """解码字节串，返回 (文本, 实际生效的编码)。

    `encoding` 非 utf-8（调用方显式指定）时解码失败直接抛出，不做自动切换——
    那是调用方自己的选择，静默换编码只会把一个明确的错误变成另一个隐蔽的乱码。
    """
    is_default = encoding.lower() in _UTF8_ALIASES
    try:
        text = raw.decode(encoding)
        used = encoding
    except (UnicodeDecodeError, LookupError):
        if not is_default:
            raise
        try:
            text = raw.decode(SYSTEM_ENCODING)
            used = SYSTEM_ENCODING
        except (UnicodeDecodeError, LookupError):
            text = raw.decode(encoding, errors="replace")
            used = encoding
    # 手动解码不走文本模式的 universal newlines 翻译，这里补上，否则 \r\n 原样
    # 保留会让后续按行处理（readlines 风格）的结果和之前的文本模式读取不一致。
    return text.replace("\r\n", "\n").replace("\r", "\n"), used


def decode_subprocess_output(raw: bytes) -> str:
    """子进程 stdout/stderr 解码：现代 CLI（尤其继承了 PYTHONUTF8=1 的嵌套进程）
    大多输出 UTF-8，优先按 UTF-8 解；解不出来再退回系统代码页（Windows 上是
    OEM 代码页，`dir`/`type` 等 cmd 原生命令的真实输出编码）。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(SYSTEM_ENCODING, errors="replace")


def split_lines(text: str) -> list[str]:
    """按行切分并保留行尾的 \\n，语义对齐文本模式 `readlines()`，而不是
    `str.splitlines()`。

    二者不等价：`splitlines()` 还认 `\\x0b`(VT)、`\\x0c`(FF)、`\\u2028` 等一整套
    Unicode 行分隔符，碰到含分页符的老文件（C/汇编源码、打印排版文本常见）会
    把这些字符也当成行尾，切出比编辑器里看到的行号更多的行，导致
    `file_read` 的 start_line/end_line 与用户看到的行号错位。这里只认
    `decode_bytes` 已经归一过的 `\\n`。
    """
    if not text:
        return []
    parts = text.split("\n")
    trailing_newline = parts[-1] == ""
    lines = [p + "\n" for p in parts[:-1]]
    if not trailing_newline:
        lines.append(parts[-1])
    return lines
