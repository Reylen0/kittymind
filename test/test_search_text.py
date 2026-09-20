"""检索文本预处理的单元测试（`session/_search_text.py`）。

这一层是全文搜索的地基：入库切词与查询切词**必须用同一套规则**，
一旦两侧不一致，索引建得再对也永远匹配不上。

本文件重点钉三件事：
  1. 中文 2 字词能被切出来（这是选二元组而不是 trigram 的全部理由——
     trigram 要求查询词 ≥3 字符，「压缩」「阈值」这类词在它那里是 0 命中）；
  2. 中英混排时不跨语言边界组二元组；
  3. `to_match_expr` 是安全边界：任何用户输入都只能变成字面短语，
     绝不能让 FTS5 把它当语法解析。
"""

import sqlite3

import pytest

from kittymind.session._search_text import query_terms, to_match_expr, to_search_text


# ── to_search_text：切词规则 ──────────────────────────────────────

def test_cjk_becomes_overlapping_bigrams():
    assert to_search_text("上下文压缩") == "上下 下文 文压 压缩"


def test_single_cjk_char_is_kept_as_is():
    """单个汉字组不出二元组，原样保留——否则「猫」这类单字词直接消失。"""
    assert to_search_text("猫") == "猫"


def test_ascii_passes_through_untouched():
    """英文交给 unicode61 按空白切即可，不该被这层动过。"""
    assert to_search_text("hello async world") == "hello async world"


def test_mixed_text_does_not_bridge_language_boundary():
    """中英交界处不组二元组：`用async` 不该产出「用a」这种跨语言 token。"""
    out = to_search_text("用 async 重构压缩")
    assert "async" in out.split()
    assert "重构" in out.split()
    assert "构压" in out.split()          # 「重构压缩」内部照常滑窗
    assert not any("a" in t and len(t) == 2 and "一" <= t[0] <= "鿿"
                   for t in out.split()), "出现了跨语言边界的二元组"


def test_empty_and_whitespace_give_empty_string():
    for bad in ("", "   ", "\n\t"):
        assert to_search_text(bad) == ""


def test_punctuation_between_cjk_splits_segments():
    """标点会断开 CJK 段——「压缩，管线」不该产出跨标点的「缩管」。"""
    assert "缩管" not in to_search_text("压缩，管线").split()


# ── to_match_expr：查询表达式 + 安全边界 ──────────────────────────

def test_two_char_chinese_query_survives():
    """选二元组方案的核心理由：2 字中文查询必须切得出 token。"""
    assert to_match_expr("压缩") == '"压缩"'


def test_multi_token_query_joins_with_and():
    assert to_match_expr("上下文") == '"上下" AND "下文"'


def test_empty_query_returns_empty_string():
    """空串是「没有可搜的词」的信号；调用方据此短路，不得交给 MATCH。"""
    for bad in ("", "   ", "\n"):
        assert to_match_expr(bad) == ""


def test_embedded_quote_is_doubled():
    """FTS5 短语内的双引号要转义成两个，否则短语被提前闭合、语法崩掉。"""
    assert to_match_expr('say"hi') == '"say""hi"'


@pytest.mark.parametrize("hostile", [
    'foo*', 'a AND b', 'a OR b', 'NEAR(x y)', '"', '((', '-bar', 'x^2',
    'a" OR "1', '*', 'AND', '{}', 'col:val',
])
def test_hostile_input_never_breaks_match(hostile):
    """任何用户输入都必须能安全地交给 MATCH——这是注入防线，不是格式化。

    真的建一张 FTS5 表来跑，而不是只断言字符串形状：语法是否合法只有
    SQLite 自己说了算，字符串断言会漏掉真实的解析行为。
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(body, tokenize='unicode61')")
    con.execute("INSERT INTO t VALUES (?)", (to_search_text("无关内容 some text"),))

    expr = to_match_expr(hostile)
    if not expr:
        return                                  # 切不出 token，调用方会短路
    con.execute("SELECT rowid FROM t WHERE t MATCH ?", (expr,)).fetchall()


def test_fts5_operators_are_treated_as_literals():
    """`a AND b` 里的 AND 是用户打的字，不该变成布尔运算符。"""
    assert to_match_expr("a AND b") == '"a" AND "AND" AND "b"'


# ── query_terms：高亮定位用的原始词 ───────────────────────────────

def test_query_terms_keeps_cjk_segment_whole():
    """高亮要标「压缩」整个词，不是拆成「压」「缩」到处乱标。"""
    assert query_terms("压缩") == ["压缩"]


def test_query_terms_splits_mixed_input():
    assert query_terms("压缩 async") == ["压缩", "async"]


def test_query_terms_drops_empty_pieces():
    assert query_terms("   ") == []
