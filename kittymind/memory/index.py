"""记忆向量索引 —— 与记忆文件同级的独立库 `memory_index.db`（Phase 17M）。

职责范围很窄：把记忆文本编码成向量、落库、对账、提供余弦检索。**它不读记忆
目录**——调用方把 `MemoryStore.all_memories()` 的结果传进来，索引只回答
"哪些条目的向量过期了、需要重嵌"。这样 `memory/store.py` 保持"只管 Markdown
文件读写"的单一职责。

四个刻意的设计决定：

  1. **独立库，不是 sessions.db 加一张表**。索引是纯派生数据（随时可从
     `memory/*.md` 全量重建），与会话库的保留期 / 归档 / 删除语义毫不相干；
     混在一起会让"重建记忆索引"变成碰用户会话数据的风险操作。代价正好是收益：
     它**不进备份、不进隔离**，损坏时直接删库重建即可。
  2. **model_id / dim 是硬校验，不是描述性元数据**。不同模型（乃至同模型不同
     `dimensions`）的向量不可比，混用会静默给出垃圾相似度——这是这类设计最阴的
     失败模式，因为结果"看起来像那么回事"。对账时任一不匹配即整表重嵌。
  3. **对账走 stat 快路径，只有疑似变化才构造索引文本算 hash**。1000 条记忆每次
     召回都读 1000 个文件不可接受；而 mtime+size 没变还去算 sha256 是纯浪费。
     mtime 变了但 hash 没变（编辑器 touch、我们自己的原子写重写同内容）时只更新
     元数据，不重嵌。
  4. **无文件信息的条目（纯 dict）退化为纯 hash 对账**。mtime 为 0 表示"没有可比
     的文件时间戳"，此时不能走快路径，否则同 size 同"无 mtime"的不同内容会被漏检。

检索本身是 numpy 全量暴力余弦，**刻意不引入 sqlite-vec / ANN**：本机实测
1000 条 × 768 维一次全量余弦 0.082 ms、常驻 2.93 MB（2048 维 1000 条约 7.8 MB /
0.25 ms）。ANN 在 10³ 量级毫无意义，而 loadable extension 的打包成本是实打实的。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config import cfg
from ..embeddings import get_embedding
from ..storage import (
    Migration,
    MigrationFailedError,
    checkpoint,
    quick_check,
    run_migrations,
)

logger = logging.getLogger(__name__)

# 向量以 float32 小端紧凑数组存 BLOB：2048 维 = 8 KB/条，512 维 = 2 KB/条。
_VECTOR_DTYPE = "<f4"

_DDL = """
CREATE TABLE IF NOT EXISTS memory_vec (
    name       TEXT PRIMARY KEY,
    file       TEXT NOT NULL,
    text_hash  TEXT NOT NULL,      -- 索引文本（含正文前 N 字）的 sha256，不是文件字节
    mtime_ns   INTEGER NOT NULL DEFAULT 0,   -- 0 = 无文件信息，必须走 hash 对账
    file_size  INTEGER NOT NULL DEFAULT 0,
    model_id   TEXT NOT NULL,      -- 向量来源模型（硬校验，见模块 docstring ②）
    dim        INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    vector     BLOB NOT NULL
);
"""


def _m_init_memory_vec(conn: sqlite3.Connection) -> None:
    """v1：建向量表（幂等）。"""
    conn.executescript(_DDL)


_MIGRATIONS = [Migration(1, "create_memory_vec", _m_init_memory_vec)]


def index_text(memory: dict, body_chars: int = 300) -> str:
    """构造用于嵌入的索引文本：`[类型] 名称: 描述` + 正文前 N 字。

    **必须含正文**（实测依据）：只用平均 42 字的 description 时，平均
    `top1 - median` 间距 0.132；加上正文前 300 字后升到 0.145，且「你应该怎么跟
    我说话」这个 query 的 top1 从错误项（user-name）纠正为正确项（persona 记忆）。
    正文合计是描述长度的 4.8 倍，信息量全在这里。
    """
    head = (
        f"[{memory.get('type', '?')}] {memory.get('name', '?')}: "
        f"{memory.get('description', '')}"
    )
    body = str(memory.get("body") or "")
    if body_chars > 0 and body:
        head += "\n" + body[:body_chars]
    return head


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_stat(path: Path) -> tuple[int, int]:
    """返回 (mtime_ns, size)；拿不到文件时返回 (0, 0)。"""
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (int(st.st_mtime_ns), int(st.st_size))


class MemoryVectorIndex:
    """记忆名的向量索引。线程安全（递归调用点都在 `to_thread` 里）。"""

    def __init__(self, db_path: str | Path, embedder, *, maintenance: bool | None = None):
        self._path = Path(db_path)
        self._embedder = embedder
        self._lock = threading.Lock()
        self._cache: tuple[np.ndarray, list[str]] | None = None
        self._maintenance = cfg.DB_MAINTENANCE_ENABLED if maintenance is None else maintenance
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._prepare()

    # ── 生命周期 ──────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)

    def _discard(self, conn: sqlite3.Connection) -> None:
        """删掉库文件（连同 -wal/-shm）。

        与 `session/store.py` 的"隔离留档"刻意不同：记忆索引是纯派生数据，
        可以从 Markdown 全量重建，没有抢救价值——留档只会堆垃圾文件。
        """
        with contextlib.suppress(Exception):
            conn.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(str(self._path) + suffix).unlink()

    def _prepare(self) -> sqlite3.Connection:
        conn = self._connect()
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA journal_mode=WAL")
        if self._maintenance and cfg.DB_SELF_CHECK and quick_check(conn) != "ok":
            logger.warning("记忆索引库损坏，删库重建（记忆文件不受影响）")
            self._discard(conn)
            conn = self._connect()
        try:
            outcome = run_migrations(conn, _MIGRATIONS)
        except MigrationFailedError as e:
            logger.warning("记忆索引库迁移失败，删库重建：%s", e)
            self._discard(conn)
            conn = self._connect()
            outcome = run_migrations(conn, _MIGRATIONS)
        if outcome.changed:
            logger.info("记忆索引库 %s", outcome.describe())
        with contextlib.suppress(Exception):
            checkpoint(conn, "TRUNCATE")
        return conn

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            if self._conn is not None:
                if self._maintenance:
                    with contextlib.suppress(Exception):
                        checkpoint(self._conn, "TRUNCATE")
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None

    # ── 对账与写入 ────────────────────────────────────────────────

    def sync(self, memories: list[dict]) -> int:
        """让索引与 `memories` 对齐，返回本次新嵌的条数。

        `memories` 必须是 `MemoryStore.all_memories()` 的**完整**结果：不在其中的
        条目会被当作已删除而从索引里清掉。
        """
        with self._lock:
            rows = self._read_meta()
            if self._model_drifted(rows):
                logger.info(
                    "记忆索引模型/维度已变化（%s → %s），全量重嵌",
                    next(iter(rows.values()))["model_id"] if rows else "-",
                    self._embedder.model_id,
                )
                self._clear()
                rows = {}  # 必须同步清掉内存里的元数据视图，否则下面仍按旧 row 命中
                           # "hash 一致"而走 _touch，漂移根本不会触发重嵌

            pending: list[tuple] = []
            seen: set[str] = set()
            for m in memories:
                name = str(m.get("name") or "")
                if not name:
                    continue
                seen.add(name)
                path = Path(str(m.get("path") or ""))
                mtime_ns, size = _file_stat(path)
                row = rows.get(name)
                if row and mtime_ns > 0 and row["mtime_ns"] == mtime_ns and row["size"] == size:
                    continue  # 快路径：文件物理上没动过
                text = index_text(m, cfg.MEMORY_INDEX_TEXT_BODY_CHARS)
                digest = text_hash(text)
                if row and row["text_hash"] == digest:
                    self._touch(name, mtime_ns, size)  # 只是被 touch，向量仍然有效
                    continue
                pending.append((name, str(path), digest, mtime_ns, size, text))

            orphans = [n for n in rows if n not in seen]
            if orphans:
                self._delete(orphans)

            if not pending:
                return 0

            vectors = self._embedder.embed([p[5] for p in pending])
            if len(vectors) != len(pending):
                raise RuntimeError(
                    f"embedding 返回条数不匹配：期望 {len(pending)}，实际 {len(vectors)}"
                )
            self._upsert(pending, vectors)
            logger.info("记忆索引：新嵌/重嵌 %d 条（共 %d 条）", len(pending), len(seen))
            return len(pending)

    def _read_meta(self) -> dict[str, dict]:
        try:
            rows = self._conn.execute(
                "SELECT name, text_hash, mtime_ns, file_size, model_id, dim FROM memory_vec"
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("读取记忆索引失败（按空索引处理）：%s", e)
            return {}
        return {
            str(r[0]): {
                "text_hash": str(r[1]),
                "mtime_ns": int(r[2]),
                "size": int(r[3]),
                "model_id": str(r[4]),
                "dim": int(r[5]),
            }
            for r in rows
        }

    def _model_drifted(self, rows: dict[str, dict]) -> bool:
        live = (str(self._embedder.model_id), int(self._embedder.dim))
        return any((r["model_id"], r["dim"]) != live for r in rows.values())

    def _clear(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute("DELETE FROM memory_vec")
        self._cache = None

    def _touch(self, name: str, mtime_ns: int, size: int) -> None:
        """内容 hash 未变，只刷新文件时间戳（避免下次又走慢路径）。"""
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute(
                "UPDATE memory_vec SET mtime_ns=?, file_size=? WHERE name=?",
                (mtime_ns, size, name),
            )

    def _delete(self, names: list[str]) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.executemany(
                "DELETE FROM memory_vec WHERE name=?", [(n,) for n in names]
            )
        self._cache = None

    def _upsert(self, pending: list[tuple], vectors: Sequence) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for (name, file, digest, mtime_ns, size, _text), vec in zip(
            pending, vectors, strict=False
        ):
            arr = np.asarray(vec, dtype=np.float32)
            rows.append((
                name, file, digest, mtime_ns, size,
                str(self._embedder.model_id), int(arr.size), now,
                arr.tobytes(),
            ))
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO memory_vec"
                " (name, file, text_hash, mtime_ns, file_size, model_id, dim, indexed_at, vector)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise
        self._cache = None

    # ── 检索 ──────────────────────────────────────────────────────

    def scores(self, query: str, top_k: int = 0) -> dict[str, float]:
        """返回若干记忆名 → 余弦相似度（top_k<=0 表示全量）。

        只返回 top_k 是为了避免把整表分数搬进 Python dict：融合排序只需要靠前
        的候选，未出现的记忆在调用方那里分数按 0 处理。
        """
        with self._lock:
            loaded = self._load_matrix()
            if loaded is None:
                return {}
            matrix, names = loaded
            q = self._embed_one(query)
            if q is None:
                return {}
            sims = matrix @ q
            if 0 < top_k < len(names):
                picked = np.argpartition(-sims, top_k - 1)[:top_k]
            else:
                picked = np.arange(len(names))
            return {names[int(i)]: float(sims[int(i)]) for i in picked}

    def near_duplicate_pairs(self, min_sim: float) -> list[tuple[str, str]] | None:
        """返回两两余弦 >= `min_sim` 的记忆名对；索引为空/不可用时返回 None。

        1000 条时 `M @ M.T` 约 4 MB / 1 ms（实测），比把它交给 LLM 便宜好几个量级。
        """
        with self._lock:
            loaded = self._load_matrix()
            if loaded is None:
                return None
            matrix, names = loaded
            if len(names) < 2:
                return []
            sims = matrix @ matrix.T
            iu = np.triu_indices(len(names), k=1)
            hits = sims[iu] >= min_sim
            return [
                (names[int(i)], names[int(j)])
                for i, j in zip(iu[0][hits], iu[1][hits], strict=False)
            ]

    def _load_matrix(self) -> tuple[np.ndarray, list[str]] | None:
        """把全部向量读进内存并缓存（sync 后失效）。"""
        if self._cache is not None:
            return self._cache
        try:
            rows = self._conn.execute(
                "SELECT name, dim, vector FROM memory_vec ORDER BY name"
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("读取记忆向量失败（按无索引处理）：%s", e)
            return None
        names: list[str] = []
        vectors: list[np.ndarray] = []
        expected = 0
        for name, _dim, blob in rows:
            arr = np.frombuffer(blob, dtype=_VECTOR_DTYPE)
            if expected and arr.size != expected:
                logger.warning("记忆索引中 %s 维度异常（%d ≠ %d），已跳过", name, arr.size, expected)
                continue
            expected = arr.size
            names.append(str(name))
            vectors.append(arr)
        if not vectors:
            return None
        matrix = np.stack(vectors).astype(np.float32, copy=False)
        self._cache = (matrix, names)
        return self._cache

    def _embed_one(self, text: str) -> np.ndarray | None:
        try:
            vectors = self._embedder.embed([text])
        except Exception as e:
            logger.warning("查询向量编码失败（本轮退回关键词）：%s: %s", type(e).__name__, e)
            return None
        if not vectors:
            return None
        arr = np.asarray(vectors[0], dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        return arr / norm if norm > 0 else None

    # ── 观测 ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        """索引现状（供日志、验证脚本与测试断言）。"""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(dim), 0), COALESCE(MAX(model_id), '')"
                    " FROM memory_vec"
                ).fetchone()
            except sqlite3.Error:
                row = (0, 0, "")
            return {
                "count": int(row[0]),
                "dim": int(row[1]),
                "model_id": str(row[2]),
                "db_bytes": self._path.stat().st_size if self._path.exists() else 0,
            }


# ── 进程级单例 ────────────────────────────────────────────────────

_INDEX_CACHE: dict[tuple, MemoryVectorIndex] = {}

# 索引库文件名模板。路径**由记忆目录派生**而不是写成全局配置里的固定路径——与
# Phase 17 的备份目录同一条纪律：测试用临时记忆目录时天然隔离，不会往用户的
# ~/.kittymind 写垃圾（少了这条，任何触发提取/召回的测试都会污染真实索引库）。
#
# 名字里带记忆目录名（`memory` → `memory_index.db`）而不是固定叫 `memory_index.db`：
# 两个记忆目录若同处一个父目录，固定名会让它们共用同一个索引库，而索引是**按名字
# 对齐**的——两个库的记忆会互相覆盖，是最难查的那类串味。
_INDEX_DB_SUFFIX = "_index.db"


def index_db_path(memory_dir: str | Path | None = None) -> Path:
    """索引库路径：与记忆目录同级、以记忆目录名派生的独立库。"""
    base = Path(memory_dir) if memory_dir else cfg.MEMORY_DIR
    return base.parent / f"{base.name}{_INDEX_DB_SUFFIX}"


def get_memory_index(memory_dir: str | Path | None = None) -> MemoryVectorIndex | None:
    """返回记忆索引；未配 embedding 或索引库建不起来时返回 None。

    与 `get_aux_llm()` 同构：**失败不缓存**，调用方按 `None` 退化到旧路径。
    配置热更新（settings.json 改了模型 / 维度）会让键变化，此时关掉同库的旧连接
    再新建——否则第一次 `embed` 探测出真实维度后键就变了，会静默泄漏一个连接。
    """
    embedder = get_embedding()
    if embedder is None:
        return None

    db_path = index_db_path(memory_dir)
    key = (str(db_path), embedder.model_id, embedder.dim)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    for stale in [k for k in _INDEX_CACHE if k[0] == key[0] and k != key]:
        _INDEX_CACHE.pop(stale).close()

    try:
        index = MemoryVectorIndex(db_path, embedder)
    except Exception as e:
        logger.warning("记忆索引不可用（本次退回旧路径）：%s: %s", type(e).__name__, e)
        return None
    _INDEX_CACHE[key] = index
    return index


def reset_memory_index() -> None:
    """关闭并清空单例（配置热更新 / 测试隔离时调用）。"""
    for index in _INDEX_CACHE.values():
        index.close()
    _INDEX_CACHE.clear()
