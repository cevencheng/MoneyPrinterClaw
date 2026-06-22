"""
Skill 多源数据获取：GitHub 子目录 + 本地 zip。

返回统一格式：dict[相对路径, bytes]。**所有返回都已通过安全校验**：
- 无 `..` 路径段（防遍历）
- 至少含 1 个 SKILL.md（防误装非 skill 目录）
- zip 总大小 ≤10MB + 文件数 ≤200（防 zip bomb）
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import zipfile
from urllib.parse import unquote, urlparse

import httpx

from agent.config import settings

logger = logging.getLogger(__name__)


# 安全限位
_MAX_ZIP_BYTES = 10 * 1024 * 1024   # zip 解压总大小上限 10MB
_MAX_ZIP_FILES = 200                # 文件数上限,防 zip slip / zip bomb
_GITHUB_API = "https://api.github.com"

# GitHub URL 模式: https://github.com/<owner>/<repo>/tree/<ref>/<subpath>
_GITHUB_SUBDIR_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tree/(?P<ref>[^/]+)/(?P<subpath>.+?)/?$"
)


def parse_github_subdir_url(url: str) -> tuple[str, str, str, str]:
    """解析 GitHub 子目录 URL。

    Returns:
        (owner, repo, ref, subpath)。subpath 已 URL 解码,如 'skills/pdf-tools'。

    Raises:
        ValueError: URL 格式不符（须为 https://github.com/o/r/tree/ref/sub/path）。
    """
    m = _GITHUB_SUBDIR_RE.match(url.strip())
    if not m:
        raise ValueError(
            f"invalid github subdir url: {url!r}; "
            "expected https://github.com/<owner>/<repo>/tree/<ref>/<subpath>"
        )
    return (
        m.group("owner"),
        m.group("repo"),
        m.group("ref"),
        unquote(m.group("subpath")),
    )


def _safe_relative_path(path: str) -> str:
    """校验路径无 `..` / 绝对路径段,返回 posix 形式的相对路径。

    Raises:
        ValueError: 路径含非法段。
    """
    if not path or not path.strip():
        raise ValueError(f"unsafe path: {path!r}")
    p = path.replace("\\", "/")
    # 拒绝绝对路径（以 / 开头或含盘符如 C:）
    if p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
        raise ValueError(f"unsafe path (absolute): {path!r}")
    p = p.strip("/")
    if not p or any(seg in ("..", ".") or not seg for seg in p.split("/")):
        raise ValueError(f"unsafe path: {path!r}")
    return p


def _validate_skill_files(files: dict[str, bytes]) -> None:
    """校验文件集合：至少 1 个 SKILL.md;无非法路径。"""
    has_skill_md = any(p.endswith("SKILL.md") for p in files)
    if not has_skill_md:
        raise ValueError("not a valid skill: missing SKILL.md")


async def fetch_github_subdir(
    owner: str,
    repo: str,
    ref: str,
    subpath: str,
    *,
    token: str | None = None,
) -> dict[str, bytes]:
    """拉 GitHub 子目录所有文件,返回 dict[相对 subpath 路径, bytes]。

    实现：1 次 trees API（recursive=1）拿整树清单 → 多次 contents API 并发拉文件内容。

    Args:
        token: GitHub Personal Access Token；None 时走匿名,有 60 次/小时速率限。
    """
    token = token or settings.github_token or None
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    sub = subpath.strip("/")

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        # 1. 拿整树（recursive=1）
        tree_url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
        try:
            resp = await client.get(tree_url)
        except httpx.HTTPError as e:
            raise RuntimeError(f"GitHub trees API 请求失败: {type(e).__name__}: {e}") from e
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise RuntimeError(
                "GitHub API 速率限制（60 次/小时,无 token）。请稍后重试或在 config.toml 配 github_token。"
            )
        if resp.status_code == 404:
            raise RuntimeError(f"GitHub 路径不存在: {owner}/{repo}@{ref}（仓库/分支/标签错误）")
        resp.raise_for_status()
        tree = resp.json()
        if tree.get("truncated"):
            logger.warning("[skills/install/source] GitHub trees 截断,可能漏文件（仓库过大）")

        # 2. 过滤 subpath 下的 blob
        prefix = f"{sub}/" if sub else ""
        blob_paths: list[str] = []
        for node in tree.get("tree", []):
            path = node.get("path", "")
            if node.get("type") == "blob" and (not prefix or path.startswith(prefix)):
                blob_paths.append(path)
        if not blob_paths:
            raise RuntimeError(f"GitHub 子目录无文件: {sub} in {owner}/{repo}@{ref}")
        if len(blob_paths) > _MAX_ZIP_FILES:
            raise RuntimeError(
                f"skill 文件过多（{len(blob_paths)} > {_MAX_ZIP_FILES}）,拒绝安装"
            )

        # 3. 并发拉文件内容（用 contents API,响应是 base64 编码 content）
        async def _fetch_one(path: str) -> tuple[str, bytes]:
            content_url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
            r = await client.get(content_url)
            r.raise_for_status()
            data = r.json()
            if data.get("encoding") != "base64":
                raise RuntimeError(f"GitHub contents 非 base64 编码: {path}")
            return path, base64.b64decode(data["content"])

        # 并发上限 8（避免触发 secondary rate limit）
        sem = asyncio.Semaphore(8)

        async def _bounded(path: str) -> tuple[str, bytes]:
            async with sem:
                return await _fetch_one(path)

        try:
            results = await asyncio.gather(*[_bounded(p) for p in blob_paths])
        except httpx.HTTPError as e:
            raise RuntimeError(f"GitHub contents API 请求失败: {type(e).__name__}: {e}") from e

    # 4. 整理 path：去掉 subpath 前缀,得到相对路径；累计校验大小
    total_bytes = 0
    files: dict[str, bytes] = {}
    for path, content in results:
        rel = path[len(prefix):] if prefix else path
        rel = _safe_relative_path(rel)
        total_bytes += len(content)
        if total_bytes > _MAX_ZIP_BYTES:
            raise RuntimeError(
                f"skill 总大小超过 {_MAX_ZIP_BYTES} bytes,拒绝安装（防 zip bomb）"
            )
        files[rel] = content

    _validate_skill_files(files)
    logger.info(
        "[skills/install/source] github 拉取完成 %s/%s@%s/%s: %d 文件 %d B",
        owner, repo, ref, sub, len(files), total_bytes,
    )
    return files


def extract_zip(zip_bytes: bytes) -> dict[str, bytes]:
    """解压 zip 包到 dict[相对路径, bytes]。

    安全：拒绝 `..` 段、绝对路径、总大小 > _MAX_ZIP_BYTES、文件数 > _MAX_ZIP_FILES。
    自动剥离 zip 中常见的单一顶层目录（如 GitHub Release 自动生成的 repo-ref/）。
    """
    if len(zip_bytes) > _MAX_ZIP_BYTES:
        raise ValueError(f"zip 文件过大（{len(zip_bytes)} > {_MAX_ZIP_BYTES} bytes）")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip 文件: {e}") from e

    # 收集非目录条目
    members = [m for m in zf.infolist() if not m.is_dir()]
    if len(members) > _MAX_ZIP_FILES:
        raise ValueError(f"zip 文件数过多（{len(members)} > {_MAX_ZIP_FILES}）")

    # 检测公共顶层目录（如 anthropics-skills-abcdef/skills/pdf/SKILL.md → 公共前缀 anthropics-skills-abcdef/）
    paths = [m.filename for m in members]
    common_prefix = ""
    if paths:
        first_parts = paths[0].split("/", 1)
        if len(first_parts) > 1:
            candidate = first_parts[0] + "/"
            if all(p.startswith(candidate) for p in paths):
                common_prefix = candidate

    total_bytes = 0
    files: dict[str, bytes] = {}
    for m in members:
        rel = m.filename
        if common_prefix:
            rel = rel[len(common_prefix):]
        # 跳过空相对路径（公共前缀本身就是文件名时不应该,但保险起见）
        if not rel:
            continue
        rel = _safe_relative_path(rel)
        try:
            content = zf.read(m)
        except zipfile.BadZipFile as e:
            raise ValueError(f"zip 文件损坏: {m.filename}: {e}") from e
        total_bytes += len(content)
        if total_bytes > _MAX_ZIP_BYTES:
            raise ValueError(f"zip 解压总大小超过 {_MAX_ZIP_BYTES} bytes（防 zip bomb）")
        files[rel] = content

    _validate_skill_files(files)
    logger.info("[skills/install/source] zip 解压完成: %d 文件 %d B", len(files), total_bytes)
    return files
