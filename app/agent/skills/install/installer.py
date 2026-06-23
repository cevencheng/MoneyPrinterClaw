"""
Skill 安装/卸载：写盘 + .install.json 元数据 + 触发 catalog 热刷新。

安装位置 INSTALL_BASE = ~/.agents/skills/。与 settings.skills_user_dir 对齐
（discovery.py 已支持 user/project 双扫,project 覆盖 user 语义）。

卸载安全：仅删带 .install.json 的目录,绝不删项目内置 skill（无元数据 = 用户手放或 MVP 示例）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TypedDict

from agent.config import settings
from agent.skills.discovery import refresh_skills
from agent.skills.install.preview import (
    PreviewResult,
    _cache_get,
    preview_from_url,
    preview_from_zip_b64,
    preview_from_zip_bytes,
)

logger = logging.getLogger(__name__)


# 安装根目录：~/.agents/skills/（与 settings.skills_user_dir 的默认值约定一致）。
# 启动时 discovery 会同时扫 project + user 目录,所以装在这里即时可被发现。
INSTALL_BASE = Path(os.path.expanduser("~/.agents/skills")).resolve()
_META_FILE = ".install.json"


class InstallResult(TypedDict):
    name: str
    path: str
    env_required: list[str]
    env_already_granted: list[str]
    env_missing: list[str]
    source_type: str
    source_ref: str


def _ensure_install_base() -> None:
    INSTALL_BASE.mkdir(parents=True, exist_ok=True)


def _write_files(skill_dir: Path, files: dict[str, bytes]) -> dict[str, str]:
    """把 files 写入 skill_dir,返回每个文件的 sha256。

    files 的 path 是相对 skill 根目录的（preview._build_preview 已剥外层）。
    再次校验路径不越界 skill_dir（防 preview 之后被攻击者篡改）。
    """
    sha_map: dict[str, str] = {}
    skill_dir_resolved = skill_dir.resolve()
    for rel, content in files.items():
        target = (skill_dir / rel).resolve()
        # 安全检查：必须落在 skill_dir 内
        try:
            target.relative_to(skill_dir_resolved)
        except ValueError as e:
            raise RuntimeError(f"非法路径越界: {rel}") from e
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        sha_map[rel] = hashlib.sha256(content).hexdigest()
    return sha_map


def _filter_skill_root_files(
    files: dict[str, bytes], skill_name: str
) -> dict[str, bytes]:
    """从可能嵌套的 files 提取 skill 真正的根级文件（SKILL.md 同级及其子目录）。

    GitHub 子目录拉下来的 path 已经是相对 subpath 的（无 prefix）。
    但 zip 可能含嵌套结构（如顶层目录已被 extract_zip 剥过,但 SKILL.md 仍可能在 N/SKILL.md）。
    本函数定位 SKILL.md 所在层级,把其同级及以下文件作为最终内容,去掉外层路径。
    """
    skill_md_path = next(p for p in files if p.endswith("SKILL.md"))
    if "/" in skill_md_path:
        skill_prefix = skill_md_path.rsplit("/", 1)[0] + "/"
    else:
        skill_prefix = ""
    result: dict[str, bytes] = {}
    for path, content in files.items():
        if skill_prefix and not path.startswith(skill_prefix):
            continue
        rel = path[len(skill_prefix):] if skill_prefix else path
        if rel:
            result[rel] = content
    return result


async def _install_files(
    files: dict[str, bytes],
    preview: PreviewResult,
) -> InstallResult:
    """通用安装路径：files dict → 写盘 → 写 .install.json → 刷新 catalog。"""
    _ensure_install_base()
    name = preview["name"]
    # 安全校验：name 只能含 a-z 0-9 - _（防恶意 SKILL.md 写 `name: ../../etc/passwd`）
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError(f"非法 skill 名: {name!r}（仅允许字母/数字/连字符/下划线）")

    skill_dir = (INSTALL_BASE / name).resolve()
    try:
        skill_dir.relative_to(INSTALL_BASE)
    except ValueError as e:
        raise ValueError(f"非法 skill 路径越界: {name!r}") from e

    if skill_dir.exists():
        raise FileExistsError(
            f"skill '{name}' 已安装在 {skill_dir}；请先卸载再装,或换一个不同 name 的版本"
        )

    # 剥外层 + 写盘
    skill_files = _filter_skill_root_files(files, name)
    skill_dir.mkdir(parents=True)
    try:
        sha_map = _write_files(skill_dir, skill_files)
    except Exception:
        # 部分写入后失败 → 整体回滚
        shutil.rmtree(skill_dir, ignore_errors=True)
        raise

    # 写元数据
    meta = {
        "name": name,
        "version": preview["version"],
        "source_type": preview["source_type"],
        "source_ref": preview["source_ref"],
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requires_env": list(preview["requires_env"]),
        "files_sha256": sha_map,
    }
    (skill_dir / _META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 热刷新 catalog → 主 agent 下一轮对话即可用
    await asyncio.to_thread(refresh_skills)

    # 计算 env 授权差异
    required = list(preview["requires_env"])
    allowlist = set(settings.skills_env_allowlist or [])
    granted = [k for k in required if k in allowlist]
    missing = [k for k in required if k not in allowlist]

    logger.info(
        "[skills/install] 安装完成 name=%s dir=%s files=%d env_required=%s env_missing=%s",
        name, skill_dir, len(skill_files), required, missing,
    )

    return {
        "name": name,
        "path": str(skill_dir),
        "env_required": required,
        "env_already_granted": granted,
        "env_missing": missing,
        "source_type": preview["source_type"],
        "source_ref": preview["source_ref"],
    }


async def install_from_url(url: str, *, token: str | None = None) -> InstallResult:
    """从 GitHub 子目录 URL 安装。优先用 preview 缓存,缺则重新拉。"""
    cached = _cache_get(url)
    if cached:
        preview, files = cached
    else:
        preview = await preview_from_url(url, token=token)
        # 缓存里现在应该有了
        cached = _cache_get(url)
        if not cached:
            raise RuntimeError("preview 缓存未命中,这是 bug")
        _, files = cached
    return await _install_files(files, preview)


async def install_from_zip_bytes(zip_bytes: bytes) -> InstallResult:
    """从 zip bytes 安装。"""
    sha = hashlib.sha256(zip_bytes).hexdigest()
    cached = _cache_get(sha)
    if cached:
        preview, files = cached
    else:
        preview = preview_from_zip_bytes(zip_bytes)
        cached = _cache_get(sha)
        if not cached:
            raise RuntimeError("preview 缓存未命中,这是 bug")
        _, files = cached
    return await _install_files(files, preview)


async def install_from_zip_b64(zip_b64: str) -> InstallResult:
    """前端 base64 上传的 zip。"""
    import base64
    try:
        zip_bytes = base64.b64decode(zip_b64, validate=True)
    except Exception as e:
        raise ValueError(f"zip base64 解码失败: {e}") from e
    return await install_from_zip_bytes(zip_bytes)


async def uninstall(name: str) -> dict:
    """卸载 user 级 skill（仅删带 .install.json 的目录）。

    Raises:
        FileNotFoundError: skill 目录不存在。
        PermissionError: 目录无 .install.json（说明是 project 级或用户手放,不删）。
    """
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError(f"非法 skill 名: {name!r}")

    skill_dir = (INSTALL_BASE / name).resolve()
    try:
        skill_dir.relative_to(INSTALL_BASE)
    except ValueError as e:
        raise ValueError(f"非法 skill 路径越界: {name!r}") from e

    if not skill_dir.is_dir():
        raise FileNotFoundError(f"skill '{name}' 未安装在 {INSTALL_BASE}")

    meta_file = skill_dir / _META_FILE
    if not meta_file.is_file():
        raise PermissionError(
            f"skill '{name}' 无 {_META_FILE} 元数据,可能是项目内置示例或用户手放,卸载被拒绝。"
            f"如需删除请手动 rm -rf {skill_dir}"
        )

    await asyncio.to_thread(shutil.rmtree, skill_dir)
    await asyncio.to_thread(refresh_skills)
    logger.info("[skills/install] 卸载完成 name=%s", name)
    return {"name": name, "removed": True}
