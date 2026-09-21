import uuid
from datetime import datetime, timezone
from pathlib import Path

from ._search_text import query_terms
from .store import SqliteSessionStore
from ..config import cfg

UTC = timezone.utc

# 命中词前后各保留多少字符作为上下文。侧栏只有 264px 宽，给太多也显示不下。
_SNIPPET_RADIUS = 40
_ELLIPSIS = "…"


def _merge_spans(spans: list[tuple[int, int]]) -> list[list[int]]:
    """合并重叠/相邻的高亮区间。

    多个查询词可能标到同一段文字上（搜「压缩 缩管」时区间会重叠），
    不合并的话前端按区间切分会切出交错的碎片。
    """
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _make_snippet(content: str | None, terms: list[str],
                  radius: int = _SNIPPET_RADIUS) -> dict:
    """截取命中词周围的上下文，并给出高亮区间（偏移相对**返回的片段**）。

    先单行化再定位：多行原文塞进侧栏那条窄列表项没有意义，而偏移量必须相对
    最终展示的那份文本来算，先截断后单行化会让高亮整体错位。
    """
    text = " ".join((content or "").split())
    if not text:
        return {"text": "", "marks": []}

    low = text.lower()
    spans: list[tuple[int, int]] = []
    for term in terms:
        needle = term.lower()
        pos = low.find(needle)
        while pos != -1:
            spans.append((pos, pos + len(term)))
            pos = low.find(needle, pos + 1)

    if not spans:
        # 索引走的是二元组模糊匹配，偶尔原文里凑不出完整查询串是正常的
        # （命中靠的是若干二元组同时存在）。这时给开头一段、不高亮，不算错误。
        head = text[:radius * 2]
        return {"text": head + (_ELLIPSIS if len(text) > len(head) else ""),
                "marks": []}

    merged = _merge_spans(spans)
    first = merged[0]
    begin = max(0, first[0] - radius)
    end   = min(len(text), first[1] + radius)

    prefix = _ELLIPSIS if begin > 0 else ""
    suffix = _ELLIPSIS if end < len(text) else ""
    shift  = begin - len(prefix)

    return {
        "text":  prefix + text[begin:end] + suffix,
        "marks": [[s - shift, e - shift] for s, e in merged
                  if s >= begin and e <= end],
    }


