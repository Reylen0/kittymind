"""全文检索的存储层测试（`session/store.py` 的 FTS5 索引与 search）。

覆盖三类风险：

  1. **索引范围**：只有 user/assistant 正文进索引。role='tool' 的 content 是
     工具原始输出（文件全文、bash stdout），一旦进了索引，搜索结果会被工具
     回显淹没，索引也会比 messages 表本身还大。
  2. **压缩后仍可搜**：被压缩掉的中段消息仍以 compacted=1 留在库里，搜索走
     完整视图，必须照样命中——只搜活跃消息的话，长会话的搜索基本等于失效。
     同时摘要行不得重复命中（它是原文的浓缩，两边都索引会出现重影）。
  3. **删除即消失**：删会话后其内容必须立刻搜不到。这条靠 SQL 触发器 + 外键
     级联实现，是隐私相关的硬要求，不是锦上添花。

外加一条迁移测试：v5 老库升上来要自动建表并回填存量消息，否则老用户升级后
「搜不到任何历史」——而历史恰恰是最需要搜的部分。
"""

import sqlite3
from pathlib import Path

import pytest

from kittymind.session.manager import SessionManager
from kittymind.session.store import SqliteSessionStore, _SCHEMA_VERSION


_CREATED = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    return SqliteSessionStore(tmp_path / "test.db")


def _seed(store, session_id="s1", title="会话", rows=()):
    store.write_header(session_id, {"title": title, "created_at": _CREATED})
    for i, (role, content) in enumerate(rows):
        store.append(session_id, {"seq": i, "role": role, "content": content})


# ── 基本检索 ──────────────────────────────────────────────────────

def test_chinese_and_english_both_searchable(store):
    _seed(store, rows=[
        ("user", "上下文压缩管线用的是多层阈值"),
        ("assistant", "已经用 async 重构了工具执行层"),
    ])

    assert len(store.search("压缩")) == 1           # 2 字中文
    assert len(store.search("阈值")) == 1
    assert len(store.search("上下文")) == 1         # 3 字中文
    assert len(store.search("async")) == 1          # 英文
    assert store.search("量子纠缠") == []           # 无关词


def test_search_returns_seq_and_title_and_original_content(store):
    """返回的是**原文**，不是索引里的二元组串；且带 seq 供前端定位。"""
    _seed(store, title="我的会话", rows=[("user", "上下文压缩")])

    hit = store.search("压缩")[0]
    assert hit["content"] == "上下文压缩", "返回了索引文本而不是原文"
    assert hit["seq"] == 0
    assert hit["title"] == "我的会话"
    assert hit["role"] == "user"
    assert hit["session_id"] == "s1"


def test_cross_word_bigram_does_not_false_positive(store):
    """「压缩，管线」被标点断开，不该造出跨段的「缩管」token。"""
    _seed(store, rows=[("user", "压缩，管线")])
    assert store.search("缩管") == []


def test_empty_query_returns_empty_not_error(store):
    """输入框清空是正常状态，不是错误——不能让空表达式打到 MATCH 上。"""
    _seed(store, rows=[("user", "随便什么内容")])
    for bad in ("", "   ", "\n\t"):
        assert store.search(bad) == []


def test_hostile_query_does_not_raise(store):
    _seed(store, rows=[("user", "正常内容")])
    for hostile in ('foo*', 'a AND b', '"', '((', 'NEAR(x y)', '-x'):
        store.search(hostile)          # 不抛异常即可


# ── 索引范围 ──────────────────────────────────────────────────────

def test_tool_output_is_not_indexed(store):
    """role='tool' 的正文不进索引，否则搜索结果会被工具回显淹没。"""
    store.write_header("s1", {"title": "t", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "user", "content": "帮我找找压缩相关的"})
    store.append("s1", {"seq": 1, "role": "tool", "tool_call_id": "c1",
                        "content": "压缩 压缩 压缩 grep 输出里全是这个词"})

    hits = store.search("压缩")
    assert len(hits) == 1
    assert hits[0]["role"] == "user"


def test_empty_content_is_not_indexed(store):
    """assistant 只发工具调用的那一轮 content 为 None，跳过即可，不该报错。"""
    store.write_header("s1", {"title": "t", "created_at": _CREATED})
    store.append("s1", {"seq": 0, "role": "assistant", "content": None,
                        "tool_calls": [{"id": "c1", "function": {"name": "ls", "arguments": "{}"}}]})
    store.append("s1", {"seq": 1, "role": "user", "content": "看看目录"})

    assert store.fts_row_count() == 1


