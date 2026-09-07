import uuid
from datetime import datetime, timezone
from pathlib import Path

from .store import SessionStore
from ..config import cfg

UTC = timezone.utc


class SessionManager:
    DEFAULT_DIR = cfg.SESSIONS_DIR

    def __init__(self, data_dir: Path | None = None) -> None:
        self.store = SessionStore(data_dir or self.DEFAULT_DIR)

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
        header, records = self.store.read(session_id)
        if header is None:
            return None
        return {"header": header, "messages": records}

    def delete_session(self, session_id: str) -> None:
        self.store.delete(session_id)

    def append_turn(self, session_id: str, turn_messages: list[dict],
                    first_input: str | None = None,
                    workspace_id: str | None = None) -> None:
        if not self.store.exists(session_id):
            self.create_session(title=self.generate_title(first_input or ""),
                                session_id=session_id, workspace_id=workspace_id)
        _, existing = self.store.read(session_id)
        seq = len(existing)
        for msg in turn_messages:
            record: dict = {"seq": seq, "role": msg["role"], "content": msg.get("content")}
            if msg.get("tool_calls"):
                record["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                record["tool_call_id"] = msg["tool_call_id"]
            self.store.append(session_id, record)
            seq += 1

    def load_history(self, session_id: str) -> list[dict]:
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

    def generate_title(self, text: str) -> str:
        text = text.strip()
        return text[:20] + ("..." if len(text) > 20 else "")
