"""记忆账本 —— 变更事件（`memory/events.jsonl`，**真相源**）+ 召回留痕（`memory.db`，**派生**）。

两个载体的性质不同，所以规矩也不同：

| 载体 | 性质 | 备份 | 损坏 |
|---|---|---|---|
| `events.jsonl` | 真相源 · append-only · 人类可读可 git | **随记忆目录一起备** | 隔离留档，不删 |
| `memory_recall_log` | 派生 · 仅统计素材 | 不进备份 | 删库重建 |

**为什么变更历史不能只存数据库**：它是审计依据，一旦数据库重建（派生库的既定
策略就是"损坏即重建"）历史就全失联了。所以变更历史落在 append-only 的 JSONL 里，
与 `tool_audit.jsonl` 的同居思路一致。

召回留痕**不依赖 embedding**：`MEMORY_RECALL_MODE=keyword` 的隐私模式下同样要写，
所以账本与 `MemoryVectorIndex` 是两条独立的取用路径（后者没配 key 就是 None）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..config import cfg
from .db import memory_db_path, open_memory_db
from .schema import now_stamp

logger = logging.getLogger(__name__)

EVENTS_FILE = "events.jsonl"

# 事件类型（写入侧只允许这几个，避免日志被自由文本污染）
EVENT_TYPES = frozenset(
    {"create", "update", "merge", "supersede", "forget", "restore", "hard_delete", "migrate"}
)


# ── 变更事件（真相源）─────────────────────────────────────────────


def events_path(memory_dir: str | Path | None = None) -> Path:
    base = Path(memory_dir) if memory_dir else cfg.MEMORY_DIR
    return base / EVENTS_FILE


def append_event(
    memory_dir: str | Path | None,
    event: str,
    *,
    memory_id: str | None = None,
    name: str | None = None,
    actor: str = "system",
    **detail,
) -> None:
    """追加一条变更事件。**失败只告警**——留痕不该阻断写入。

    用 `O_APPEND` 追加 + `fsync`：单进程顺序追加在这里是安全的，
    不需要整份重写（那是 `atomic_write_text` 的语义，代价 O(全文)）。
    """
    record = {
        "ts": now_stamp(),
        "event": str(event),
        "actor": str(actor or "system"),
    }
    if memory_id:
        record["id"] = str(memory_id)
    if name:
        record["name"] = str(name)
    if detail:
        record["detail"] = detail
    try:
        path = events_path(memory_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception as e:
        logger.warning("记忆事件写入失败（不影响本次操作）：%s: %s", type(e).__name__, e)


def read_events(
    memory_dir: str | Path | None = None,
    *,
    memory_id: str | None = None,
    name: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """读变更历史（新的在前）。解析不出的行**跳过但不删**——手改坏一行不该丢全档。"""
    path = events_path(memory_dir)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        logger.warning("读取记忆事件失败：%s", e)
        return []
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if memory_id and record.get("id") != memory_id:
            continue
        if name and record.get("name") != name:
            continue
        out.append(record)
        if len(out) >= max(int(limit), 1):
            break
    return out


# ── 召回留痕（派生）──────────────────────────────────────────────


class MemoryLedger:
    """召回留痕的读写。所有方法都**不抛异常**——留痕失败绝不能影响一轮对话。"""

    def __init__(self, memory_dir: str | Path | None = None,
                 db_path: str | Path | None = None,
                 maintenance: bool | None = None):
        self._dir = Path(memory_dir) if memory_dir else cfg.MEMORY_DIR
        self._path = Path(db_path) if db_path else memory_db_path(self._dir)
        self._lock = threading.Lock()
        self._maintenance = cfg.DB_MAINTENANCE_ENABLED if maintenance is None else maintenance
        self._conn = open_memory_db(self._path, maintenance=self._maintenance)
        if self._maintenance:
            self.prune()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None

    # ── 写 ────────────────────────────────────────────────────────

    def log_recall(
        self,
        *,
        session_id: str | None,
        mode: str,
        query_text: str,
        candidates: list[tuple[str, float]] | None = None,
        injected: list[str] | None = None,
        gate_blocked: list[str] | None = None,
        margin: float = 0.0,
        latency_ms: int = 0,
        turn_seq: int = 0,
    ) -> None:
        """记录一轮召回。`candidates` 是 (名字, 分数) 列表。"""
        try:
            row = (
                now_stamp(),
                str(session_id or ""),
                int(turn_seq or 0),
                str(mode or ""),
                str(query_text or "")[:2000],
                json.dumps(
                    [{"name": n, "score": round(float(s), 6)} for n, s in (candidates or [])],
                    ensure_ascii=False,
                ),
                json.dumps(list(injected or []), ensure_ascii=False),
                json.dumps(list(gate_blocked or []), ensure_ascii=False),
                float(margin or 0.0),
                int(latency_ms or 0),
            )
        except Exception as e:
            logger.debug("召回留痕序列化失败：%s", e)
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT INTO memory_recall_log"
                    " (ts, session_id, turn_seq, mode, query_text, candidates,"
                    "  injected, gate_blocked, margin, latency_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            except sqlite3.Error as e:
                logger.warning("召回留痕写入失败：%s", e)

    # ── 读 ────────────────────────────────────────────────────────

    def recent(self, limit: int = 50, session_id: str | None = None) -> list[dict]:
        sql = (
            "SELECT ts, session_id, turn_seq, mode, query_text, candidates,"
            " injected, gate_blocked, margin, latency_ms FROM memory_recall_log"
        )
        params: list = []
        if session_id:
            sql += " WHERE session_id = ?"
            params.append(str(session_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(int(limit), 1))
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                logger.warning("读取召回留痕失败：%s", e)
                return []
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row) -> dict:
        def _loads(text, fallback):
            try:
                value = json.loads(text)
                return value if isinstance(value, type(fallback)) else fallback
            except (TypeError, json.JSONDecodeError):
                return fallback

        return {
            "ts": str(row[0]),
            "session_id": str(row[1] or ""),
            "turn_seq": int(row[2] or 0),
            "mode": str(row[3] or ""),
            "query_text": str(row[4] or ""),
            "candidates": _loads(row[5], []),
            "injected": _loads(row[6], []),
            "gate_blocked": _loads(row[7], []),
            "margin": float(row[8] or 0.0),
            "latency_ms": int(row[9] or 0),
        }

    def stats(self) -> dict:
        with self._lock:
            if self._conn is None:
                return {"count": 0, "db_bytes": 0}
            try:
                count = int(
                    self._conn.execute("SELECT COUNT(*) FROM memory_recall_log").fetchone()[0]
                )
            except sqlite3.Error:
                count = 0
        return {
            "count": count,
            "db_bytes": self._path.stat().st_size if self._path.exists() else 0,
        }

    # ── 保留期 ────────────────────────────────────────────────────

    def prune(self, days: int | None = None) -> int:
        """裁剪超过保留期的召回留痕。`<=0` 表示永久保留。返回删除行数。"""
        keep = cfg.MEMORY_RECALL_LOG_RETENTION_DAYS if days is None else int(days)
        if keep <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=keep)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if self._conn is None:
                return 0
            try:
                cur = self._conn.execute(
                    "DELETE FROM memory_recall_log WHERE ts < ?", (cutoff,)
                )
                removed = int(cur.rowcount or 0)
            except sqlite3.Error as e:
                logger.warning("召回留痕裁剪失败：%s", e)
                return 0
        if removed:
            logger.info("召回留痕裁剪：删除 %d 行（保留 %d 天）", removed, keep)
        return removed


# ── 进程级单例 ────────────────────────────────────────────────────

_LEDGER_CACHE: dict[str, MemoryLedger] = {}


def get_memory_ledger(memory_dir: str | Path | None = None) -> MemoryLedger | None:
    """账本单例；建不起来返回 None（调用方静默跳过留痕）。

    与 `get_memory_index()` 同构：**失败不缓存**，避免把一次故障固化成永久降级。
    """
    path = str(memory_db_path(memory_dir))
    cached = _LEDGER_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        ledger = MemoryLedger(memory_dir)
    except Exception as e:
        logger.warning("记忆账本不可用（本轮不留痕）：%s: %s", type(e).__name__, e)
        return None
    _LEDGER_CACHE[path] = ledger
    return ledger


def reset_memory_ledger() -> None:
    """关闭并清空单例（测试隔离用）。"""
    for ledger in _LEDGER_CACHE.values():
        ledger.close()
    _LEDGER_CACHE.clear()
