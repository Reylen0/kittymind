"""会话全文检索的文本预处理（入库与查询共用同一套切词）。

为什么要自己切词，而不是用 FTS5 自带的分词器：

  - `unicode61`（默认）按空白/标点切分，中文整句会变成一个巨型 token，
    等于完全搜不了；
  - `trigram` 看似能救中文，但它要求查询词**至少 3 个字符**——实测「压缩」
    「权限」「阈值」这类 2 字词全部 0 命中，而中文检索里 2 字词恰恰是主力，
    等于功能半废。

所以入库前把 CJK 段展开成重叠二元组（「上下文压缩」→「上下 下文 文压 压缩」），
再交给 unicode61 按空白切。查询词走同一个函数，「压缩」切出来还是「压缩」，
正好命中索引里的那个 token。非 CJK 段原样穿过，英文照常按词匹配。

注意：索引里存的是本模块产出的文本，**不是原文**。原文仍在 messages.content，
搜索命中后按 rowid JOIN 回去取——所以 FTS5 的 snippet()/highlight() 在这里
用不了（它们只会还给你二元组串），片段与高亮由 manager 层基于原文自己算。
"""

import re

# CJK 统一表意文字 + 扩展 A + 日文假名 + 韩文音节
_CJK = re.compile(r"[一-鿿㐀-䶿぀-ヿ가-힯]+")


def to_search_text(text: str) -> str:
    """把原文转成可被 unicode61 正确切分的检索文本。

    CJK 段展开为重叠二元组（单字段原样保留），其余片段原样穿过。
    入库与查询都走这个函数——两侧切法必须一致，否则永远匹配不上。
    """
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for m in _CJK.finditer(text):
        parts.append(text[last:m.start()])
        seg = m.group()
        parts.append(
            seg if len(seg) == 1
            else " ".join(seg[i:i + 2] for i in range(len(seg) - 1))
        )
        last = m.end()
    parts.append(text[last:])
    return " ".join(" ".join(parts).split())


def to_match_expr(query: str) -> str:
    """把用户输入转成 FTS5 MATCH 表达式；空查询（或只有空白）返回空串。

    每个 token 都包成**字面短语**——内部双引号按 FTS5 规则转义成两个——
    所以用户打的 `AND` / `OR` / `NEAR` / `*` / `(` 一律当普通文字，不参与语法。

    这是安全边界，不是格式化：调用方拿到结果必须直接交给 MATCH 占位符，
    不得再做任何字符串拼接。空串表示「没有可搜的词」，调用方应直接返回空结果，
    绝不能把空表达式交给 MATCH（那会抛 fts5: syntax error）。
    """
    tokens = to_search_text(query).split()
    if not tokens:
        return ""
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def query_terms(query: str) -> list[str]:
    """取出用于**高亮定位**的原始词（不做二元组展开）。

    与 `to_search_text` 的分工：那个是给索引匹配用的，这个是给「在原文里找
    高亮位置」用的。中文按 CJK 段整段保留（搜「压缩」就高亮「压缩」两个字，
    而不是拆成「压」「缩」到处乱标），英文按空白切。
    """
    terms: list[str] = []
    last = 0
    for m in _CJK.finditer(query):
        terms.extend(query[last:m.start()].split())
        terms.append(m.group())
        last = m.end()
    terms.extend(query[last:].split())
    return [t for t in terms if t]
