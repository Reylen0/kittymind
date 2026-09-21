"""
持久化记忆存储 —— 每条记忆是一个带 YAML frontmatter 的 Markdown 文件。

存储位置（全局，不随工作区变化）：
  ~/.kittymind/memory/
    MEMORY.md          ← 索引（每条记忆一行）
    user-dark-theme.md ← 具体记忆文件
    project-auth.md
    ...
    _quarantine/       ← 无法解析的损坏文件（启动自检时移入，不删除）

写入一律走「同目录临时文件 + 原子替换」：直接 `write_text` 时若进程在写一半
被杀死/断电，会留下半截 Markdown（frontmatter 断裂），这条记忆就再也读不出来，
而且是静默丢失——索引重建时它直接消失，没人知道曾经有过。
"""

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..config import cfg
from ..storage import atomic_write_text

logger = logging.getLogger(__name__)

_INDEX_FILE = "MEMORY.md"
_QUARANTINE_DIR = "_quarantine"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^(\w[\w-]*)\s*:\s*(.+)$", re.MULTILINE)

MEMORY_TYPES = {"user", "feedback", "project", "reference"}


def _default_memory_dir() -> Path:
    return cfg.MEMORY_DIR


class MemoryStore:
    """记忆文件的读写和索引管理。路径固定为 ~/.kittymind/memory/。"""

    def __init__(self, memory_dir: str | Path | None = None,
                 selfcheck: bool | None = None):
        self._dir = Path(memory_dir) if memory_dir else _default_memory_dir()
        self._index = self._dir / _INDEX_FILE
        enabled = cfg.DB_MAINTENANCE_ENABLED if selfcheck is None else selfcheck
        if enabled:
            self.selfcheck()

    # ── 公开接口 ──────────────────────────────────────────────────

    def write(self, name: str, mem_type: str, description: str, body: str) -> Path:
        """写入或覆盖一条记忆，同步重建索引。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{self._slug(name)}.md"
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_text(
            path, self._document(name, mem_type, description, body, updated_at)
        )
        self._rebuild_index()
        return path

    def selfcheck(self) -> list[Path]:
        """扫描记忆目录，把解析不出来的文件移入 `_quarantine/`，返回被移走的路径。

        为什么要动它而不是忽略：读不出来的记忆在 `all_memories()` / `catalog()`
        里是**静默消失**的（`_load_file` 返回 None 就跳过），用户只会觉得"我明明
        存过这条记忆"。隔离（改名移到子目录 + 告警）让损坏可见，同时保住原文——
        手工修好 frontmatter 再移回来即可。
        """
        if not self._dir.is_dir():
            return []
        moved: list[Path] = []
        for path in sorted(self._dir.glob("*.md")):
            if path.name == _INDEX_FILE:
                continue
            if self._load_file(path) is not None:
                continue
            target = self._quarantine(path)
            if target is not None:
                moved.append(target)
        if moved:
            self._rebuild_index()
        return moved

    def _quarantine(self, path: Path) -> Path | None:
        """把损坏文件移到 `_quarantine/`（重名时加时间戳，绝不覆盖）。"""
        dest_dir = self._dir / _QUARANTINE_DIR
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / path.name
            if target.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = dest_dir / f"{path.stem}-{stamp}{path.suffix}"
            shutil.move(str(path), str(target))
        except OSError:
            return None
        logger.warning(
            "记忆 %s 无法解析（frontmatter 缺失或损坏），已隔离到 %s/%s",
            path.name, _QUARANTINE_DIR, target.name,
        )
        return target

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
                entries = [ln[2:] for ln in lines if ln.startswith("- [")]
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
            atomic_write_text(self._dir / filename, content)
        self._rebuild_index()

    # ── 内部工具 ──────────────────────────────────────────────────

    def _rebuild_index(self):
        memories = self.all_memories()
        self._dir.mkdir(parents=True, exist_ok=True)
        if not memories:
            atomic_write_text(self._index, "# Memory Index\n\n(no memories yet)\n")
            return
        lines = ["# Memory Index\n"]
        for m in memories:
            filename = Path(m["path"]).name
            lines.append(
                f"- [{m.get('name','?')}]({filename}) "
                f"[{m.get('type','?')}] — {m.get('description','')}"
            )
        atomic_write_text(self._index, "\n".join(lines) + "\n")

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
    def _document(name: str, mem_type: str, description: str, body: str, updated_at: str) -> str:
        return (
            f"---\nname: {name}\ntype: {mem_type}\n"
            f"description: {description}\nupdated_at: {updated_at}\n---\n\n{body.strip()}\n"
        )

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        m = _FRONTMATTER_RE.match(content)
        if not m:
            return {}, content
        meta = {k.strip(): v.strip() for k, v in _KV_RE.findall(m.group(1))}
        body = content[m.end():].strip()
        return meta, body
