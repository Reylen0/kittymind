"""Phase v2 / P0 验收测试 —— 数据安全（身份、不覆盖、软删、round-trip）与召回留痕。

这一批测的不是"新功能"，而是**旧实现会静默丢数据的三条路径**：

  1. `write("My Note")` 与 `write("my note")` 落到同一个 `my-note.md`，第一条被无声吃掉；
  2. 整合的 `drop` 直接 `path.unlink()`，模型误判一次，记忆就永久消失；
  3. description 含换行 / `: ` / `---` 时被截断、注入伪键或提前结束 frontmatter。

加上一条新的可观测性要求：**每轮召回都要留痕**（否则门控与重排的任何调参都无法评估）。
"""

import types

import pytest

from kittymind.config import cfg
from kittymind.memory import db
from kittymind.memory import extract as ex
from kittymind.memory import schema
from kittymind.memory.db import memory_db_path
from kittymind.memory.ledger import MemoryLedger, get_memory_ledger, read_events
from kittymind.memory.recall import MemoryRecall
from kittymind.memory.store import MemoryStore


@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path / "memory", selfcheck=False)


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return types.SimpleNamespace(content=self._content)


class _NoLLM:
    def invoke(self, messages):
        raise AssertionError("这条路径不该调用 LLM")


def _legacy_doc(name, mem_type, desc, body, updated) -> str:
    """Phase v2 之前的文件格式（字符串拼接 + 单行 key: value）。"""
    return (
        f"---\nname: {name}\ntype: {mem_type}\n"
        f"description: {desc}\nupdated_at: {updated}\n---\n\n{body}\n"
    )


# ── ① frontmatter：不存在"能写进去但读不出来"的文件 ─────────────────


def test_frontmatter_round_trip_survives_hostile_text(mem):
    """description / 正文里塞满 YAML 与老正则的杀手字符，读回来必须一字不差。"""
    body = "第一行\n\n- 列表项\n--- 分隔线\n: 冒号开头的行\n结尾带引号\"与#井号"
    mem.write("hostile", "user", "多行\n描述: 带冒号 --- 分隔 # 井号 [方括号]", body)

    record = mem.read("hostile")
    assert record["body"] == body
    # 描述归一为单行（换行变空格，内容不丢）；超长才截断
    assert record["description"] == "多行 描述: 带冒号 --- 分隔 # 井号 [方括号]"

    raw = (mem.directory / "hostile.md").read_text(encoding="utf-8")
    meta, parsed_body = schema.parse(raw)
    assert meta is not None and meta["name"] == "hostile"
    assert parsed_body == body
    assert raw.split("---")[1].strip().startswith("schema: 2")


def test_v1_file_is_read_by_regex_not_yaml(tmp_path):
    """旧文件必须走正则：`safe_load` 会把 `# 备注` 吃掉、把时间串变成 datetime。"""
    (tmp_path / "old.md").write_text(
        _legacy_doc("old-note", "user", "见 https://a.com/x # 备注", "旧正文", "2026-09-21 17:19:49"),
        encoding="utf-8",
    )
    record = MemoryStore(tmp_path, selfcheck=False).read("old-note")

    assert record["description"] == "见 https://a.com/x # 备注"
    assert record["updated_at"] == "2026-09-21 17:19:49"


def test_frontmatter_type_coercion(tmp_path):
    """`pinned: "false"` 必须是 False —— `bool("false")` 是 True，这类坑最隐蔽。"""
    (tmp_path / "m.md").write_text(
        "---\nschema: 2\nname: t\ntype: user\npinned: \"false\"\nlocal_only: 'true'\n"
        "strength: \"1.5\"\nrecall_count: \"3\"\nrelated: [a, b]\n"
        "source: {via: extract}\n---\n\n正文\n",
        encoding="utf-8",
    )
    meta, _ = schema.parse((tmp_path / "m.md").read_text(encoding="utf-8"))

    assert meta["pinned"] is False
    assert meta["local_only"] is True
    assert meta["strength"] == 1.5
    assert meta["recall_count"] == 3
    assert meta["related"] == ["a", "b"]
    assert meta["source"] == {"via": "extract"}


# ── ② 身份：同名不覆盖、slug 撞车不丢条目 ──────────────────────────


def test_p0_acceptance_hundred_plus_one(mem):
    """验收：100 条互不同名 + 1 条 slug 与首条撞车 → 101 条，旧条内容一字未动。"""
    for i in range(100):
        mem.write(f"mem-{i:03d}", "project", f"第{i}条", f"正文{i}")
    before = mem.read("mem-000")["body"]

    mem.write("MEM 000", "project", "撞车条目", "正文X")   # slug 与 mem-000 相同

    assert mem.count() == 101
    assert mem.read("mem-000")["body"] == before
    assert mem.read("MEM 000")["body"] == "正文X"


def test_slug_collision_uses_distinct_files(mem):
    first = mem.write("My Note", "project", "第一条", "正文A")
    second = mem.write("my note", "project", "第二条", "正文B")

    assert first != second
    assert mem.count() == 2
    assert mem.read("My Note")["body"] == "正文A"
    assert mem.read("my note")["body"] == "正文B"


def test_same_name_write_keeps_id_and_audits_old_body(mem):
    mem.write("主题", "user", "d", "第 0 版")
    memory_id = mem.read("主题")["id"]

    for i in range(1, 5):
        mem.write("主题", "user", "d", f"第 {i} 版")

    record = mem.read("主题")
    assert record["body"] == "第 4 版"
    assert record["id"] == memory_id, "身份不随写入变化"
    assert mem.count() == 1

    updates = [e for e in read_events(mem.directory, memory_id=memory_id)
               if e["event"] == "update"]
    assert len(updates) == 4
    # read_events 新的在前：updates[0] 是最后一次写入，updates[-1] 是最早那次
    assert updates[0]["detail"]["old_body"] == "第 3 版", "旧正文必须留痕，可回溯"
    assert updates[-1]["detail"]["old_body"] == "第 0 版", "最早那次保留最初正文"
    assert {e["detail"]["mode"] for e in updates} == {"replace"}, "默认不合并"
    # hash 也要留痕：内容比对不该靠正文全文
    assert updates[0]["detail"]["new_hash"] == record["content_hash"]


def test_read_accepts_id(mem):
    mem.write("by-id", "project", "d", "正文")
    memory_id = mem.read("by-id")["id"]
    assert mem.read(memory_id)["name"] == "by-id"
    assert mem.read("by-id")["id"] == memory_id


def test_merge_mode_concatenates_bodies(mem):
    mem.write("合并目标", "project", "d", "第一段")
    mem.write("合并目标", "project", "d", "第二段\n第一段", merge=True)
    record = mem.read("合并目标")
    assert record["body"].splitlines() == ["第一段", "第二段"]
    assert record["strength"] > 1.0


# ── ③ 软删：默认动作是归档，不是删除 ───────────────────────────────


def test_delete_is_recoverable(mem):
    mem.write("待删", "project", "d", "正文")
    memory_id = mem.read("待删")["id"]

    assert mem.delete("待删") is True
    assert mem.count() == 0
    assert mem.read("待删") is None
    assert [m["name"] for m in mem.trashed()] == ["待删"]
    assert list((mem.directory / "_trash").glob("*.md")), "墓碑必须在 _trash/ 里"

    assert mem.restore_item("待删") is True
    restored = mem.read("待删")
    assert restored["body"] == "正文"
    assert restored["id"] == memory_id
    assert restored["status"] == "active"
    assert mem.trashed() == [], "恢复后不该留下幽灵墓碑"

    events = [e["event"] for e in read_events(mem.directory, memory_id=memory_id)]
    assert "forget" in events and "restore" in events


def test_soft_deleted_item_leaves_vector_index(mem):
    """软删后条目不在 `all_memories()` 里 → 召回与索引都不会再看到它。"""
    mem.write("要删的", "project", "d", "正文")
    mem.delete("要删的")
    assert mem.all_memories() == []
    assert mem.catalog() == ""


def test_hard_delete_is_explicit_only(mem):
    mem.write("真删", "project", "d", "正文")
    assert mem.hard_delete("真删") is True
    assert mem.count() == 0
    assert mem.trashed() == []


def test_consolidation_drop_is_soft_and_recoverable(mem):
    """整合的 drop 曾经是 `path.unlink()` —— 模型误判一次，记忆永久消失。"""
    mem.write("keep-me", "project", "保留", "正文K")
    mem.write("drop-me", "project", "删除", "正文D")

    ex._consolidate(_FakeLLM('[{"op": "drop", "name": "drop-me"}]'), mem)

    assert mem.read("drop-me") is None
    assert [m["name"] for m in mem.trashed()] == ["drop-me"]

    assert mem.restore_item("drop-me") is True
    assert mem.read("drop-me")["body"] == "正文D"


def test_consolidation_merge_records_target(mem):
    mem.write("target", "project", "目标", "正文T")
    mem.write("source", "project", "来源", "正文S")

    ex._consolidate(
        _FakeLLM('[{"op": "merge", "into": "target", "from": ["source"]}]'), mem,
    )

    assert mem.read("source") is None
    tomb = [m for m in mem.trashed() if m["name"] == "source"]
    assert tomb and tomb[0]["merged_into"] == mem.read("target")["id"]
    body = mem.read("target")["body"]
    assert "正文T" in body and "正文S" in body


# ── ④ 召回留痕：没有它，后面的调参全是拍脑袋 ───────────────────────


def test_recall_log_written_without_embedding(mem, monkeypatch):
    """`keyword` 是零外发的隐私模式，也必须留痕（账本不依赖 embedder）。"""
    mem.write("alpha-note", "project", "存储层设计", "SQLite 迁移链自己实现")
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MODE", "keyword")

    recall = MemoryRecall(mem, _NoLLM())
    recall.bind_session("s-1", turn_seq=7)
    picked = recall.select_relevant("存储层怎么改")

    rows = get_memory_ledger(mem.directory).recent(5, session_id="s-1")
    assert len(rows) == 1
    assert rows[0]["mode"] == "keyword"
    assert rows[0]["turn_seq"] == 7
    assert rows[0]["injected"] == [m["name"] for m in picked]
    assert rows[0]["query_text"] == "存储层怎么改"
    assert rows[0]["latency_ms"] >= 0


def test_recall_log_records_query_and_is_queryable_by_session(mem, monkeypatch):
    mem.write("alpha-note", "project", "存储层设计", "SQLite 迁移链")
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MODE", "keyword")
    recall = MemoryRecall(mem, _NoLLM())

    recall.bind_session("s-A")
    recall.select_relevant("存储")
    recall.bind_session("s-B")
    recall.select_relevant("存储")

    ledger = get_memory_ledger(mem.directory)
    assert len(ledger.recent(10, session_id="s-A")) == 1
    assert len(ledger.recent(10, session_id="s-B")) == 1
    assert len(ledger.recent(10)) == 2


