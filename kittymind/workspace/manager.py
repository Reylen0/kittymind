import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


class WorkspaceManager:
    """工作区持久化管理。

    存储位置：~/.kittymind/workspaces.json
    格式：[{ id, name, path, created_at }, ...]
    """

    DEFAULT_PATH = Path.home() / ".kittymind" / "workspaces.json"

    def __init__(self, file_path: Path | None = None) -> None:
        self._path = file_path or self.DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, workspaces: list[dict]) -> None:
        self._path.write_text(
            json.dumps(workspaces, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_workspaces(self) -> list[dict]:
        return self._load()

    def get_workspace(self, workspace_id: str) -> dict | None:
        for ws in self._load():
            if ws.get("id") == workspace_id:
                return ws
        return None

    def create_workspace(self, name: str, path: str) -> dict:
        workspaces = self._load()
        ws = {
            "id":         str(uuid.uuid4()),
            "name":       name,
            "path":       path,
            "created_at": datetime.now(UTC).isoformat(),
        }
        workspaces.append(ws)
        self._save(workspaces)
        return ws

    def delete_workspace(self, workspace_id: str) -> bool:
        workspaces = self._load()
        new_list = [w for w in workspaces if w.get("id") != workspace_id]
        if len(new_list) == len(workspaces):
            return False
        self._save(new_list)
        return True
