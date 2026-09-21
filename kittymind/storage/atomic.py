"""原子写文本文件：同目录临时文件 + `os.replace`。

为什么需要它：直接 `path.write_text(...)` 写到一半时进程退出（或断电），会留下
**被截断的半截文件**。对记忆文件（用户的长期资料）和审计 JSONL（排查凭证）来说，
"半截"比"没写"更糟——读回时是能解析但不完整的内容，信息静默丢失。

两条实现上的硬约束：

  1. **必须与目标同目录**：`os.replace` 只在同一文件系统内保证原子性；跨盘会
     退化成复制 + 删除，既没有原子性，还更容易留下半截文件。
  2. **临时名带 pid**：避免多进程（或并行测试）互踩同一个 `.tmp`。

会话库/审计库不需要这个：它们的原子性由 SQLite 事务自己保证。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """把 content 原子地写入 path（父目录不存在时自动创建）。

    失败时清理临时文件并原样抛出——调用方需要知道"这次写没成功"。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
