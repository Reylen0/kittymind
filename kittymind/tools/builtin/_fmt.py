"""工具错误文案里的尺寸格式化。

存在的理由：原先错误文案把限制值硬编码成字面量（"内容超过 1MB 限制"、
"文件超过 2MB"、"800KB"），而真实上限来自 `cfg.FILE_WRITE_MAX_BYTES` 等配置。
配置一改文案就开始说谎——模型据此判断"还能写多少"会得到错误结论。
统一按实际配置值渲染，改配置就不需要改文案。
"""


def human_size(num_bytes: float) -> str:
    """把字节数渲染成 B / KB / MB（1000 进制，与 config 里 1_000_000 的写法一致）。"""
    if num_bytes >= 1_000_000:
        return f"{num_bytes / 1_000_000:.1f}MB"
    if num_bytes >= 1000:
        return f"{num_bytes / 1000:.0f}KB"
    return f"{num_bytes:.0f}B"
