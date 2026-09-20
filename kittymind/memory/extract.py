"""
记忆提取与整合 —— 会话结束后自动从对话中提取值得长期保存的信息。

流程：
  1. LLM 扫描对话，返回候选记忆列表（含 scope 字段）
  2. _should_store() 过滤：拒绝 current_task、字段不完整、类型非法、已存在同名记忆
  3. 写入磁盘，触发索引重建
  4. 记忆数量达到阈值时自动整合（LLM 判断合并/删除，正文由本地拼接）

整合的两条设计约束（都是踩坑换来的）：
  - **模型不碰转义**：模型只输出「保留/合并/删除」指令，不输出正文，
    因此不必自行完成 `\\"` / `\\n` 转义（旧实现让它重写多行正文，一次
    漏转义整批就失败）。见 _consolidate。
  - **未被提及 = 保留**：只执行模型显式给出的删除/合并，漏写不会丢记忆。

整合有快照/回滚保护，解析或写盘失败时恢复原始文件。
"""

import json
import logging
from pathlib import Path

from .store import MEMORY_TYPES
from ..config import cfg
from ..core.llm_json import extract_json_text, log_parse_failure
from ..prompts import build_memory_extract_prompt, build_memory_consolidate_prompt

logger = logging.getLogger(__name__)


def extract_memories(messages: list[dict], llm, memory_store) -> bool:
    """
    从对话历史中提取候选记忆并写入磁盘。
    使用同步 llm.invoke()，应在 asyncio.to_thread() 中调用。
    返回 True 表示至少写入了一条记忆。
    """
    history_text = _format_conversation(messages)
    if not history_text.strip():
        return False

    existing_catalog = memory_store.catalog()
    candidates = _llm_extract(history_text, llm, existing_catalog)
    if not candidates:
        return False

    wrote_any = False
    for candidate in candidates:
        if not _should_store(candidate, memory_store):
            continue
        try:
            memory_store.write(
                name=candidate["name"],
                mem_type=candidate["type"],
                description=candidate["description"],
                body=candidate["body"],
            )
            logger.info("提取记忆: %s", candidate["name"])
            wrote_any = True
        except Exception as e:
            logger.warning("记忆写入失败: %s", e)

    if wrote_any and memory_store.count() >= cfg.MEMORY_CONSOLIDATE_THRESHOLD:
        _consolidate(llm, memory_store)

    return wrote_any


def _consolidate(llm, memory_store) -> None:
    """让模型只产出「保留 / 合并 / 删除」指令，记忆正文由本地拼接。

    旧实现把每条记忆（含多行 Markdown 正文、半角引号）原文拼进 prompt，要求
    模型把整批记忆原样吐回 JSON 字符串——模型必须自行完成 `\\"` 与 `\\n` 转义，
    一处漏掉整批整合就失败（现场报错：Expecting ',' delimiter: line 11
    column 30）。而且旧实现先 `delete_all()` 再按模型输出重建，模型少写一条
    就会**静默丢记忆**（不抛异常，快照也不会回滚）。

    现在模型只回短指令：名字是 ASCII slug、描述单行，转义风险基本消失；
    未被任何指令提及的记忆原样保留，漏写不再等于丢失。
    """
    memories = memory_store.all_memories()
    if len(memories) < 2:
        return

    by_name = {m["name"]: m for m in memories if m.get("name")}
    if len(by_name) < 2:
        return

    records_text = "\n\n".join(
        f"[{m.get('type')}] {m.get('name')}: {_to_cjk_quotes(str(m.get('description', '')))}\n"
        f"{_to_cjk_quotes(str(m.get('body', '')))}"
        for m in memories if m.get("name")
    )
    prompt = build_memory_consolidate_prompt(records_text)

    snap = memory_store.snapshot()
    text = ""
    try:
        response = llm.invoke([{"role": "user", "content": prompt}])
        text = response.content or ""

        payload = _extract_json_array(text)
        if payload is None:
            log_parse_failure("记忆整合", text, "输出里找不到 JSON 数组，跳过本次整合")
            _dump_raw(text, "no-json-array")
            return

        ops = json.loads(payload)
        if not isinstance(ops, list):
            log_parse_failure("记忆整合", text, "输出不是 JSON 数组，跳过本次整合")
            _dump_raw(text, "not-an-array")
            return

        updates, deletions = _plan_ops(ops, by_name)
        for item in updates:  # 先写后删：合并来源的正文已被并进目标
            memory_store.write(
                item["name"], item["type"], item["description"], item["body"],
            )
        for name in deletions:
            if not memory_store.delete(name):
                logger.warning("记忆整合：删除未生效（未找到文件）%s", name)
        logger.info(
            "记忆整合: %d → %d 条（重写 %d 条，移除 %d 条）",
            len(memories), memory_store.count(), len(updates), len(deletions),
        )

    except Exception as e:
        logger.warning("记忆整合失败，已回滚到快照: %s: %s", type(e).__name__, e)
        _dump_raw(text, f"{type(e).__name__}: {e}")
        if snap:
            memory_store.restore(snap)


