"""记忆召回 —— 会话开始时选出与当前请求相关的记忆，注入 system prompt。

Phase 17M 起有四条路径，由 `MEMORY_RECALL_MODE` 选择：

```
auto（默认）：向量 + 关键词融合 ──(无 key / 端点报错 / 超时 / 索引为空)──>
              改造前的 LLM 选序号 ──(解析失败)──> 2-gram 关键词
vector      ：只用向量 + 关键词融合（不调 LLM）；向量不可用时退回 2-gram
llm         ：强制走改造前的 LLM 选序号路径
keyword     ：只用 2-gram 关键词（**零外发的隐私模式**）
```

**旧路径不是被删掉，而是降级链的末端**：没配 embedding 的用户行为一字不变，
这也让「回滚对照」测试能拿今天的行为当基准（`auto` + 无 key 必须返回与改造前
逐字节一致的结果）。

为什么是融合而不是替换：2-gram 对专名 / 代码标识符 / 版本号天生比向量强
（`sqlite-vec`、`MEMORY_RECALL_TIMEOUT` 这类串在向量空间里几乎没有区分度），
而向量强在"描述没写到的同义表达"。两者互补。

使用同步 API（`llm.invoke()` / 网络调用），在 KittyAgent 中经 `asyncio.to_thread()`
调用；超时兜底由调用方做（见 `kitty_agent._compute_recall_section`）。
"""

import json
import logging
import re
import statistics
import time

from ..config import cfg
from ..core.llm_json import extract_json_text, log_parse_failure
from ..prompts import MEMORY_RECALL_SECTION_HEADER, build_memory_recall_select_prompt
from .index import get_memory_index
from .ledger import get_memory_ledger

logger = logging.getLogger(__name__)

_MODES = frozenset({"auto", "vector", "llm", "keyword"})


