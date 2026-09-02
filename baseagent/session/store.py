"""JSONL 会话文件 I/O 层。

每个会话对应一个目录：
  {data_dir}/{session_id}/session.jsonl

文件格式：
  第 1 行 — header JSON（id、title、created_at、version）
  后续行 — 消息 JSON（seq、role、content、[tool_calls]、[tool_call_id]）

崩溃恢复：读时遇到不完整行（JSONDecodeError）即 break，跳过 torn tail。
"""

import json
import shutil
from pathlib import Path


class SessionStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def _path(self, session_id: str) -> Path:
        return self.data_dir / session_id / "session.jsonl"

    def write_header(self, session_id: str, header: dict) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")

    def append(self, session_id: str, record: dict) -> None:
        with self._path(session_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read(self, session_id: str) -> tuple[dict | None, list[dict]]:
        """读取会话，返回 (header, records)。torn tail 时截断，不报错。"""
        path = self._path(session_id)
        if not path.exists():
            return None, []

        header: dict | None = None
        records: list[dict] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    break  # torn tail — 截断
                if header is None:
                    header = data
                else:
                    records.append(data)
        return header, records

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def delete(self, session_id: str) -> None:
        path = self._path(session_id).parent
        if path.exists():
            shutil.rmtree(path)

    def list_ids(self) -> list[str]:
        if not self.data_dir.exists():
            return []
        return [
            d.name for d in self.data_dir.iterdir()
            if d.is_dir() and (d / "session.jsonl").exists()
        ]