def _plan_ops(ops: list, by_name: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """把模型的操作指令翻译成可执行的「重写清单」与「移除清单」（纯函数，便于单测）。

    任何一条指令非法都只忽略该条并记日志——整合是后台增强，个别指令有问题
    不该影响其余记忆的安全。返回 (要重写的记忆, 要从磁盘移除的名字)。
    """
    bodies = {n: str(m.get("body", "")) for n, m in by_name.items()}
    descriptions = {n: str(m.get("description", "")) for n, m in by_name.items()}
    rewritten: set[str] = set()   # 正文被合并改写过的名字
    gone: set[str] = set()        # 需要从磁盘移除的名字

    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")

        if kind == "keep":
            continue  # 未提及的记忆本来就原样保留，这里无需动作

        if kind == "drop":
            name = op.get("name")
            if name in by_name and name not in gone and name not in rewritten:
                gone.add(name)
            else:
                logger.debug("记忆整合：忽略无效 drop 指令 %r", op)
            continue

        if kind == "merge":
            into = op.get("into")
            # into 本身已被并走（在 gone 里）时该指令自相矛盾，忽略
            if into not in by_name or into in gone:
                logger.debug("记忆整合：忽略无效 merge 指令 %r", op)
                continue
            raw_sources = op.get("from")
            sources = raw_sources if isinstance(raw_sources, list) else []
            valid = [
                s for s in sources
                if isinstance(s, str) and s in by_name
                and s != into and s not in gone and s not in rewritten
            ]
            if not valid:
                logger.debug("记忆整合：merge 没有可并入的来源 %r", op)
                continue
            bodies[into] = _merge_bodies([bodies[into], *(bodies[s] for s in valid)])
            description = op.get("description")
            if isinstance(description, str) and description.strip():
                descriptions[into] = _to_cjk_quotes(description.strip())[:200]
            rewritten.add(into)
            gone.update(valid)
            continue

        logger.debug("记忆整合：忽略未知指令 %r", op)

    updates = [
        {
            "name": n,
            "type": by_name[n].get("type"),
            "description": descriptions[n],
            "body": bodies[n],
        }
        for n in sorted(rewritten) if n not in gone
    ]
    return updates, sorted(gone)


def _merge_bodies(bodies: list[str]) -> str:
    """按行拼接多段正文并去重（合并正文不再由模型改写，避免转义与信息歪曲）。"""
    seen: set[str] = set()
    lines: list[str] = []
    for body in bodies:
        for raw in body.splitlines():
            key = raw.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(raw.rstrip())
    return "\n".join(lines)


def _to_cjk_quotes(text: str) -> str:
    """把半角双引号换成中文引号（成对交替）。

    现有记忆正文里常带半角 `"`（如 `每次回复结尾加上"喵！"`）。这些字符会随
    prompt 进入模型上下文，成为它输出 JSON 时可直接照抄的裸引号——是整合
    失败的直接诱因。输入侧先归一化，模型就没有可照抄的裸引号了。
    """
    out: list[str] = []
    opening = True
    for ch in text:
        if ch == '"':
            out.append("“" if opening else "”")
            opening = not opening
        else:
            out.append(ch)
    return "".join(out)


def _extract_json_array(text: str) -> str | None:
    """从模型输出里取出 JSON 数组文本（见 core.llm_json 的统一实现）。"""
    return extract_json_text(text)


def _dump_raw(text: str, reason: str) -> None:
    """整合失败时把模型原始输出落盘，便于事后定位（不参与记忆索引）。"""
    try:
        path = Path(cfg.MEMORY_DIR).parent / "consolidate-failed-raw.txt"
        path.write_text(f"# {reason}\n\n{text}\n", encoding="utf-8")
        logger.warning("记忆整合：模型原始输出已写入 %s", path)
    except Exception as e:
        logger.debug("记忆整合：原始输出落盘失败: %s", e)


def _llm_extract(history_text: str, llm, existing_catalog: str = "") -> list[dict]:
    prompt = build_memory_extract_prompt(history_text, existing_catalog)
    text = ""
    try:
        response = llm.invoke([{"role": "user", "content": prompt}])
        text = response.content or ""
        payload = _extract_json_array(text)
        if payload is None:
            log_parse_failure("记忆提取", text, "输出里找不到 JSON 数组")
            return []
        candidates = json.loads(payload)
        return [c for c in candidates if isinstance(c, dict)]
    except Exception as e:
        # 旧实现静默吞掉异常，提取失败完全不可见；这里至少留下一条带原文的日志。
        log_parse_failure("记忆提取", text, f"{type(e).__name__}: {e}")
        return []


def _should_store(candidate: dict, memory_store) -> bool:
    if candidate.get("scope") != "persistent":
        return False
    if not all(candidate.get(k) for k in ("name", "type", "description", "body")):
        return False
    if candidate.get("type") not in MEMORY_TYPES:
        return False
    if memory_store.read(candidate["name"]):
        return False  # 同名记忆已存在，不覆盖（用 write_memory 工具显式更新）
    return True


def _format_conversation(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        content = str(msg.get("content") or "")
        if role == "user":
            lines.append(f"用户：{content[:500]}")
        elif role == "assistant" and content:
            lines.append(f"Agent：{content[:500]}")
        elif role == "tool":
            lines.append(f"工具结果：{content[:200]}")
    return "\n".join(lines)[:cfg.MEMORY_MAX_HISTORY_CHARS]
