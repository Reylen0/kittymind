"""微压缩：无 LLM、就地截断超长 tool 结果。

优先于主压缩执行——有时仅靠截断工具输出就能腾出足够空间，避免一次昂贵的摘要调用。
"""

from ..config import cfg

_HEAD_LINES = 20
_TAIL_LINES = 20


def prune_tool_outputs(
    messages: list[dict],
    keep_recent: int = 3,
    max_chars: int | None = None,
) -> list[dict]:
    """截断较旧的超长 tool 结果，保留最近 keep_recent 条不动。

    就地修改内容字符串，不改消息条数/角色，不破坏 tool_call 配对。
    返回新列表（浅拷贝，只有被修改的消息是新对象）。
    """
    limit = max_chars if max_chars is not None else cfg.COMPRESS_MICRO_TOOL_CHARS

    # 定位所有 tool 消息索引
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    # 最近 keep_recent 条保留原样
    protect = set(tool_indices[-keep_recent:]) if keep_recent > 0 else set()

    result = list(messages)
    for i in tool_indices:
        if i in protect:
            continue
        msg = result[i]
        content = msg.get("content") or ""
        if len(content) <= limit:
            continue
        lines = content.splitlines()
        if len(lines) <= _HEAD_LINES + _TAIL_LINES:
            continue
        head = "\n".join(lines[:_HEAD_LINES])
        tail = "\n".join(lines[-_TAIL_LINES:])
        omitted = len(content) - len(head) - len(tail)
        trimmed = f"{head}\n[...已截断 {omitted} 字符...]\n{tail}"
        result[i] = {**msg, "content": trimmed}

    return result
