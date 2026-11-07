"""技能系统单元测试（ARCHITECTURE.md Phase 25）。

覆盖三块：
  1. schema.py 的 frontmatter round-trip + slugify 边界（只有 name/description
     两个元信息字段，没有 id/created_at/source 等）；
  2. SkillStore 的写/读/目录/损坏文件容错；
  3. LoadSkillTool / WriteSkillTool 两个工具的端到端协同。
"""

import pytest

from kittymind.skills import schema
from kittymind.skills.store import SkillStore


# ── schema.py ────────────────────────────────────────────────────

def test_document_round_trips_through_parse():
    meta = {"name": "git-commit-flow", "description": "规范化提交流程"}
    content = schema.document(meta, "正文内容")
    parsed_meta, body = schema.parse(content)
    assert parsed_meta["name"] == "git-commit-flow"
    assert parsed_meta["description"] == "规范化提交流程"
    assert body == "正文内容"


def test_parse_without_frontmatter_returns_none_meta():
    meta, body = schema.parse("裸文本，没有 frontmatter")
    assert meta is None
    assert body == "裸文本，没有 frontmatter"


def test_dumps_rejects_non_roundtrippable_payload(monkeypatch):
    """round-trip 校验是关键防线：safe_load(safe_dump(x)) != x 时必须拒绝写入。"""
    import yaml

    monkeypatch.setattr(yaml, "safe_load", lambda *_a, **_k: {"not": "matching"})
    with pytest.raises(ValueError, match="round-trip"):
        schema.dumps({"name": "x"})


@pytest.mark.parametrize("raw,expected", [
    ("Git Commit Flow!", "git-commit-flow"),
    ("  多个   空格  ", "多个-空格"),
    ("", "skill"),
    ("a" * 100, "a" * 64),
])
def test_slugify(raw, expected):
    assert schema.slugify(raw) == expected


# ── SkillStore ───────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return SkillStore(tmp_path / "skills")


def test_write_then_read_round_trips(store):
    store.write("git-commit-flow", "规范化提交流程", "## 步骤\n1. git status")
    skill = store.read("git-commit-flow")
    assert skill["description"] == "规范化提交流程"
    assert skill["body"] == "## 步骤\n1. git status"


def test_write_is_whole_document_replace_not_merge(store):
    """技能更新语义是整篇替换——与记忆系统的 merge=True 按行合并刻意不同。"""
    store.write("x", "d1", "旧正文")
    store.write("x", "d2", "新正文")
    skill = store.read("x")
    assert skill["body"] == "新正文"
    assert skill["description"] == "d2"
    assert "旧正文" not in skill["body"]


def test_read_missing_skill_returns_none(store):
    assert store.read("does-not-exist") is None


def test_catalog_lists_name_and_description(store):
    store.write("a", "desc-a", "body")
    store.write("b", "desc-b", "body")
    catalog = store.catalog()
    assert "**a**" in catalog and "desc-a" in catalog
    assert "**b**" in catalog and "desc-b" in catalog


def test_catalog_truncates_long_description(store, monkeypatch):
    from kittymind.config import cfg
    monkeypatch.setattr(cfg, "SKILL_CATALOG_MAX_DESC_CHARS", 10)
    store.write("x", "a" * 50, "body")
    catalog = store.catalog()
    assert "a" * 11 not in catalog  # 截断后不应出现超过限制+省略号之前的原文长串


def test_catalog_empty_when_no_skills(store):
    assert store.catalog() == ""


def test_list_skills_skips_corrupted_file_without_crashing(tmp_path):
    store = SkillStore(tmp_path / "skills")
    store.write("good", "d", "body")
    broken_dir = tmp_path / "skills" / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "SKILL.md").write_text("裸文本，没有 frontmatter", encoding="utf-8")

    skills = store.list_skills()
    assert [s["name"] for s in skills] == ["good"]


# ── LoadSkillTool / WriteSkillTool ────────────────────────────────

@pytest.fixture
def tools(store):
    from kittymind.tools.builtin.load_skill_tool import LoadSkillInput, LoadSkillTool
    from kittymind.tools.builtin.write_skill_tool import WriteSkillInput, WriteSkillTool

    return (
        WriteSkillTool(store), WriteSkillInput,
        LoadSkillTool(store), LoadSkillInput,
    )


def test_write_skill_then_load_skill_round_trips(tools):
    write_tool, WriteInput, load_tool, LoadInput = tools

    write_result = write_tool.execute(WriteInput(
        name="git-commit-flow", description="规范化提交流程", body="## 步骤\n1. git status",
    ))
    assert write_result.ok
    assert "git-commit-flow" in write_result.content

    load_result = load_tool.execute(LoadInput(name="git-commit-flow"))
    assert load_result.ok
    assert load_result.content == "## 步骤\n1. git status"


def test_load_skill_missing_returns_failure_with_catalog(tools, store):
    _write_tool, _WriteInput, load_tool, LoadInput = tools
    store.write("existing-skill", "desc", "body")

    result = load_tool.execute(LoadInput(name="does-not-exist"))
    assert not result.ok
    assert "does-not-exist" in result.content
    assert "existing-skill" in result.content  # 失败文案里回显可用目录，方便模型纠错


def test_write_skill_overwrites_body_not_merges(tools):
    write_tool, WriteInput, load_tool, LoadInput = tools
    write_tool.execute(WriteInput(name="x", description="d1", body="第一版"))
    write_tool.execute(WriteInput(name="x", description="d2", body="第二版"))

    result = load_tool.execute(LoadInput(name="x"))
    assert result.content == "第二版"
