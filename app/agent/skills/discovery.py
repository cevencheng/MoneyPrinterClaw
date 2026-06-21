"""
Skills 发现与 COW 缓存。

启动时扫 `.agents/skills/` 找含 SKILL.md 的子目录,解析 YAML frontmatter 取
{name, description, requires-env}。同名 project 覆盖 user。

COW 缓存（地雷4修正）：
- 模块级 `_CATALOG_CACHE` 是不可变快照（list[SkillMeta]）。
- `refresh_skills()` 在局部组装新 list,最后一行 `_CATALOG_CACHE = new_cache` 原子替换。
- GIL 保证赋值原子,读者要么看旧要么看新,零锁争用。
- 绝不用 dict.clear() + dict.update() 模式（迭代中改会 RuntimeError）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent.config import settings

logger = logging.getLogger(__name__)


# 相对路径锚点：固定到 app/ 目录（= 本文件向上 3 层 agent/skills/discovery.py → app/）。
# 不依赖 cwd,启动方式或测试入口不同也能稳定解析,与 settings 字段注释「相对 app/ 工作目录」语义对齐。
_APP_ROOT = Path(__file__).resolve().parents[2]


# ---- 扫描安全限位 ----
_MAX_SCAN_DEPTH = 4           # 子目录递归深度
_MAX_SCAN_DIRS = 2000         # 总目录数上限,防扫到巨型仓库假死
_SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}


@dataclass(frozen=True)
class SkillMeta:
    """单个 skill 的元信息（catalog 项）。

    frozen=True：dataclass 不可变,配合 COW 缓存语义——quote 出去的 SkillMeta
    引用永远是原值,不会被并发刷新冲掉字段。
    """
    name: str
    description: str
    location: str   # skill 目录绝对路径
    version: str = ""
    requires_env: tuple[str, ...] = field(default_factory=tuple)


# COW 缓存：模块级单一全局变量,只通过原子赋值更新（非 mutate）
_CATALOG_CACHE: list[SkillMeta] = []


def get_catalog() -> list[SkillMeta]:
    """只读访问缓存当前快照。

    返回的是当前 _CATALOG_CACHE 引用——调用方拿到后即使并发触发了
    refresh_skills() 也不影响它的迭代（COW 语义,旧 list 仍在内存）。
    """
    return _CATALOG_CACHE


def refresh_skills() -> int:
    """重扫所有 skill 目录,原子替换缓存。返回当前缓存条目数。

    扫描完整 list 后一行赋值替换 `_CATALOG_CACHE`,GIL 保证读者侧无竞态。
    """
    if not settings.skills_enabled:
        global _CATALOG_CACHE
        _CATALOG_CACHE = []
        return 0
    new_cache = discover_skills()
    # 原子指针替换：读者要么看旧 list 要么看新 list,绝不会看到部分填充态
    globals()["_CATALOG_CACHE"] = new_cache
    logger.info("[skills/discovery] refresh 完成,catalog=%d 个 skill", len(new_cache))
    return len(new_cache)


def discover_skills() -> list[SkillMeta]:
    """扫描 project + user 目录,返回去重后的 SkillMeta 列表（project 覆盖 user）。

    扫描顺序：user → project,后扫的同名 skill 覆盖前者,实现「project 优先」。
    """
    by_name: dict[str, SkillMeta] = {}

    # user 先扫
    user_dir = settings.skills_user_dir.strip()
    if user_dir:
        user_path = Path(os.path.expanduser(user_dir))
        if user_path.is_dir():
            for meta in _scan_dir(user_path):
                by_name[meta.name] = meta

    # project 后扫（覆盖 user）
    project_dir = settings.skills_project_dir.strip()
    if project_dir:
        # 相对路径以 app/ 为锚点（_APP_ROOT）,不依赖 cwd。绝对路径直接用。
        project_path = Path(project_dir) if os.path.isabs(project_dir) else _APP_ROOT / project_dir
        if project_path.is_dir():
            for meta in _scan_dir(project_path):
                by_name[meta.name] = meta

    return list(by_name.values())


def _scan_dir(root: Path) -> list[SkillMeta]:
    """扫描单个根目录,深度/数量受限,找含 SKILL.md 的子目录解析。"""
    result: list[SkillMeta] = []
    dir_count = 0
    root_abs = root.resolve()

    def _walk(p: Path, depth: int) -> None:
        nonlocal dir_count
        if depth > _MAX_SCAN_DEPTH or dir_count > _MAX_SCAN_DIRS:
            return
        try:
            entries = list(p.iterdir())
        except (PermissionError, OSError) as e:
            logger.debug("[skills/discovery] 跳过不可读目录 %s: %s", p, e)
            return
        # 当前层有 SKILL.md → 解析为 skill,不再下钻
        skill_md = p / "SKILL.md"
        if skill_md.is_file():
            meta = _parse_skill_md(skill_md, root_abs)
            if meta:
                result.append(meta)
            return
        # 否则递归子目录
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name in _SKIP_DIR_NAMES or entry.name.startswith("."):
                # `.agents/skills/` 自身允许；隐藏子目录跳过
                if entry.name != "skills":  # 防把 .agents/skills 自己 skip 掉
                    continue
            dir_count += 1
            _walk(entry, depth + 1)

    _walk(root, 0)
    return result


def _parse_skill_md(skill_md: Path, scan_root: Path) -> SkillMeta | None:
    """解析单个 SKILL.md 的 YAML frontmatter。宽松校验：

    - description 空 → skip（return None,对齐 agentskills 标准）。
    - name 与父目录名不符 → warn 但仍以 frontmatter name 收录。
    - requires-env 字段（list[str]）可选,声明 skill 需要的环境变量,由 activate_skill
      工具时核对 skills_env_allowlist 决定是否 warn 用户。
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("[skills/discovery] 读 %s 失败: %s", skill_md, e)
        return None

    # 拆 frontmatter：必须以 --- 开头,第二个 --- 之前是 YAML
    if not text.startswith("---"):
        logger.warning("[skills/discovery] %s 无 frontmatter,跳过", skill_md)
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning("[skills/discovery] %s frontmatter 未闭合,跳过", skill_md)
        return None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        logger.warning("[skills/discovery] %s frontmatter YAML 解析失败: %s", skill_md, e)
        return None
    if not isinstance(fm, dict):
        logger.warning("[skills/discovery] %s frontmatter 不是 dict,跳过", skill_md)
        return None

    name = str(fm.get("name", "") or "").strip()
    description = str(fm.get("description", "") or "").strip()
    if not description:
        logger.warning("[skills/discovery] %s description 为空,跳过", skill_md)
        return None

    parent_name = skill_md.parent.name
    if name and name != parent_name:
        logger.warning(
            "[skills/discovery] %s frontmatter name=%r ≠ 父目录名 %r,以 frontmatter 为准",
            skill_md, name, parent_name,
        )
    elif not name:
        # frontmatter 没写 name → 用父目录名兜底（agentskills 标准允许）
        name = parent_name

    version = str(fm.get("version", "") or "").strip()

    # requires-env：list[str],声明 skill 需要的环境变量
    raw_env = fm.get("requires-env") or fm.get("requires_env") or []
    if isinstance(raw_env, str):
        raw_env = [raw_env]
    requires_env: tuple[str, ...] = tuple(str(x).strip() for x in raw_env if str(x).strip())

    return SkillMeta(
        name=name,
        description=description,
        location=str(skill_md.parent.resolve()),
        version=version,
        requires_env=requires_env,
    )
