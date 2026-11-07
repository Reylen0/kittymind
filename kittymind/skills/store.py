"""技能文档存储：文件系统为真相源，`~/.kittymind/skills/<slug>/SKILL.md` 一个
技能一个文件。更新语义是"整篇替换正文"，不做记忆系统那套按行合并——技能是
版本化文档，不是可追加的记忆片段。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import cfg
from ..storage import atomic_write_text
from . import schema

logger = logging.getLogger(__name__)


class SkillStore:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory is not None else cfg.SKILLS_DIR
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        return self.directory / schema.slugify(name) / "SKILL.md"

    # ── 读 ───────────────────────────────────────────────────────

    def list_skills(self) -> list[dict]:
        """扫描全部技能的 frontmatter（不读正文，省 IO）。

        单个技能文件损坏（frontmatter 解析失败）只记日志跳过，不中断整体扫描
        （与 MCP loader 的"单点失败不拖垮整体"同一原则）。
        """
        if not self.directory.is_dir():
            return []
        result: list[dict] = []
        for manifest in sorted(self.directory.glob("*/SKILL.md")):
            if not manifest.is_file():
                continue
            try:
                manifest.resolve().relative_to(self.directory.resolve())
            except ValueError:
                continue
            try:
                content = manifest.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("技能文件读取失败 %s: %s", manifest, e)
                continue
            meta, _body = schema.parse(content)
            if meta is None:
                logger.warning("技能文件 frontmatter 解析失败，已跳过: %s", manifest)
                continue
            result.append(meta)
        return result

    def catalog(self) -> str:
        """技能目录文本：`- **name**: description` 列表，与 `build_system_prompt`
        给工具列表用的格式对齐。空列表返回 `""`。"""
        skills = self.list_skills()
        if not skills:
            return ""
        limit = cfg.SKILL_CATALOG_MAX_DESC_CHARS
        lines = []
        for s in skills:
            desc = str(s.get("description") or "")
            if len(desc) > limit:
                desc = desc[:limit].rstrip() + "…"
            lines.append(f"- **{s.get('name')}**：{desc}")
        return "\n".join(lines)

    def read(self, name: str) -> dict | None:
        """按名称读取完整技能（frontmatter + 正文）。找不到/损坏返回 None。"""
        path = self._path_for(name)
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("技能文件读取失败 %s: %s", path, e)
            return None
        meta, body = schema.parse(content)
        if meta is None:
            logger.warning("技能文件 frontmatter 解析失败: %s", path)
            return None
        return {**meta, "body": body}

    # ── 写 ───────────────────────────────────────────────────────

    def write(self, name: str, description: str, body: str) -> Path:
        """新建或整篇替换一个技能（更新语义是"替换正文"，不合并）。"""
        path = self._path_for(name)
        meta = {
            "name": name,
            "description": schema.normalize_description(description),
        }
        content = schema.document(meta, body)
        atomic_write_text(path, content)
        logger.info("写入技能: %s", path)
        return path
