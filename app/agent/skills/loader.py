"""
Skill 激活：读 SKILL.md 正文 + 列资源清单（不预读内容）。

`activate(name)` 由 activate_skill 工具调用,返回 dict;tools 层负责拼成
对 LLM 友好的标签包裹文本（含 requires-env 是否在白名单的 warning）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from agent.skills.discovery import SkillMeta, get_catalog

logger = logging.getLogger(__name__)

_RESOURCE_SUBDIRS = ("scripts", "references", "assets")


def _find_meta(name: str) -> SkillMeta | None:
    """从当前 catalog 找到指定 skill。"""
    for m in get_catalog():
        if m.name == name:
            return m
    return None


def activate(name: str) -> dict | None:
    """加载 skill 完整指令 + 资源清单。

    Returns:
        {"meta": SkillMeta, "content": "SKILL.md 正文（已剥 frontmatter）",
         "resources": {"scripts": [...], "references": [...], "assets": [...]}}
        skill 不存在或 SKILL.md 读取失败 → None。
    """
    meta = _find_meta(name)
    if not meta:
        return None

    skill_md = Path(meta.location) / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("[skills/loader] %s 读取失败: %s", skill_md, e)
        return None

    content = _strip_frontmatter(text)
    resources = _list_resources(Path(meta.location))

    return {"meta": meta, "content": content, "resources": resources}


def _strip_frontmatter(text: str) -> str:
    """剥掉 SKILL.md 的 YAML frontmatter,只留正文。"""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def _list_resources(skill_dir: Path) -> dict[str, list[str]]:
    """列出 scripts/references/assets 下的文件清单（递归一级,跳过隐藏文件）。"""
    result: dict[str, list[str]] = {sub: [] for sub in _RESOURCE_SUBDIRS}
    for sub in _RESOURCE_SUBDIRS:
        sub_dir = skill_dir / sub
        if not sub_dir.is_dir():
            continue
        try:
            for entry in sorted(sub_dir.rglob("*")):
                if not entry.is_file():
                    continue
                # 跳过隐藏文件
                if any(p.startswith(".") for p in entry.relative_to(sub_dir).parts):
                    continue
                # 相对 skill_dir 的路径,前端 LLM 看到方便引用
                rel = entry.relative_to(skill_dir).as_posix()
                result[sub].append(rel)
        except OSError as e:
            logger.debug("[skills/loader] 列资源 %s 失败: %s", sub_dir, e)
    return result
