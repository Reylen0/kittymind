from dataclasses import dataclass, field


@dataclass
class StreamEvent:
    """stream_with_tools() 产生的流式事件。

    type:
      'text_delta'      — 文字片段
      'tool_calls_done' — 流结束时的完整工具调用列表
      'usage'           — 本次调用真实 token 用量（OpenAI usage 字段）
    """
    type: str
    delta: str = ''
    tool_calls: list = field(default_factory=list)
    usage: dict | None = None


@dataclass
class LLMResponse:
    """非流式调用的返回（与 StreamEvent 同为 dataclass，职责一致）。

    构造点全部用关键字参数且 tool_calls 恒传 list（llm_adapters.py 两处），
    不会踩 dataclass 不做 `or []` 归一的坑。
    """
    content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)

    def is_tool_call(self) -> bool:
        return len(self.tool_calls) > 0
