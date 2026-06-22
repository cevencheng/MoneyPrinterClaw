"""
Skill 装前审查：解析 frontmatter + 列脚本清单 + 抽前 N 行预览。

让用户在确认安装前能看到「这个 skill 包含什么代码、要哪些环境变量」。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TypedDict

import yaml

from agent.skills.install.source import (
    extract_zip,
    fetch_github_subdir,
    parse_github_subdir_url,
)

logger = logging.getLogger(__name__)


# 缓存：先 preview 后 install 两步不重复拉 GitHub。TTL 10 分钟。
_PREVIEW_CACHE: dict[str, tuple[float, "PreviewResult", dict[str, bytes]]] = {}
_PREVIEW_TTL = 600.0
# 脚本预览行数上限
_SCRIPT_PREVIEW_LINES = 50


class ScriptPreview(TypedDict):
    path: str          # 相对 skill 根目录,如 'scripts/extract.py'
    head: str          # 前 N 行内容
    total_lines: int   # 文件总行数


class PreviewResult(TypedDict):
    name: str
    description: str
    version: str
    requires_env: list[str]
    file_count: int
    script_count: int
    scripts_preview: list[ScriptPreview]
    source_type: str   # "github" | "zip"
    source_ref: str    # URL 或 zip SHA256 摘要,key 引用缓存用


def _parse_skill_md_bytes(content: bytes) -> dict:
    """从 SKILL.md 内容解析 frontmatter。

    Returns:
        {"name", "description", "version", "requires_env"} 全填,空时给默认值。

    Raises:
        ValueError: frontmatter 缺失或格式错。
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"SKILL.md 不是 UTF-8: {e}") from e

    if not text.startswith("---"):
        raise ValueError("SKILL.md 无 YAML frontmatter（须以 --- 开头）")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter 未闭合（缺第二个 ---）")
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"SKILL.md frontmatter YAML 解析失败: {e}") from e
    if not isinstance(fm, dict):
        raise ValueError("SKILL.md frontmatter 不是 dict 结构")

    name = str(fm.get("name", "") or "").strip()
    description = str(fm.get("description", "") or "").strip()
    if not description:
        raise ValueError("SKILL.md frontmatter 缺 description 字段")

    raw_env = fm.get("requires-env") or fm.get("requires_env") or []
    if isinstance(raw_env, str):
        raw_env = [raw_env]
    requires_env = [str(x).strip() for x in raw_env if str(x).strip()]

    return {
        "name": name,
        "description": description,
        "version": str(fm.get("version", "") or "").strip(),
        "requires_env": requires_env,
    }


def _build_preview(files: dict[str, bytes], source_type: str, source_ref: str) -> PreviewResult:
    """从 files dict 构造 PreviewResult,并把 files 缓存供后续 install 复用。"""
    # 找 SKILL.md（容忍位于嵌套目录中,取第一个）
    skill_md_path = next(p for p in files if p.endswith("SKILL.md"))
    fm = _parse_skill_md_bytes(files[skill_md_path])

    # name 缺省时用 SKILL.md 父目录名兜底
    name = fm["name"]
    if not name:
        if "/" in skill_md_path:
            name = skill_md_path.rsplit("/", 2)[-2]
        else:
            name = "unnamed"

    # 收集 scripts/ 下文件 + 抽前 N 行预览
    scripts_preview: list[ScriptPreview] = []
    skill_dir = skill_md_path.rsplit("SKILL.md", 1)[0]  # 'scripts/' or 'pdf/scripts/...'
    scripts_prefix = f"{skill_dir}scripts/"
    for path, content in sorted(files.items()):
        if not path.startswith(scripts_prefix):
            continue
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            continue
        lines = text.split("\n")
        head = "\n".join(lines[:_SCRIPT_PREVIEW_LINES])
        scripts_preview.append({
            "path": path[len(skill_dir):],  # 相对 skill 根目录
            "head": head,
            "total_lines": len(lines),
        })

    result: PreviewResult = {
        "name": name,
        "description": fm["description"],
        "version": fm["version"],
        "requires_env": fm["requires_env"],
        "file_count": len(files),
        "script_count": len(scripts_preview),
        "scripts_preview": scripts_preview,
        "source_type": source_type,
        "source_ref": source_ref,
    }
    # 缓存,供后续 install 复用（避免重复拉 GitHub / 重复解压）
    _cache_put(source_ref, result, files)
    return result


def _cache_put(key: str, result: PreviewResult, files: dict[str, bytes]) -> None:
    """带 TTL 的缓存写入。"""
    # 顺手清理过期项（小流量场景这样做够用）
    now = _now()
    expired = [k for k, (ts, _, _) in _PREVIEW_CACHE.items() if now - ts > _PREVIEW_TTL]
    for k in expired:
        _PREVIEW_CACHE.pop(k, None)
    _PREVIEW_CACHE[key] = (now, result, files)


def _cache_get(key: str) -> tuple[PreviewResult, dict[str, bytes]] | None:
    entry = _PREVIEW_CACHE.get(key)
    if not entry:
        return None
    ts, result, files = entry
    if _now() - ts > _PREVIEW_TTL:
        _PREVIEW_CACHE.pop(key, None)
        return None
    return result, files


def _now() -> float:
    return time.time()


async def preview_from_url(url: str, *, token: str | None = None) -> PreviewResult:
    """从 GitHub 子目录 URL 预览 skill。"""
    cached = _cache_get(url)
    if cached:
        logger.debug("[skills/install/preview] 命中缓存 %s", url)
        return cached[0]
    owner, repo, ref, subpath = parse_github_subdir_url(url)
    files = await fetch_github_subdir(owner, repo, ref, subpath, token=token)
    return _build_preview(files, "github", url)


def preview_from_zip_bytes(zip_bytes: bytes) -> PreviewResult:
    """从 zip bytes 预览 skill。"""
    sha = hashlib.sha256(zip_bytes).hexdigest()
    cached = _cache_get(sha)
    if cached:
        logger.debug("[skills/install/preview] 命中 zip 缓存 sha=%s", sha[:8])
        return cached[0]
    files = extract_zip(zip_bytes)
    return _build_preview(files, "zip", sha)


def preview_from_zip_b64(zip_b64: str) -> PreviewResult:
    """前端 base64 上传的 zip。"""
    import base64
    try:
        zip_bytes = base64.b64decode(zip_b64, validate=True)
    except Exception as e:
        raise ValueError(f"zip base64 解码失败: {e}") from e
    return preview_from_zip_bytes(zip_bytes)
