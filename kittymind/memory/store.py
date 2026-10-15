"""
持久化记忆存储 —— 每条记忆是一个带 YAML frontmatter 的 Markdown 文件。

存储位置（全局，不随工作区变化）：
  ~/.kittymind/memory/
    MEMORY.md          ← 人类可读索引（运行时只读不用，见 P4）
    <slug>.md          ← 具体记忆文件（同名不同 slug 时加 `-<id8>` 后缀消歧）
    events.jsonl       ← 追加式变更历史（真相源）
    _trash/            ← 软删 / 被合并条目的墓碑（可 restore）
    _quarantine/       ← 无法解析的损坏文件（启动自检时移入，不删除）
    .v2_migrated       ← 迁移标记（幂等重跑的快速出口）

## v2 的三条硬规矩（P0 修的是"静默丢数据"）

1. **身份是 `id`，不是文件名。** 旧实现的身份就是文件名（`_slug(name)`），于是
   `write("My Note")` 会把 `write("my note")` 静默覆盖掉（实测 count 2→1），
   改名等于失忆，事件日志也无法与文件对应。
2. **同名写入永不静默覆盖。** 同名 → 保留 `id`、替换正文，旧正文进 `events.jsonl`
   （可回溯）；`merge=True` 时改按行去重拼接。名字不同但 slug 撞车（`My Note` vs
   `my note`，旧实现就在这里把第一条吃掉了）→ **新条目 + 文件名加 `-<id8>`**。
   两种情况都不会丢内容。
3. **默认不删。** 删除 = `status=forgotten` + 文件移入 `_trash/`，可 `restore_item()`；
   物理删除只有 `hard_delete()` 一个入口，且只应由用户显式操作触发。

写入一律走「同目录临时文件 + 原子替换」：直接 `write_text` 时若进程在写一半
被杀死/断电，会留下半截 Markdown（frontmatter 断裂），这条记忆就再也读不出来，
而且是静默丢失——索引重建时它直接消失，没人知道曾经有过。
"""

import hashlib
import logging
import shutil
from datetime import datetime
from pathlib import Path

from ..config import cfg
from ..storage import atomic_write_text
from . import schema
from .db import purge_legacy_index_db
from .ledger import append_event
from .schema import MEMORY_TYPES, document, normalize, parse, slugify

logger = logging.getLogger(__name__)

_INDEX_FILE = "MEMORY.md"
_QUARANTINE_DIR = "_quarantine"
_TRASH_DIR = "_trash"
_MIGRATED_MARKER = ".v2_migrated"

__all__ = ["MEMORY_TYPES", "MemoryStore"]


