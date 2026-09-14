import uuid
from datetime import datetime, timezone
from pathlib import Path

from .store import SqliteSessionStore
from ..config import cfg

UTC = timezone.utc


class SessionManager:
    def __init__(self, db_path: Path | None = None) -> None:
        self.store = SqliteSessionStore(db_path or cfg.SESSIONS_DB)

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
        result = []
        for sid in self.store.list_ids():
            header, _ = self.store.read(sid)
            if header:
                result.append({
                    "id":           header["id"],
                    "title":        header.get("title", ""),
                    "created_at":   header.get("created_at", ""),
                    "workspace_id": header.get("workspace_id"),
                })
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def get_session(self, session_id: str) -> dict | None:
        header, _ = self.store.read(session_id)
        if header is None:
            return None
        # 前端展示用原始消息（compacted）+ 活跃非摘要消息，不含压缩摘要行
        display_records = self.store.read_display(session_id)
        header["used_tokens"]  = header.get("last_prompt_tokens") or 0
        header["total_tokens"] = max(
            1, cfg.LLM_CONTEXT_WINDOW - cfg.LLM_RESERVED_OUTPUT_TOKENS
        )
        return {"header": header, "messages": display_records}

    def delete_session(self, session_id: str) -> None:
        self.store.delete(session_id)

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
