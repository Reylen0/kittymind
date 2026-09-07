"""
持久化记忆存储 —— 每条记忆是一个带 YAML frontmatter 的 Markdown 文件。

存储位置（全局，不随工作区变化）：
  ~/.kittymind/memory/
    MEMORY.md          ← 索引（每条记忆一行）
    user-dark-theme.md ← 具体记忆文件
    project-auth.md
    ...
"""

import os
import re
from pathlib import Path

_INDEX_FILE = "MEMORY.md"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^(\w[\w-]*)\s*:\s*(.+)$", re.MULTILINE)

MEMORY_TYPES = {"user", "feedback", "project", "reference"}


def _default_memory_dir() -> Path:
    return Path.home() / ".kittymind" / "memory"


class MemoryStore:
    """记忆文件的读写和索引管理。路径固定为 ~/.kittymind/memory/。"""

    def __init__(self, memory_dir: str | Path | None = None):
        self._dir = Path(memory_dir) if memory_dir else _default_memory_dir()
        self._index = self._dir / _INDEX_FILE

    # ── 公开接口 ──────────────────────────────────────────────────

    def write(self, name: str, mem_type: str, description: str, body: str) -> Path:
        """写入或覆盖一条记忆，同步重建索引。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{self._slug(name)}.md"
        path.write_text(
            self._document(name, mem_type, description, body),
            encoding="utf-8",
        )
        self._rebuild_index()
        return path

    def read(self, name: str) -> dict | None:
        """按名称读取一条记忆。"""
        path = self._dir / f"{self._slug(name)}.md"
        if path.is_file():
            return self._load_file(path)
        for p in self._dir.glob("*.md"):
            if p.name == _INDEX_FILE:
                continue
            record = self._load_file(p)
            if record and record.get("name") == name:
                return record
        return None

    def delete(self, name: str) -> bool:
        """删除一条记忆，返回是否成功。"""
        path = self._dir / f"{self._slug(name)}.md"
        if path.is_file():
            path.unlink()
            self._rebuild_index()
            return True
        return False

    def all_memories(self) -> list[dict]:
        """返回所有记忆，按文件名排序。"""
        if not self._dir.is_dir():
            return []
        memories = []
        for path in sorted(self._dir.glob("*.md")):
            if path.name == _INDEX_FILE:
                continue
            record = self._load_file(path)
            if record and record.get("name"):
                memories.append(record)
        return memories

    def catalog(self) -> str:
        """生成记忆目录字符串（序号. [类型] 名称: 描述），供 LLM 选择。"""
        if self._index.is_file():
            try:
                lines = self._index.read_text(encoding="utf-8").splitlines()
                entries = [l[2:] for l in lines if l.startswith("- [")]
                if entries:
                    return "\n".join(f"{i}. {e}" for i, e in enumerate(entries))
            except Exception:
                pass
        memories = self.all_memories()
        if not memories:
            return ""
        return "\n".join(
            f"{i}. [{m.get('type','?')}] {m.get('name','?')}: {m.get('description','')}"
            for i, m in enumerate(memories)
        )

    def count(self) -> int:
        if not self._dir.is_dir():
            return 0
        return sum(1 for p in self._dir.glob("*.md") if p.name != _INDEX_FILE)

    def snapshot(self) -> dict[str, str]:
        """备份所有记忆文件内容（用于整合失败时回滚）。"""
        if not self._dir.is_dir():
            return {}
        return {
            p.name: p.read_text(encoding="utf-8")
            for p in self._dir.glob("*.md")
            if p.name != _INDEX_FILE
        }

    def restore(self, snap: dict[str, str]):
        for p in self._dir.glob("*.md"):
            if p.name != _INDEX_FILE:
                p.unlink()
        for filename, content in snap.items():
            (self._dir / filename).write_text(content, encoding="utf-8")
        self._rebuild_index()

    def delete_all(self):
        if not self._dir.is_dir():
            return
        for path in self._dir.glob("*.md"):
            if path.name != _INDEX_FILE:
                path.unlink()

    # ── 内部工具 ──────────────────────────────────────────────────

    def _rebuild_index(self):
        memories = self.all_memories()
        self._dir.mkdir(parents=True, exist_ok=True)
        if not memories:
            self._index.write_text("# Memory Index\n\n(no memories yet)\n", encoding="utf-8")
            return
        lines = ["# Memory Index\n"]
        for m in memories:
            filename = Path(m["path"]).name
            lines.append(
                f"- [{m.get('name','?')}]({filename}) "
                f"[{m.get('type','?')}] — {m.get('description','')}"
            )
        self._index.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_file(self, path: Path) -> dict | None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            meta, body = self._parse_frontmatter(content)
            if not meta.get("name"):
                return None
            return {**meta, "body": body, "path": str(path)}
        except Exception:
            return None

    @staticmethod
    def _slug(name: str) -> str:
        slug = name.lower()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:64] or "memory"

    @staticmethod
    def _document(name: str, mem_type: str, description: str, body: str) -> str:
        return (
            f"---\nname: {name}\ntype: {mem_type}\n"
            f"description: {description}\n---\n\n{body.strip()}\n"
        )

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        m = _FRONTMATTER_RE.match(content)
        if not m:
            return {}, content
        meta = {k.strip(): v.strip() for k, v in _KV_RE.findall(m.group(1))}
        body = content[m.end():].strip()
        return meta, body
