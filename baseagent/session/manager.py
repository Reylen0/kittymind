"""会话生命周期管理。

负责：创建/查询/删除会话、追加消息、加载历史、自动生成标题。
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from .store import SessionStore

UTC = timezone.utc


class SessionManager:
    DEFAULT_DIR = Path.home() / ".kittymind" / "sessions"

    def __init__(self, data_dir: Path | None = None) -> None:
        self.store = SessionStore(data_dir or self.DEFAULT_DIR)

    # ──────────────────────────────────────────────────────────────
    # CRUD
    # ──────────────────────────────────────────────────────────────

    def create_session(self, title: str | None = None, session_id: str | None = None) -> str:
        """创建新会话，返回 session_id。"""
        sid = session_id or str(uuid.uuid4())
        header = {
            "version": 1,
            "id": sid,
            "title": title or "新会话",
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.store.write_header(sid, header)
        return sid

    def session_exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def list_sessions(self) -> list[dict]:
        """返回所有会话摘要列表，按创建时间倒序。"""
        result = []
        for sid in self.store.list_ids():
            header, _ = self.store.read(sid)
            if header:
                result.append({
                    "id": header["id"],
                    "title": header.get("title", ""),
                    "created_at": header.get("created_at", ""),
                })
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def get_session(self, session_id: str) -> dict | None:
        """返回完整会话（header + messages），不存在则返回 None。"""
        header, records = self.store.read(session_id)
        if header is None:
            return None
        return {"header": header, "messages": records}

    def delete_session(self, session_id: str) -> None:
        self.store.delete(session_id)

    # ──────────────────────────────────────────────────────────────
    # 消息持久化
    # ──────────────────────────────────────────────────────────────

    def append_turn(
        self,
        session_id: str,
        turn_messages: list[dict],
        first_input: str | None = None,
    ) -> None:
        """追加本轮消息到 JSONL。

        turn_messages 是仅本轮新增的消息（user + 中间工具调用 + final assistant），
        不含历史。session 不存在时自动创建并用 first_input 生成标题。
        """
        if not self.store.exists(session_id):
            title = self.generate_title(first_input or "")
            self.create_session(title=title, session_id=session_id)

        _, existing = self.store.read(session_id)
        seq = len(existing)

        for msg in turn_messages:
            record: dict = {
                "seq": seq,
                "role": msg["role"],
                "content": msg.get("content"),
            }
            if msg.get("tool_calls"):
                record["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                record["tool_call_id"] = msg["tool_call_id"]
            self.store.append(session_id, record)
            seq += 1

    # ──────────────────────────────────────────────────────────────
    # 历史加载
    # ──────────────────────────────────────────────────────────────

    def load_history(self, session_id: str) -> list[dict]:
        """从 JSONL 读取消息记录，返回去除 seq 字段的 dict 列表（可直接传给 LLM）。"""
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

    # ──────────────────────────────────────────────────────────────
    # 工具
    # ──────────────────────────────────────────────────────────────

    def generate_title(self, text: str) -> str:
        text = text.strip()
        return text[:20] + ("..." if len(text) > 20 else "")