class SessionManager:
    def __init__(self, db_path: Path | None = None) -> None:
        self.store = SqliteSessionStore(db_path or cfg.SESSIONS_DB)

    def close(self) -> None:
        """进程收尾时关闭底层 SQLite 连接（幂等，见 SqliteSessionStore.close）。"""
        self.store.close()

    def create_session(self, title: str | None = None, session_id: str | None = None,
                       workspace_id: str | None = None) -> str:
        sid = session_id or str(uuid.uuid4())
        header: dict = {
            "version": 1, "id": sid,
            "title": title or "新对话",
            "created_at": datetime.now(UTC).isoformat(),
        }
        if workspace_id:
            header["workspace_id"] = workspace_id
        self.store.write_header(sid, header)
        return sid

    def session_exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def list_sessions(self) -> list[dict]:
        """会话概要列表。

        只查 sessions 表（list_headers），不加载任何消息——列表页只需要标题/时间。
        """
        return [
            {
                "id":           h["id"],
                "title":        h.get("title", ""),
                "created_at":   h.get("created_at", ""),
                "workspace_id": h.get("workspace_id"),
            }
            for h in self.store.list_headers()
        ]

    def get_session_header(self, session_id: str) -> dict | None:
        """只读 header（含派生 token 字段），不加载任何消息。

        给「只需要知道会话归属/标题」的调用方用（如 agent 解析工作目录）：这类调用
        每轮都会发生，不该顺带把整个会话的消息读出来。
        """
        header = self.store.read_header(session_id)
        return self._with_token_fields(header) if header else None

    def get_session(
        self, session_id: str, limit: int | None = None, before_seq: float | None = None
    ) -> dict | None:
        """会话详情。

        limit 缺省 → 全量消息（旧行为，供内部/审计使用）；
        limit 给定 → 只返回 seq < before_seq 的最新 limit 条，并附带 has_more / cursor，
        供前端「加载更早的消息」继续翻页（cursor 原样回传即可）。

        两种模式都返回展示视图（原始消息 + 活跃非摘要消息），摘要行不出现在这里。
        """
        header = self.store.read_header(session_id)
        if header is None:
            return None
        header = self._with_token_fields(header)

        if limit is None:
            return {"header": header, "messages": self.store.read_display(session_id)}

        page = self.store.read_display_page(session_id, limit, before_seq)
        return {
            "header":   header,
            "messages": page["messages"],
            "has_more": page["has_more"],
            "cursor":   page["cursor"],
        }

    @staticmethod
    def _with_token_fields(header: dict) -> dict:
        """把持久化的 used 和按当前配置现算的 total 派进 header。"""
        header["used_tokens"]  = header.get("last_prompt_tokens") or 0
        header["total_tokens"] = max(
            1, cfg.LLM_CONTEXT_WINDOW - cfg.LLM_RESERVED_OUTPUT_TOKENS
        )
        return header


    def delete_session(self, session_id: str) -> bool:
        return self.store.delete(session_id)

    def search_messages(self, query: str, session_id: str | None = None,
                        limit: int = 50) -> list[dict]:
        """全文搜索，结果按会话分组。

        形状：`[{session_id, title, hits: [{seq, role, text, marks}]}]`
        —— `text` 是命中词周围的片段，`marks` 是片段内的高亮区间 [[起, 止]]。

        分组用 dict 累积，天然保留 store 给出的 bm25 相关度序：最相关的那条
        命中所在的会话排在最前面。
        """
        terms = query_terms(query)
        groups: dict[str, dict] = {}
        for hit in self.store.search(query, session_id=session_id, limit=limit):
            group = groups.get(hit["session_id"])
            if group is None:
                group = {"session_id": hit["session_id"],
                         "title": hit["title"], "hits": []}
                groups[hit["session_id"]] = group
            snippet = _make_snippet(hit["content"], terms)
            group["hits"].append({
                "seq":   hit["seq"],
                "role":  hit["role"],
                "text":  snippet["text"],
                "marks": snippet["marks"],
            })
        return list(groups.values())

    def append_turn(self, session_id: str, turn_messages: list[dict],
                    first_input: str | None = None,
                    workspace_id: str | None = None) -> None:
        if not self.store.exists(session_id):
            self.create_session(title=self.generate_title(first_input or ""),
                                session_id=session_id, workspace_id=workspace_id)
        seq = self.store.next_seq(session_id)
        for msg in turn_messages:
            record: dict = {"seq": seq, "role": msg["role"], "content": msg.get("content")}
            if msg.get("tool_calls"):
                record["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                record["tool_call_id"] = msg["tool_call_id"]
            self.store.append(session_id, record)
            seq += 1

    def load_messages(self, session_id: str) -> list[dict]:
        """返回带 seq 的活跃消息列表（供 agent 打 _seq 标记）。"""
        _, records = self.store.read(session_id)
        return records  # 每条已含 seq 字段

    def load_history(self, session_id: str) -> list[dict]:
        """返回不含 seq 的消息列表（兼容外部调用方）。"""
        _, records = self.store.read(session_id)
        result = []
        for r in records:
            msg: dict = {"role": r["role"], "content": r.get("content")}
            if r.get("tool_calls"):
                msg["tool_calls"] = r["tool_calls"]
            if r.get("tool_call_id"):
                msg["tool_call_id"] = r["tool_call_id"]
            result.append(msg)
        return result

    def load_full_history(self, session_id: str) -> list[dict]:
        """完整视图（active + compacted），审计/回溯用。"""
        return self.store.read_full(session_id)

    def archive_and_compact(
        self,
        session_id: str,
        compacted_seqs: set,
        summary_rows: list[dict],
        new_active_msgs: list[dict],
    ) -> None:
        """原子压缩落库，透传 store。"""
        self.store.archive_and_compact(
            session_id, compacted_seqs, summary_rows, new_active_msgs
        )

    def get_session_state(self, session_id: str) -> dict:
        """读取会话的压缩状态（compressed_once / last_prompt_tokens）。"""
        return self.store.get_state(session_id)

    def save_session_state(self, session_id: str, **fields) -> None:
        """持久化会话的压缩状态。"""
        self.store.save_state(session_id, **fields)

    def generate_title(self, text: str) -> str:
        text = text.strip()
        return text[:20] + ("..." if len(text) > 20 else "")

    # ── 用量成本追踪（Phase 16）─────────────────────────────────

    def record_usage(self, session_id: str | None, model_id: str,
                     prompt_tokens: int, completion_tokens: int,
                     n_calls: int = 1, kind: str = "turn") -> None:
        self.store.record_usage(
            session_id, model_id, prompt_tokens, completion_tokens, n_calls, kind
        )

    def aggregate_usage(self, group_by: str = "model",
                        since: int | None = None, until: int | None = None) -> list[dict]:
        return self.store.aggregate_usage(group_by, since, until)

    def session_usage(self, session_id: str) -> dict:
        return self.store.session_usage(session_id)