class MemoryStore:
    """记忆文件的读写与索引管理。路径固定为 ~/.kittymind/memory/。"""

    def __init__(self, memory_dir: str | Path | None = None,
                 selfcheck: bool | None = None):
        self._dir = Path(memory_dir) if memory_dir else cfg.MEMORY_DIR
        self._index = self._dir / _INDEX_FILE
        enabled = cfg.DB_MAINTENANCE_ENABLED if selfcheck is None else selfcheck
        if enabled:
            self.selfcheck()
            self.migrate_v2()

    # ── 公开接口 ──────────────────────────────────────────────────

    @property
    def directory(self) -> Path:
        """记忆目录。派生库与账本的路径由它派生（见 memory/db.py）。"""
        return self._dir

    def write(self, name: str, mem_type: str, description: str, body: str,
              *, merge: bool = False, source: dict | None = None,
              actor: str = "system") -> Path:
        """写入一条记忆。**同名绝不静默覆盖**，返回落盘路径。

        同名时的默认语义是**替换正文但保留身份**：`id` / `created_at` 不变，
        旧正文写进 `events.jsonl`（`update` 事件带 `old_body`），所以"覆盖"是可回溯、
        可复原的。`merge=True` 则按行去重拼接正文（留给"确认同一件事"的合并语义）。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        clean_name = str(name or "").strip()
        clean_type = mem_type if mem_type in MEMORY_TYPES else schema.DEFAULT_TYPE
        clean_desc = schema.normalize_description(description)
        clean_body = str(body or "").strip()
        stamp = schema.now_stamp()

        existing = self.read(clean_name)
        if existing is not None:
            return self._update_existing(
                existing, clean_name, clean_type, clean_desc, clean_body,
                stamp, merge=merge, actor=actor,
            )

        meta = normalize({
            "id": schema.new_id(),
            "name": clean_name,
            "type": clean_type,
            "description": clean_desc,
            "created_at": schema.now_iso(),
            "updated_at": stamp,
            "last_confirmed_at": schema.now_iso(),
            "source": source,
            "content_hash": _digest(clean_body),
        })
        path = self._target_path(clean_name, meta["id"])
        self._write_atomic(path, meta, clean_body)
        self._rebuild_index()
        append_event(self._dir, "create", memory_id=meta["id"], name=clean_name,
                     actor=actor, type=clean_type, path=path.name)
        return path

    def read(self, name: str) -> dict | None:
        """按名称或 id 读取一条记忆（不含 `_trash/` 里的墓碑）。"""
        key = str(name or "")
        fast = self._dir / f"{slugify(key)}.md"
        if fast.is_file():
            record = self._load_file(fast)
            if record and record.get("name") == key:
                return record
        for path in self._iter_items():
            record = self._load_file(path)
            if record and (record.get("name") == key or record.get("id") == key):
                return record
        return None

    def delete(self, name: str, *, merged_into: str | None = None,
               superseded_by: str | None = None, actor: str = "system") -> bool:
        """**软删**一条记忆：标记 `forgotten` 并移入 `_trash/`，返回是否成功。

        必须是软删：整合的 `drop` 走的就是这里，而"两条记忆是否重复"是模型的主观
        判断，误判时用户必须能把记忆拿回来。
        """
        record = self.read(name)
        if record is None:
            return False
        meta = normalize(record)
        meta["status"] = "forgotten"
        meta["updated_at"] = schema.now_stamp()
        if merged_into:
            # 引用一律落 **id**：名字是可变属性，目标改名后墓碑上的名字就成了死链，
            # "这条去哪了"再也答不出来。整合侧给的是名字，这里统一归一。
            meta["merged_into"] = self._resolve_id(merged_into)
        if superseded_by:
            meta["superseded_by"] = self._resolve_id(superseded_by)
        path = Path(record["path"])
        target = self._move_to_trash(path, meta["id"])
        if target is None:
            return False
        self._write_atomic(target, meta, str(record.get("body", "")))
        self._rebuild_index()
        event = "merge" if merged_into else ("supersede" if superseded_by else "forget")
        append_event(self._dir, event, memory_id=meta["id"], name=meta["name"],
                     actor=actor, merged_into=meta.get("merged_into"),
                     superseded_by=meta.get("superseded_by"),
                     trashed_as=target.name)
        return True

    def hard_delete(self, name: str, *, actor: str = "user") -> bool:
        """物理删除（连同 `_trash/` 里的同名墓碑）。**只应由用户显式操作触发。**"""
        key = str(name or "")
        removed = False
        for path in list(self._iter_items()) + list(self._iter_trash()):
            record = self._load_file(path)
            if record and (record.get("name") == key or record.get("id") == key):
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning("物理删除失败 %s: %s", path.name, e)
                    continue
                append_event(self._dir, "hard_delete", memory_id=record.get("id"),
                             name=record.get("name"), actor=actor, path=path.name)
                removed = True
        if removed:
            self._rebuild_index()
        return removed

    def restore_item(self, name: str, *, actor: str = "system") -> bool:
        """把任意状态的记忆恢复到 `active`（含从 `_trash/` 移回主目录）。"""
        key = str(name or "")
        record = self.read(key)
        trashed: Path | None = None
        if record is None:
            for path in self._iter_trash():
                item = self._load_file(path)
                if item and (item.get("name") == key or item.get("id") == key):
                    record, trashed = item, path
                    break
        if record is None:
            return False

        meta = normalize(record)
        meta["status"] = "active"
        meta["updated_at"] = schema.now_stamp()
        meta["merged_into"] = None
        meta["superseded_by"] = None
        body = str(record.get("body", ""))
        if trashed is not None:
            target = self._target_path(meta["name"], meta["id"])
            self._dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(trashed), str(target))
            except OSError as e:
                logger.warning("从 _trash 移回失败 %s: %s", trashed.name, e)
                return False
            self._write_atomic(target, meta, body)
        else:
            self._write_atomic(Path(record["path"]), meta, body)
        self._rebuild_index()
        append_event(self._dir, "restore", memory_id=meta["id"], name=meta["name"],
                     actor=actor)
        return True

    def trashed(self) -> list[dict]:
        """`_trash/` 里的墓碑（供 UI 的"已删除/可恢复"列表）。"""
        out: list[dict] = []
        for path in self._iter_trash():
            record = self._load_file(path)
            if record:
                out.append(record)
        return sorted(out, key=lambda r: str(r.get("updated_at") or ""), reverse=True)

    def all_memories(self) -> list[dict]:
        """返回所有**在册**记忆（不含 `_trash/` 与 `_quarantine/`），按文件名排序。"""
        memories = []
        for path in self._iter_items():
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
        return sum(1 for _ in self._iter_items())

    def snapshot(self) -> dict[str, str]:
        """备份所有在册记忆文件内容（用于整合失败时回滚）。"""
        return {
            p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in self._iter_items()
        }

    def restore(self, snap: dict[str, str]):
        """整体回滚到快照（整合失败时用）。

        除了重写主目录，还要**清掉快照里已存在于 `_trash/` 的墓碑**——否则回滚后
        同一条记忆会同时存在于主目录与回收站，`trashed()` 会显示一条"已删除"的
        幽灵条目。
        """
        for p in list(self._iter_items()):
            p.unlink()
        for filename, content in snap.items():
            atomic_write_text(self._dir / filename, content)
            ghost = self._dir / _TRASH_DIR / filename
            if ghost.is_file():
                try:
                    ghost.unlink()
                except OSError as e:
                    logger.debug("清理回滚残留墓碑失败 %s: %s", ghost.name, e)
        self._rebuild_index()

    # ── 启动自检与迁移 ────────────────────────────────────────────

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

    def migrate_v2(self) -> int:
        """一次性补齐 v2 frontmatter 字段（幂等）。返回本次改写的条数。

        两条纪律：

        - **解析用旧正则、序列化用 YAML**。直接用 `safe_load` 读旧文件会让一批记忆
          从"能读"变成"读不出"或"内容被吃掉"（见 `schema.py` 的实测表），所以旧
          文件必须先用正则读懂，再按新格式写回。
        - **文件名与位置不变**（只补 frontmatter），于是 git diff 只有内容变化，
          没有几十个 rename。

        带 `.v2_migrated` 标记做快速出口：标记在且版本一致就直接返回，不再逐个读文件。
        **标记必须最后落**：它一落下这段逻辑就再也不会重进，所以清旧库、写审计
        都得排在它前面，否则一次中断会永久留下半截痕迹（真实库上出现过：标记写着
        `items=12`，旧索引库却还在，事件也没写）。清理没成功就不写标记，
        下次启动靠同一个快速出口之外的重试补上——重试代价只是重扫一遍目录。
        """
        # 旧索引库的清理是幂等的（文件不在就只是一次 stat），放在快速出口**之前**：
        # 万一它后来又冒出来（回退到 Phase 17M 代码再升回来），也不会被标记挡住。
        legacy_clean = purge_legacy_index_db(self._dir)
        if self._v2_done():
            return 0
        changed = 0
        for path in list(self._iter_items()):
            record = self._load_file(path)
            if record is None:
                continue
            if schema.is_valid_id(record.get("id")) and record.get("created_at"):
                continue
            meta = normalize(record)
            if not schema.is_valid_id(meta.get("id")):
                meta["id"] = schema.new_id()
            if not meta.get("created_at"):
                meta["created_at"] = _mtime_stamp(path)
            if not meta.get("updated_at"):
                meta["updated_at"] = _mtime_stamp(path)
            meta["status"] = meta.get("status") or "active"
            meta["content_hash"] = _digest(str(record.get("body", "")))
            self._write_atomic(path, meta, str(record.get("body", "")))
            changed += 1
        if changed:
            # 传 kwargs 而不是 `detail={...}`：`append_event` 的签名是 `**detail`，
            # 再套一层会写成 `detail.detail`（真实库上第一版就是这样落盘的）。
            append_event(self._dir, "migrate", actor="system",
                         version=schema.SCHEMA_VERSION, items=changed)
            logger.info("记忆 frontmatter 迁移到 v2：改写 %d 条", changed)
        if not legacy_clean:
            return changed          # 不写标记 → 下次启动重试（重试代价只是重扫一遍目录）
        self._write_marker(changed)
        return changed

    def _v2_done(self) -> bool:
        marker = self._dir / _MIGRATED_MARKER
        if not marker.is_file():
            return False
        try:
            first = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        return bool(first) and first[0].strip() == str(schema.SCHEMA_VERSION)

    def _write_marker(self, changed: int) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / _MIGRATED_MARKER).write_text(
                f"{schema.SCHEMA_VERSION}\n# at={schema.now_stamp()} items={changed}\n",
                encoding="utf-8",
            )
        except OSError as e:
            logger.debug("迁移标记写入失败（下次会重扫）：%s", e)

    # ── 内部工具 ──────────────────────────────────────────────────

    def _iter_items(self):
        """主目录里的记忆文件（`_trash/` `_quarantine/` 是子目录，天然不在内）。"""
        if not self._dir.is_dir():
            return iter(())
        return (p for p in sorted(self._dir.glob("*.md")) if p.name != _INDEX_FILE)

    def _iter_trash(self):
        trash = self._dir / _TRASH_DIR
        if not trash.is_dir():
            return iter(())
        return iter(sorted(trash.glob("*.md")))

    def _update_existing(self, existing: dict, name: str, mem_type: str,
                         description: str, body: str, stamp: str, *,
                         merge: bool, actor: str) -> Path:
        """同名写入：保留 `id`，合并或替换正文，并留痕。"""
        meta = normalize(existing)
        old_body = str(existing.get("body", ""))
        meta["type"] = mem_type
        meta["description"] = description
        meta["updated_at"] = stamp
        meta["last_confirmed_at"] = schema.now_iso()
        meta["status"] = "active"
        if merge:
            new_body = _merge_bodies([old_body, body])
            meta["strength"] = float(meta.get("strength") or 1.0) + 0.5
        else:
            new_body = body
        meta["content_hash"] = _digest(new_body)
        path = self._target_path(name, meta["id"], existing_path=existing.get("path"))
        self._write_atomic(path, meta, new_body)
        self._rebuild_index()
        append_event(
            self._dir, "update", memory_id=meta["id"], name=name, actor=actor,
            mode="merge" if merge else "replace",
            old_hash=_digest(old_body), new_hash=meta["content_hash"],
            old_body="" if merge else old_body,
        )
        return path

    def _target_path(self, name: str, memory_id: str,
                     existing_path: str | None = None) -> Path:
        """决定落盘路径：优先 `<slug>.md`；被**别的**记忆占用时加 `-<id8>` 消歧。"""
        if existing_path:
            return Path(existing_path)
        candidate = self._dir / f"{slugify(name)}.md"
        if not candidate.exists():
            return candidate
        holder = self._load_file(candidate)
        if holder is None or holder.get("name") == name or holder.get("id") == memory_id:
            return candidate
        return self._dir / f"{slugify(name)}-{schema.id_suffix(memory_id)}.md"

    def _resolve_id(self, key: str) -> str:
        """把名字或 id 归一成 id（归一不了就原样返回，绝不猜）。

        墓碑 / 取代关系里的引用必须指向 id：名字会被改，id 不会。调用方
        （整合的模型指令、RPC 的 UI 操作）习惯给人名，所以在这一层收口。
        """
        text = str(key or "").strip()
        if not text or schema.is_valid_id(text):
            return text
        target = self.read(text)
        if target and schema.is_valid_id(str(target.get("id") or "")):
            return str(target["id"])
        return text

    def _move_to_trash(self, path: Path, memory_id: str) -> Path | None:
        trash = self._dir / _TRASH_DIR
        try:
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / path.name
            if target.exists():
                target = trash / f"{path.stem}-{schema.id_suffix(memory_id)}.md"
            shutil.move(str(path), str(target))
        except OSError as e:
            logger.warning("移入回收站失败 %s: %s", path.name, e)
            return None
        return target

    def _write_atomic(self, path: Path, meta: dict, body: str) -> None:
        atomic_write_text(path, document(meta, body))

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
            meta, body = parse(content)
            if not meta or not meta.get("name"):
                return None
            return {**meta, "body": body, "path": str(path)}
        except Exception:
            return None

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

    # 兼容旧调用点（P0 起实际实现都在 schema.py）
    _slug = staticmethod(slugify)

    @staticmethod
    def _document(name: str, mem_type: str, description: str,
                  body: str, updated_at: str) -> str:
        return document(normalize({
            "name": name, "type": mem_type,
            "description": description, "updated_at": updated_at,
        }), body)

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        meta, body = parse(content)
        return (meta or {}), body


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _mtime_stamp(path: Path) -> str:
    """用文件 mtime 当 `created_at` 的近似值（迁移时的兜底，比"现在"更接近事实）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return schema.now_stamp()


def _merge_bodies(bodies: list[str]) -> str:
    """按行拼接多段正文并去重（合并正文不由模型改写，避免转义与信息歪曲）。"""
    seen: set[str] = set()
    lines: list[str] = []
    for body in bodies:
        for raw in str(body or "").splitlines():
            key = raw.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(raw.rstrip())
    return "\n".join(lines)
