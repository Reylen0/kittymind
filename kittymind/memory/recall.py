"""
记忆召回 —— 会话开始时选出与当前请求相关的记忆，注入 system prompt。

流程：LLM 选相关序号 → 失败时关键词 fallback → 加载完整内容 → 格式化注入。
使用同步 llm.invoke()，在 KittyAgent 中应通过 asyncio.to_thread() 调用。
"""

import json

from ..config import cfg


class MemoryRecall:
    """按需召回相关记忆，注入 system prompt 末尾。"""

    MAX_RELEVANT   = cfg.MEMORY_RECALL_MAX_RELEVANT
    MAX_BODY_CHARS = cfg.MEMORY_RECALL_MAX_BODY_CHARS

    def __init__(self, memory_store, llm):
        self._store = memory_store
        self._llm   = llm

    def select_relevant(self, user_input: str) -> list[dict]:
        """返回与 user_input 相关的记忆列表（最多 MAX_RELEVANT 条）。"""
        memories = self._store.all_memories()
        if not memories:
            return []
        catalog = self._store.catalog()
        indices = self._llm_select(user_input, catalog, len(memories))
        if indices is None:
            indices = self._keyword_select(user_input, memories)
        selected = [memories[i] for i in indices if 0 <= i < len(memories)]
        return selected[:self.MAX_RELEVANT]

    def build_recall_section(self, memories: list[dict]) -> str:
        """把召回的记忆格式化为可追加到 system prompt 的文本块。"""
        if not memories:
            return ""
        lines = [
            "\n\n## 相关背景记忆\n",
            "以下是与当前请求相关的历史记录，供参考。"
            "当前请求与记忆冲突时，以当前请求为准。\n",
        ]
        for m in memories:
            body = m.get("body", "")[:self.MAX_BODY_CHARS]
            lines.append(f"### [{m.get('type','?')}] {m.get('name','?')}\n{body}\n")
        return "\n".join(lines)

    # ── 内部实现 ──────────────────────────────────────────────────

    def _llm_select(self, user_input: str, catalog: str, total: int) -> list[int] | None:
        if not catalog:
            return []
        prompt = (
            f"以下是记忆目录（格式：序号. [类型] 名称: 描述）：\n\n{catalog}\n\n"
            f"用户当前请求：{user_input}\n\n"
            f"选出最相关的记忆序号（最多 {self.MAX_RELEVANT} 个）。"
            "只返回 JSON 整数数组，如 [0, 2]。没有相关的返回 []。不要输出其他内容。"
        )
        try:
            response = self._llm.invoke([{"role": "user", "content": prompt}])
            text = (response.content or "").strip()
            start, end = text.find("["), text.rfind("]") + 1
            if start == -1 or end == 0:
                return None
            indices = json.loads(text[start:end])
            if isinstance(indices, list):
                return [int(i) for i in indices if isinstance(i, (int, float))]
        except Exception:
            pass
        return None

    def _keyword_select(self, user_input: str, memories: list[dict]) -> list[int]:
        words = {w for w in user_input.lower().split() if len(w) > 2}
        if not words:
            return []
        scored = []
        for i, m in enumerate(memories):
            text = f"{m.get('name','')} {m.get('description','')} {m.get('body','')}".lower()
            score = sum(1 for w in words if w in text)
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [i for _, i in scored[:self.MAX_RELEVANT]]
