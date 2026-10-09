"""OpenAI 兼容端点的 embedding 客户端（智谱 / OpenAI / 各类兼容网关通用）。

三个实现要点都是踩坑换来的：

  - **按批切分**：`input` 数组上限各家不同（OpenAI 2048、智谱实测 16 条稳定、
    部分自建网关更小），统一按 `EMBEDDING_BATCH` 切批，把"偶发超限"变成
    可预测行为。
  - **dimensions 降级探测**：`EMBEDDING_DIM` 只对支持该参数的服务端有意义。
    不支持时首次调用会 400（或静默忽略）。这里在第一次失败后退回不带
    `dimensions` 重试一次，并从返回值长度**学习实际维度**——否则用户换个模型
    整条向量路就静默失效，而失败点在后台线程里，没人会看见。
  - **自己归一化**：检索依赖单位向量（余弦退化为点积）。智谱实测返回即
    norm=1.0，但不能指望每家都如此，统一再归一化一次。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np

logger = logging.getLogger(__name__)

_VECTOR_DTYPE = np.float32


class OpenAICompatEmbedding:
    """openai SDK 同步客户端 + 手动归一化（调用方在 `to_thread` 里用）。"""

    def __init__(
        self,
        model_id: str,
        api_key: str,
        base_url: str,
        *,
        dim: int = 0,
        batch: int = 16,
        timeout: int = 30,
    ) -> None:
        # 懒导入：没配 embedding 的用户不该为这份 import 付启动时间
        from openai import OpenAI

        self._model = model_id
        self._batch = max(1, int(batch or 1))
        self._requested_dim = max(0, int(dim or 0))
        self._probed_dim = 0                      # 首次成功调用后学习的真实维度
        self._send_dim = self._requested_dim > 0  # 是否带 dimensions 参数
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._probed_dim or self._requested_dim

    def embed(self, texts: list[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        out: list[np.ndarray] = []
        for i in range(0, len(texts), self._batch):
            out.extend(self._embed_chunk(texts[i:i + self._batch]))
        return out

    # ── 内部 ────────────────────────────────────────────────────────

    def _embed_chunk(self, chunk: list[str]) -> list[np.ndarray]:
        try:
            resp = self._call(chunk, with_dim=self._send_dim)
        except Exception:
            if not self._send_dim:
                raise
            # 只降级一次：把「服务端不支持 dimensions」变成「用服务端默认维度」，
            # 而不是让整条向量路永久失效。
            self._send_dim = False
            logger.warning(
                "embedding 端点不接受 dimensions=%d，已改用服务端默认维度",
                self._requested_dim,
            )
            resp = self._call(chunk, with_dim=False)

        vectors = [np.asarray(d.embedding, dtype=_VECTOR_DTYPE) for d in resp.data]
        if vectors:
            # 每次都用实际返回值刷新（不是"只记第一次"）：降级到服务端默认维度后
            # 维度会变，只记第一次会让 dim 永远停在请求值上——而 dim 是索引对账的
            # 硬校验依据，错了会让整表向量与新向量混用，静默产出垃圾相似度。
            self._probed_dim = int(vectors[0].size)
        return [self._normalize(v) for v in vectors]

    def _call(self, chunk: list[str], *, with_dim: bool):
        kwargs: dict = {}
        if with_dim and self._requested_dim > 0:
            kwargs["dimensions"] = self._requested_dim
        return self._client.embeddings.create(model=self._model, input=chunk, **kwargs)

    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray:
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec
