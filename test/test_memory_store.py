"""记忆文件存储的健壮性测试（Phase 17）。

覆盖两件此前会静默出事的行为：
  1. 写入是原子的——不能留下半截 Markdown（frontmatter 断裂 = 这条记忆永久读不出来）；
  2. 解析不出来的文件会被**隔离可见**，而不是在 catalog 里静默消失。
"""

import pytest

from kittymind.memory.store import MemoryStore

_VALID = """---
name: 用户偏好
type: user
description: 喜欢深色主题
updated_at: 2026-01-01 00:00:00
---

正文内容
"""


@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path, selfcheck=False)


def test_write_creates_file_and_index(tmp_path, mem):
    path = mem.write("用户偏好深色主题", "user", "喜欢深色", "正文")
    assert path.is_file()
    assert mem.read("用户偏好深色主题")["body"] == "正文"
    assert "用户偏好深色主题" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_write_leaves_no_temp_file(tmp_path, mem):
    """原子替换的中间文件不能留在目录里（否则会被索引/快照扫到）。"""
    mem.write("主题", "user", "d", "b")
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_write_is_repeatable_without_corrupting(tmp_path, mem):
    for i in range(5):
        mem.write("主题", "user", "d", f"第 {i} 版")
    record = mem.read("主题")
    assert record["body"] == "第 4 版"
    assert mem.count() == 1


def test_selfcheck_quarantines_unparsable_file(tmp_path):
    (tmp_path / "good.md").write_text(_VALID, encoding="utf-8")
    (tmp_path / "broken.md").write_text("裸文本，没有 frontmatter", encoding="utf-8")

    store = MemoryStore(tmp_path)          # 构造即自检

    assert store.count() == 1
    assert store.read("用户偏好")["body"] == "正文内容"
    quarantined = list((tmp_path / "_quarantine").glob("*.md"))
    assert [p.name for p in quarantined] == ["broken.md"]
    # 原文保留（改名而不是删除）：手工修好还能移回来
    assert "裸文本" in quarantined[0].read_text(encoding="utf-8")
    # 索引里不应再出现它
    assert "broken" not in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_selfcheck_quarantine_does_not_overwrite(tmp_path):
    """同名的坏文件隔离两次不能互相覆盖（第二次加时间戳）。"""
    (tmp_path / "x.md").write_text("坏 1", encoding="utf-8")
    MemoryStore(tmp_path)
    (tmp_path / "x.md").write_text("坏 2", encoding="utf-8")
    MemoryStore(tmp_path)

    names = sorted(p.name for p in (tmp_path / "_quarantine").glob("*.md"))
    assert len(names) == 2, "第二次隔离必须另存一份，不能覆盖第一份"
    assert "x.md" in names
    assert any(n.startswith("x-") for n in names)


def test_selfcheck_keeps_valid_files_untouched(tmp_path):
    (tmp_path / "good.md").write_text(_VALID, encoding="utf-8")
    store = MemoryStore(tmp_path)
    assert store.selfcheck() == []
    assert store.count() == 1


def test_selfcheck_on_missing_dir_is_noop(tmp_path):
    assert MemoryStore(tmp_path / "nope").selfcheck() == []


def test_quarantined_files_are_not_listed(tmp_path):
    (tmp_path / "broken.md").write_text("坏", encoding="utf-8")
    store = MemoryStore(tmp_path)
    assert store.all_memories() == []
    assert store.snapshot() == {}
    assert store.count() == 0


def test_restore_rebuilds_snapshot_atomically(tmp_path, mem):
    mem.write("a", "user", "d1", "one")
    snap = mem.snapshot()

    mem.write("b", "project", "d2", "two")
    assert mem.count() == 2

    mem.restore(snap)
    assert mem.count() == 1
    assert mem.read("a")["body"] == "one"
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_catalog_lists_only_valid_memories(tmp_path):
    (tmp_path / "good.md").write_text(_VALID, encoding="utf-8")
    (tmp_path / "broken.md").write_text("坏", encoding="utf-8")
    store = MemoryStore(tmp_path)
    catalog = store.catalog()
    assert "用户偏好" in catalog
    assert "broken" not in catalog