# ── 压缩交互 ──────────────────────────────────────────────────────

def test_compacted_messages_remain_searchable(store):
    """压缩掉的中段仍在库里，必须照样搜得到——否则长会话搜索等于失效。"""
    _seed(store, rows=[
        ("user", "第一条消息谈的是向量检索"),
        ("user", "中间这条谈的是布隆过滤器"),
        ("user", "最后一条谈的是倒排索引"),
    ])

    store.archive_and_compact(
        "s1",
        compacted_seqs={1},
        summary_rows=[{"seq": 1.5, "role": "assistant", "content": "聊了一些数据结构"}],
        new_active_msgs=[],
    )

    hits = store.search("布隆")
    assert len(hits) == 1, "被压缩的原文搜不到了"
    assert hits[0]["content"] == "中间这条谈的是布隆过滤器"


def test_summary_rows_are_not_indexed(store):
    """摘要行不进索引：原文还在 compacted 行里，两边都索引会出现重影。"""
    _seed(store, rows=[("user", "谈的是布隆过滤器")])
    before = store.fts_row_count()

    store.archive_and_compact(
        "s1",
        compacted_seqs={0},
        summary_rows=[{"seq": 0.5, "role": "assistant", "content": "聊了布隆过滤器"}],
        new_active_msgs=[],
    )

    assert store.fts_row_count() == before, "摘要行被索引了"
    assert len(store.search("布隆")) == 1, "同一内容命中了两次"


def test_new_messages_in_compaction_are_indexed(store):
    """压缩事务里追加的新消息同样要进索引（走的是另一条写入路径）。"""
    _seed(store, rows=[("user", "旧内容")])

    store.archive_and_compact(
        "s1", compacted_seqs=set(), summary_rows=[],
        new_active_msgs=[{"role": "user", "content": "新写入的内容谈到了光栅化"}],
    )

    assert len(store.search("光栅")) == 1


# ── 删除 ──────────────────────────────────────────────────────────

def test_deleting_session_purges_its_index_rows(store):
    """删会话必须连索引一起清干净——留着等于「删了还能被搜出来」。"""
    _seed(store, "s1", rows=[("user", "机密内容涉及量子隧穿")])
    _seed(store, "s2", rows=[("user", "另一个会话谈的是编译原理")])
    assert store.fts_row_count() == 2

    store.delete("s1")

    assert store.search("量子") == []
    assert store.fts_row_count() == 1, "级联删除没有清掉 FTS 行"
    assert len(store.search("编译")) == 1, "误删了其他会话的索引"


# ── 作用域 ────────────────────────────────────────────────────────

def test_session_id_narrows_the_search(store):
    _seed(store, "s1", rows=[("user", "两个会话都提到了缓存")])
    _seed(store, "s2", rows=[("user", "这里也提到了缓存")])

    assert len(store.search("缓存")) == 2
    assert len(store.search("缓存", session_id="s1")) == 1


def test_limit_caps_results(store):
    _seed(store, rows=[("user", f"第{i}条都含有关键词檢索") for i in range(10)])
    assert len(store.search("檢索", limit=3)) == 3


# ── 迁移与回填 ────────────────────────────────────────────────────

