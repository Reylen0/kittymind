"""Embedding provider 接口。

只暴露「模型标识 / 维度 / 编码」三件事。**这里刻意不做「记忆后端」那种可插拔
抽象**（记忆存在哪、谁负责抽取与检索）——那与自研检索是替代关系而非叠加关系，
接进来等于把 Phase 17M 的计划整个作废（取舍见 ARCHITECTURE.md §5 Phase 17M）。

`model_id` / `dim` 不是描述性元数据，而是**硬校验依据**：不同模型（乃至同模型
不同 `dimensions`）产出的向量不可比，混用会静默给出垃圾相似度。索引库靠这两个
值判断是否需要全量重嵌。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class BaseEmbedding(Protocol):
    """文本 → 向量。实现必须满足「语义相近的文本，向量更接近」。"""

    @property
    def model_id(self) -> str:
        """向量来源模型标识；必须能唯一区分不同的向量空间。"""
        ...

    @property
    def dim(self) -> int:
        """向量维度；返回 0 表示尚未探测出实际维度（服务端默认维度）。"""
        ...

    def embed(self, texts: list[str]) -> Sequence[Sequence[float]]:
        """批量编码，返回顺序与入参一一对应。

        失败必须抛异常——**不要返回空占位**：静默的空向量会变成"召回到一条
        完全无关的记忆"这种最难排查的故障。
        """
        ...
