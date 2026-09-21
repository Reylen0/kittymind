"""UsageRecorder：按 turn 聚合 LLM 用量。

一个 ReAct turn 可能产生多次 LLM 调用（每步工具后重进循环），`usage` 事件会
来多次。这里把每次的真值（prompt_tokens / completion_tokens）累加起来，供
`_commit_turn` 落库时一次性写入 model_usage。

子 Agent 也各自持有一个 recorder（经 session_id=None 的 async_stream_run），
其聚合结果通过委派机制回传父级，父级合并计入同一会话。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageRecorder:
    """一次 run（一个 turn）内按模型聚合的 token 用量。

    `model_id` 缺省为 None，表示「尚未观察到任何 usage 事件」；一旦 record()
    被调用，就用事件携带的模型标识回填。一个 turn 理论上只用一个主模型，但
    recorder 按 model_id 分组，天然兼容「同 turn 内切模型」的边角情况。
    """

    model_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    n_calls: int = 0
    _by_model: dict[str, dict] = field(default_factory=dict)

    def record(self, usage: dict, model_id: str | None = None) -> None:
        """累加一次真实 usage。usage 字段缺失时按 0 处理，不抛异常。"""
        pt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        ct = usage.get("completion_tokens") if isinstance(usage, dict) else None
        p = int(pt) if pt is not None else 0
        c = int(ct) if ct is not None else 0

        key = model_id or "unknown"
        self.model_id = key
        self.prompt_tokens += p
        self.completion_tokens += c
        self.n_calls += 1

        bucket = self._by_model.setdefault(
            key, {"prompt_tokens": 0, "completion_tokens": 0, "n_calls": 0}
        )
        bucket["prompt_tokens"] += p
        bucket["completion_tokens"] += c
        bucket["n_calls"] += 1

    def merge(self, other: UsageRecorder) -> None:
        """把子 Agent 的用量并入本 recorder（父级累计）。"""
        for key, b in other._by_model.items():
            self.model_id = key
            self.prompt_tokens += b["prompt_tokens"]
            self.completion_tokens += b["completion_tokens"]
            self.n_calls += b["n_calls"]
            bucket = self._by_model.setdefault(
                key, {"prompt_tokens": 0, "completion_tokens": 0, "n_calls": 0}
            )
            bucket["prompt_tokens"] += b["prompt_tokens"]
            bucket["completion_tokens"] += b["completion_tokens"]
            bucket["n_calls"] += b["n_calls"]

    @property
    def is_empty(self) -> bool:
        return self.n_calls == 0

    def rows(self) -> list[dict]:
        """按模型展开为可落库的行列表（含 n_calls）。"""
        return [
            {
                "model_id": key,
                "prompt_tokens": b["prompt_tokens"],
                "completion_tokens": b["completion_tokens"],
                "n_calls": b["n_calls"],
            }
            for key, b in self._by_model.items()
        ]