def test_recall_log_never_blocks_recall(mem, monkeypatch):
    """留痕失败不能影响召回结果（账本坏了也只是少一行日志）。

    刻意**不** monkeypatch `_log_recall` 本身：那个 try/except 就在它内部，
    从外边把整个函数换成会抛异常的桩，等于绕过了被测的保护代码——测试会
    因为"桩抛了"而失败，却什么也没验证到。要打的是它**依赖的两个环节**。
    """
    mem.write("alpha-note", "project", "存储层设计", "SQLite 迁移链")
    monkeypatch.setattr(cfg, "MEMORY_RECALL_MODE", "keyword")
    recall = MemoryRecall(mem, _NoLLM())

    # 环节一：账本整个拿不到（建库失败 / 目录只读）
    monkeypatch.setattr("kittymind.memory.recall.get_memory_ledger",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert [m["name"] for m in recall.select_relevant("存储层")] == ["alpha-note"]

    # 环节二：账本拿得到，但写入失败（磁盘满 / 库损坏）
    class _BrokenLedger:
        def log_recall(self, **kwargs):
            raise RuntimeError("disk full")

    monkeypatch.setattr("kittymind.memory.recall.get_memory_ledger",
                        lambda *a, **k: _BrokenLedger())
    assert [m["name"] for m in recall.select_relevant("存储层")] == ["alpha-note"]


def test_recall_log_prune_respects_retention(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    ledger = MemoryLedger(memory_dir, db_path=tmp_path / "memory.db", maintenance=False)
    ledger.log_recall(session_id="s", mode="keyword", query_text="q", injected=["a"])

    with ledger._lock:
        ledger._conn.execute("UPDATE memory_recall_log SET ts = '2020-01-01 00:00:00'")

    assert ledger.prune(30) == 1
    assert ledger.stats()["count"] == 0
    ledger.close()


def test_recall_log_retention_nonpositive_keeps_rows(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    ledger = MemoryLedger(memory_dir, db_path=tmp_path / "memory.db", maintenance=False)
    ledger.log_recall(session_id="s", mode="keyword", query_text="q")
    with ledger._lock:
        ledger._conn.execute("UPDATE memory_recall_log SET ts = '2020-01-01 00:00:00'")

    assert ledger.prune(0) == 0
    assert ledger.stats()["count"] == 1
    ledger.close()


# ── ⑤ 迁移：解析用正则、序列化用 YAML，原字段逐字节一致 ─────────────


def test_migrate_v2_preserves_legacy_values_byte_for_byte(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    legacy_desc = "见 https://a.com/x # 备注"
    (memory_dir / "legacy.md").write_text(
        _legacy_doc("legacy-note", "user", legacy_desc, "旧正文", "2026-09-21 17:19:49"),
        encoding="utf-8",
    )

    store = MemoryStore(memory_dir, selfcheck=False)
    assert store.migrate_v2() == 1

    record = store.read("legacy-note")
    assert record["name"] == "legacy-note"
    assert record["type"] == "user"
    assert record["description"] == legacy_desc, "YAML 会吃掉 `# 备注`，这里必须完整"
    assert record["updated_at"] == "2026-09-21 17:19:49", "不能被推断成 datetime"
    assert record["body"] == "旧正文"
    assert schema.is_valid_id(record["id"])
    assert record["status"] == "active"
    assert record["created_at"], "缺失的创建时间用文件 mtime 兜底"
    assert (memory_dir / "legacy.md").is_file(), "文件名与位置不变（git diff 只有内容变化）"

    assert store.migrate_v2() == 0, "幂等：第二次不应再改写"
    assert (memory_dir / ".v2_migrated").is_file()

    migrate = [e for e in read_events(memory_dir) if e["event"] == "migrate"]
    assert len(migrate) == 1
    # detail 不能再套一层：`append_event(**detail)` 传 `detail={...}` 会写出
    # `detail.detail` —— 真实库上落过这个盘。
    assert migrate[0]["detail"] == {"version": 2, "items": 1}


def test_migrate_v2_is_idempotent_across_reopen(tmp_path):
    memory_dir = tmp_path / "memory"
    store = MemoryStore(memory_dir, selfcheck=False)
    store.write("已迁移", "project", "d", "正文")
    memory_id = store.read("已迁移")["id"]

    assert store.migrate_v2() == 0
    assert MemoryStore(memory_dir, selfcheck=False).migrate_v2() == 0
    assert store.read("已迁移")["id"] == memory_id


def test_migrate_v2_purges_legacy_index_db(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    legacy = tmp_path / "memory_index.db"
    legacy.write_text("x", encoding="utf-8")
    (tmp_path / "memory_index.db-wal").write_text("x", encoding="utf-8")

    MemoryStore(memory_dir, selfcheck=False).migrate_v2()

    assert not legacy.exists()
    assert not (tmp_path / "memory_index.db-wal").exists()
    assert memory_db_path(memory_dir) == tmp_path / "memory.db"


def test_migrate_v2_defers_marker_until_legacy_db_cleaned(tmp_path, monkeypatch):
    """清理没成功就不能落迁移标记。

    标记是最高优先级的快速出口：它一落下，`migrate_v2` 再也不会重进这段逻辑，
    清旧库 / 写审计就永远没有第二次机会。旧索引库被别的进程打开而删不掉，在
    Windows 上是常态（真实库上就出现过"标记写着 items=12、旧库却还在"的形态），
    所以标记必须最后落，失败就当没迁完，下次启动重试。
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "legacy.md").write_text(
        _legacy_doc("legacy-note", "user", "说明", "旧正文", "2026-09-21 17:19:49"),
        encoding="utf-8",
    )
    legacy = tmp_path / "memory_index.db"
    legacy.write_text("x", encoding="utf-8")
    store = MemoryStore(memory_dir, selfcheck=False)

    monkeypatch.setattr(db, "discard_db", lambda _path: None)   # 模拟"删不掉"
    assert store.migrate_v2() == 1
    assert legacy.exists()
    assert not (memory_dir / ".v2_migrated").is_file(), "清理失败却落了标记"

    monkeypatch.undo()                                          # 占用解除
    assert MemoryStore(memory_dir, selfcheck=False).migrate_v2() == 0
    assert not legacy.exists()
    assert (memory_dir / ".v2_migrated").is_file()
    assert [e["event"] for e in read_events(memory_dir)].count("migrate") == 1, "不重复记"


def test_events_jsonl_is_append_only_and_skips_broken_lines(tmp_path):
    memory_dir = tmp_path / "memory"
    store = MemoryStore(memory_dir, selfcheck=False)
    store.write("e1", "project", "d", "正文")
    store.write("e2", "project", "d", "正文")

    path = memory_dir / "events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("这不是 JSON\n")

    events = read_events(memory_dir)
    assert [e["event"] for e in events] == ["create", "create"], "坏行跳过，其余照读"
    assert path.read_text(encoding="utf-8").count("\n") == 3