class MemoryRecall:
    """按需召回相关记忆，注入 system prompt 末尾。

    阈值类配置走 property 而非类属性：类属性在 import 期求值，会把 cfg 的值
    钉死——设置面板保存后调 cfg.reload() 也改不动。
    """

    @property
    def MAX_RELEVANT(self) -> int:
        return cfg.MEMORY_RECALL_MAX_RELEVANT

    @property
    def MAX_BODY_CHARS(self) -> int:
        return cfg.MEMORY_RECALL_MAX_BODY_CHARS

    @property
    def MODE(self) -> str:
        mode = (cfg.MEMORY_RECALL_MODE or "auto").strip().lower()
        if mode not in _MODES:
            logger.debug("未知 MEMORY_RECALL_MODE=%r，按 auto 处理", mode)
            return "auto"
        return mode

    def __init__(self, memory_store, llm, index_getter=None):
        self._store = memory_store
        self._llm = llm
        # 延迟到调用时取索引单例：配置热更新（换模型/换维度）后能拿到新的实例。
        # 索引库路径由记忆目录派生，测试的临时 store 天然隔离。
        self._get_index = index_getter or self._default_index
        # 本轮召回的留痕素材（候选集/间距/降级原因），由 select_relevant 写出。
        self._trace: dict = {}
        self._session: tuple[str | None, int] = (None, 0)

    def _default_index(self):
        return get_memory_index(self._store.directory)

    def bind_session(self, session_id: str | None, turn_seq: int = 0) -> None:
        """绑定本轮的会话上下文，供召回留痕使用。

        刻意做成"先绑定、再调用"，而不是给 `select_relevant` 加参数：召回对象是鸭子
        类型接口（测试替身只实现 select_relevant / build_recall_section），给热路径加
        关键字参数会让签名不匹配的替身抛 TypeError，而那个异常会被上层当成"召回失败"
        静默吞掉——记忆召回会**无声失效**。绑定是可选钩子，缺了只是这一轮没留痕。
        """
        self._session = (session_id, int(turn_seq or 0))

    def select_relevant(self, user_input: str) -> list[dict]:
        """返回与 user_input 相关的记忆（最多 MAX_RELEVANT 条），并写一行召回留痕。

        留痕是 P0 的一部分：**没有"这轮注入了哪几条"的记录，门控阈值与重排参数的
        任何调整都无法评估**，只能靠感觉。
        """
        started = time.perf_counter()
        self._trace = {"mode": self.MODE, "candidates": [], "margin": 0.0,
                       "gate_blocked": []}
        result = self._select_relevant(user_input)
        self._log_recall(user_input, result, started)
        return result

    def _select_relevant(self, user_input: str) -> list[dict]:
        memories = self._store.all_memories()
        if not memories:
            return []

        mode = self.MODE
        if mode == "keyword":
            self._trace["mode"] = "keyword"
            return self._keyword_only(user_input, memories)
        if mode == "llm":
            self._trace["mode"] = "llm"
            return self._llm_then_keyword(user_input, memories)

        fused = self._fused_select(user_input, memories)
        if fused is not None:
            return fused
        if mode == "vector":
            self._trace["mode"] = "vector→keyword"
            return self._keyword_only(user_input, memories)
        self._trace["mode"] = "auto→fallback"
        return self._llm_then_keyword(user_input, memories)

    def select_relevant_keyword(self, user_input: str) -> list[dict]:
        """纯关键词召回 —— 召回超时时的兜底（完全不碰网络）。同样留痕。"""
        started = time.perf_counter()
        self._trace = {"mode": "keyword-fallback", "candidates": [], "margin": 0.0,
                       "gate_blocked": []}
        memories = self._store.all_memories()
        result = self._keyword_only(user_input, memories) if memories else []
        self._log_recall(user_input, result, started)
        return result

    def _log_recall(self, user_input: str, result: list[dict], started: float) -> None:
        """写召回留痕。**失败只 debug**——留痕绝不能影响一轮对话。"""
        try:
            store = self._store
            if store is None:
                return
            ledger = get_memory_ledger(store.directory)
            if ledger is None:
                return
            trace = self._trace or {}
            session_id, turn_seq = self._session
            ledger.log_recall(
                session_id=session_id,
                mode=str(trace.get("mode") or self.MODE),
                query_text=user_input,
                candidates=trace.get("candidates") or [],
                injected=[str(m.get("name") or "") for m in result],
                gate_blocked=trace.get("gate_blocked") or [],
                margin=float(trace.get("margin") or 0.0),
                latency_ms=int((time.perf_counter() - started) * 1000),
                turn_seq=turn_seq,
            )
        except Exception as e:
            logger.debug("召回留痕失败：%s: %s", type(e).__name__, e)

    def build_recall_section(self, memories: list[dict]) -> str:
        """把召回的记忆格式化为可追加到 system prompt 的文本块。"""
        if not memories:
            return ""
        lines = [MEMORY_RECALL_SECTION_HEADER]
        for m in memories:
            body = str(m.get("body", ""))[:self.MAX_BODY_CHARS]
            lines.append(f"### [{m.get('type','?')}] {m.get('name','?')}\n{body}\n")
        return "\n".join(lines)

    # ── 向量 + 关键词融合 ─────────────────────────────────────────

    def _fused_select(self, user_input: str, memories: list[dict]) -> list[dict] | None:
        """返回融合排序结果；向量路不可用时返回 None（交给降级链）。"""
        try:
            index = self._get_index()
        except Exception as e:
            logger.warning("获取记忆索引失败：%s: %s", type(e).__name__, e)
            return None
        if index is None:
            return None

        try:
            index.sync(memories)   # 懒对账：首次补齐，之后只 stat 比对
            sims = index.scores(user_input, cfg.MEMORY_VECTOR_TOP_K)
        except Exception as e:
            logger.warning("记忆向量检索失败（本轮退回旧路径）：%s: %s", type(e).__name__, e)
            return None
        if not sims:
            return None

        by_name = {m["name"]: m for m in memories if m.get("name")}
        weight = cfg.MEMORY_RECALL_KEYWORD_WEIGHT
        keyword_scores = self._keyword_scores(user_input, memories)
        ranked = [
            (score + weight * keyword_scores.get(name, 0.0), name)
            for name, score in sims.items()
            if name in by_name
        ]
        if not ranked:
            return None

        # 门控只看**向量分**（与实测口径一致：top1 0.348~0.564，不相关项 median
        # 0.219~0.437，两区间重叠——所以任何绝对阈值换模型即失效，默认全关）。
        # 留痕先于门控：即使本轮被拦下，也要留下"本来会注入什么"的记录。
        values = sorted(sims.values(), reverse=True)
        self._trace["candidates"] = [
            (name, score)
            for score, name in sorted(ranked, key=lambda t: -t[0])[:cfg.MEMORY_VECTOR_TOP_K]
        ]
        self._trace["margin"] = (values[0] - statistics.median(values)) if values else 0.0
        if self._rejected_by_gate(sims):
            return []

        # tie-break 用 updated_at 降序（记忆是长期事实，"旧"不等于"不相关"，
        # 刻意不做时间衰减）。两趟稳定排序：先次键后主键。
        ranked.sort(key=lambda t: str(by_name[t[1]].get("updated_at") or ""), reverse=True)
        ranked.sort(key=lambda t: -t[0])
        return [by_name[name] for _score, name in ranked[:self.MAX_RELEVANT]]

    @staticmethod
    def _rejected_by_gate(sims: dict[str, float]) -> bool:
        min_score = cfg.MEMORY_RECALL_MIN_SCORE
        min_margin = cfg.MEMORY_RECALL_MIN_MARGIN
        if min_score <= 0 and min_margin <= 0:
            return False
        values = sorted(sims.values(), reverse=True)
        top1 = values[0]
        if min_score > 0 and top1 < min_score:
            return True
        return min_margin > 0 and (top1 - statistics.median(values)) < min_margin

    # ── 降级链：改造前的两条路径（行为保持不变，回滚对照的基准）────

    def _llm_then_keyword(self, user_input: str, memories: list[dict]) -> list[dict]:
        """今天的默认行为：LLM 选序号 → 失败时 2-gram 关键词。"""
        indices = self._llm_select(user_input, self._store.catalog(), len(memories))
        if indices is None:
            indices = self._keyword_select(user_input, memories)
        selected = [memories[i] for i in indices if 0 <= i < len(memories)]
        return selected[:self.MAX_RELEVANT]

    def _keyword_only(self, user_input: str, memories: list[dict]) -> list[dict]:
        indices = self._keyword_select(user_input, memories)
        return [memories[i] for i in indices if 0 <= i < len(memories)][:self.MAX_RELEVANT]

    def _llm_select(self, user_input: str, catalog: str, total: int) -> list[int] | None:
        if not catalog:
            return []
        prompt = build_memory_recall_select_prompt(catalog, user_input, self.MAX_RELEVANT)
        text = ""
        try:
            response = self._llm.invoke([{"role": "user", "content": prompt}])
            text = (response.content or "").strip()
            payload = extract_json_text(text)
            if payload is None:
                log_parse_failure("记忆召回选择", text, "输出里找不到 JSON 数组")
                return None
            indices = json.loads(payload)
            if isinstance(indices, list):
                return [int(i) for i in indices if isinstance(i, (int, float))]
            log_parse_failure("记忆召回选择", text, "输出不是数组")
        except Exception as e:
            # 记下来再降级到关键词匹配：静默吞掉会让「召回长期为空」无从排查
            log_parse_failure("记忆召回选择", text, f"{type(e).__name__}: {e}")
        return None

    # 拉丁词（≥3 字符整词）；CJK 连续段（中文无空格，须另切 2-gram，见 _extract_keywords）
    _TOKEN_RE = re.compile(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]+")

    def _extract_keywords(self, text: str) -> set[str]:
        """从输入里提取可用于匹配的关键词。

        旧实现 `text.split()` 按**空格**切词，中文整句变成一个 token 且
        `len(w) > 2` 过滤后直接为空——LLM 选择失败时中文召回必然为空。
        现在：拉丁词整词保留；CJK 连续段切 2-gram（单字噪声太大，
        2-gram 与中文词的粒度最接近）。
        """
        words: set[str] = set()
        for m in self._TOKEN_RE.finditer(text.lower()):
            tok = m.group()
            if tok[0].isascii():
                words.add(tok)
            elif len(tok) == 1:
                words.add(tok)  # 用户只敲了一个汉字：只能拿它匹配
            else:
                words.update(tok[i:i + 2] for i in range(len(tok) - 1))
        return words

    @staticmethod
    def _match_text(memory: dict) -> str:
        return (
            f"{memory.get('name','')} {memory.get('description','')} "
            f"{memory.get('body','')}"
        ).lower()

    def _keyword_select(self, user_input: str, memories: list[dict]) -> list[int]:
        words = self._extract_keywords(user_input)
        if not words:
            return []
        scored = []
        for i, m in enumerate(memories):
            score = sum(1 for w in words if w in self._match_text(m))
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [i for _, i in scored[:self.MAX_RELEVANT]]

    def _keyword_scores(self, user_input: str, memories: list[dict]) -> dict[str, float]:
        """关键词分的**归一化**版本（命中数 / query 词数），供融合排序用。

        与 `_keyword_select` 的原始计数刻意分开：那条路径是降级链末端，行为必须
        逐字节可复现；这条只在向量可用时参与加权。
        """
        words = self._extract_keywords(user_input)
        if not words:
            return {}
        total = len(words)
        scores: dict[str, float] = {}
        for m in memories:
            name = str(m.get("name") or "")
            if not name:
                continue
            hits = sum(1 for w in words if w in self._match_text(m))
            if hits:
                scores[name] = hits / total
        return scores