def _make_v5_db(path: Path) -> None:
    """造一个 v5 老库（有 is_summary，但没有 messages_fts）并塞几条历史消息。"""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',
        workspace_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        compressed_once INTEGER NOT NULL DEFAULT 0, last_prompt_tokens INTEGER);
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq REAL NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT,
        tool_call_id TEXT, ts INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0,
        is_summary INTEGER NOT NULL DEFAULT 0);
    """)
    conn.execute(
        "INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','旧会话',?,?)",
        (_CREATED, _CREATED),
    )
    for i, (role, content) in enumerate([
        ("user", "很久以前聊过的向量检索"),
        ("assistant", "当时的结论是先做倒排"),
        ("tool", "工具输出不该被回填进索引"),
    ]):
        conn.execute(
            "INSERT INTO messages(session_id,seq,role,content) VALUES('s1',?,?,?)",
            (i, role, content),
        )
    conn.execute("PRAGMA user_version=5")
    conn.commit()
    conn.close()


def test_v5_db_gets_fts_table_and_backfill(tmp_path):
    """老库升级后历史消息要能立刻搜到，否则「升级后搜不到任何旧内容」。"""
    db = tmp_path / "old.db"
    _make_v5_db(db)

    store = SqliteSessionStore(db)

    assert sqlite3.connect(str(db)).execute(
        "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
    assert store.fts_row_count() == 2, "回填条数不对（tool 消息不该进索引）"
    assert len(store.search("向量")) == 1
    assert store.search("工具输出") == [], "tool 消息被回填进索引了"


def test_backfill_runs_only_once(tmp_path):
    """反复打开同一个库不得重复回填（会造成同一内容多次命中）。"""
    db = tmp_path / "old.db"
    _make_v5_db(db)

    SqliteSessionStore(db).close()
    store = SqliteSessionStore(db)

    assert store.fts_row_count() == 2, "重复打开导致索引被回填了两遍"
    assert len(store.search("向量")) == 1


def test_fresh_db_has_working_index(tmp_path):
    """全新库走的是 _DDL 路径而非迁移路径，索引同样要能用。"""
    store = SqliteSessionStore(tmp_path / "fresh.db")
    _seed(store, rows=[("user", "全新库里的压缩讨论")])
    assert len(store.search("压缩")) == 1


# ── 片段与高亮（manager 层）───────────────────────────────────────

@pytest.fixture
def mgr(tmp_path):
    return SessionManager(db_path=tmp_path / "test.db")


def _mgr_seed(mgr, session_id, title, rows):
    mgr.create_session(title=title, session_id=session_id)
    mgr.append_turn(session_id, [{"role": r, "content": c} for r, c in rows])


def test_marks_point_at_the_query_word(mgr):
    """高亮区间必须落在片段里那个词上——错一位就是标歪。"""
    _mgr_seed(mgr, "s1", "会话", [("user", "我们来聊聊上下文压缩的实现")])

    hit = mgr.search_messages("压缩")[0]["hits"][0]
    start, end = hit["marks"][0]
    assert hit["text"][start:end] == "压缩"


def test_long_content_is_trimmed_around_the_hit(mgr):
    """命中词在长文中间时，片段以它为中心截取，两端加省略号。"""
    body = "前文" * 200 + "布隆过滤器" + "后文" * 200
    _mgr_seed(mgr, "s1", "会话", [("user", body)])

    hit = mgr.search_messages("布隆")[0]["hits"][0]
    assert len(hit["text"]) < 120, "片段没有被截断"
    assert hit["text"].startswith("…") and hit["text"].endswith("…")
    start, end = hit["marks"][0]
    assert hit["text"][start:end] == "布隆", "截断后高亮偏移没跟着修正"


def test_snippet_is_single_lined(mgr):
    """多行原文压成一行——侧栏那条窄列表项放不下换行。"""
    _mgr_seed(mgr, "s1", "会话", [("user", "第一行讲缓存\n\n第二行讲别的\t还有制表符")])

    hit = mgr.search_messages("缓存")[0]["hits"][0]
    assert "\n" not in hit["text"] and "\t" not in hit["text"]


def test_results_are_grouped_by_session(mgr):
    _mgr_seed(mgr, "s1", "第一个会话", [("user", "这里提到了倒排索引"),
                                        ("assistant", "倒排索引确实合适")])
    _mgr_seed(mgr, "s2", "第二个会话", [("user", "这里也提到了倒排索引")])

    groups = mgr.search_messages("倒排")

    assert {g["session_id"] for g in groups} == {"s1", "s2"}
    by_id = {g["session_id"]: g for g in groups}
    assert by_id["s1"]["title"] == "第一个会话"
    assert len(by_id["s1"]["hits"]) == 2
    assert len(by_id["s2"]["hits"]) == 1


def test_hits_carry_seq_for_navigation(mgr):
    """前端要靠 seq 定位/翻页，这个字段不能丢。"""
    _mgr_seed(mgr, "s1", "会话", [("user", "第一条"), ("assistant", "提到了光栅化")])

    hit = mgr.search_messages("光栅")[0]["hits"][0]
    assert hit["seq"] == 1
    assert hit["role"] == "assistant"


def test_empty_query_returns_no_groups(mgr):
    _mgr_seed(mgr, "s1", "会话", [("user", "随便什么")])
    assert mgr.search_messages("") == []


def test_overlapping_terms_produce_disjoint_marks(mgr):
    """两个词标到同一段文字上时区间要合并，否则前端切分会切出交错碎片。"""
    _mgr_seed(mgr, "s1", "会话", [("user", "上下文压缩")])

    hit = mgr.search_messages("上下文 下文压")[0]["hits"][0]
    marks = hit["marks"]
    for (_, prev_end), (next_start, _) in zip(marks, marks[1:]):
        assert prev_end <= next_start, "高亮区间出现重叠"
