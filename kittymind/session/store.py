import json
import shutil
from pathlib import Path


class SessionStore:
    """JSONL 会话文件 I/O 层。

    每个会话：{data_dir}/{session_id}/session.jsonl
    第 1 行 header，后续行为消息记录。torn tail 时截断不报错。
    """

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
                    break  # torn tail
                if header is None:
                    header = data
                else:
                    records.append(data)
        return header, records

    def update_header(self, session_id: str, updates: dict) -> None:
        """就地更新 header 字段（重写第一行）。"""
        header, records = self.read(session_id)
        if header is None:
            return
        header.update(updates)
        path = self._path(session_id)
        lines = [json.dumps(header, ensure_ascii=False)]
        for r in records:
            lines.append(json.dumps(r, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
