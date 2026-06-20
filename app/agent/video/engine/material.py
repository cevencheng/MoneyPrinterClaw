"""
素材下载:Pexels / Pixabay 视频搜索 + 下载 + 校验。

工业级实现：
- httpx 异步并发下载（Semaphore 限流防 CDN 封禁）
- 指数退避重试（max_retries=3）
- MP4 幻数校验（防 HTML 错误页混入）
- 流式写入（防内存爆）
- 异常文件原子清理（不留残渣）
- 搜索重试 + key 轮转
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from typing import Any

import httpx

from agent.video.engine.schema import VideoAspect, VideoConcatMode

logger = logging.getLogger(__name__)

_PEXELS_ENDPOINT = "https://api.pexels.com/videos/search"
_PIXABAY_ENDPOINT = "https://pixabay.com/api/videos/"

# 并发控制：同时下载不超过 4 个，防 CDN 封 IP
_MAX_CONCURRENT = 4
# HTTP 连接池：复用 TCP，减少 SSL 握手
_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10)

# API key 轮转计数器
_key_counter = {"pexels": 0, "pixabay": 0}


def _next_key(keys: list[str], provider: str) -> str:
    if not keys:
        return ""
    idx = _key_counter[provider] % len(keys)
    _key_counter[provider] += 1
    return keys[idx]


# ---- 搜索（同步，由 bridge 的 to_thread 调用）----


def _search_pexels(client: httpx.Client, query: str, api_key: str, min_duration: int, aspect: VideoAspect) -> list[dict]:
    orientation = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}.get(aspect.value, "portrait")
    resp = client.get(
        _PEXELS_ENDPOINT,
        params={"query": query, "per_page": 20, "orientation": orientation},
        headers={"Authorization": api_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    target_w, target_h = aspect.to_resolution()
    results: list[dict] = []
    for video in resp.json().get("videos", []):
        if video.get("duration", 0) < min_duration:
            continue
        for vf in video.get("video_files", []):
            if vf.get("width") == target_w and vf.get("height") == target_h:
                results.append({"url": vf["link"], "duration": video["duration"]})
                break
    return results


def _search_pixabay(client: httpx.Client, query: str, api_key: str, min_duration: int, aspect: VideoAspect) -> list[dict]:
    resp = client.get(
        _PIXABAY_ENDPOINT,
        params={"q": query, "video_type": "all", "per_page": 50, "key": api_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    target_w, _ = aspect.to_resolution()
    results: list[dict] = []
    for hit in resp.json().get("hits", []):
        if hit.get("duration", 0) < min_duration:
            continue
        videos = hit.get("videos", {})
        for quality in ("large", "medium", "small", "tiny"):
            v = videos.get(quality)
            if v and v.get("width", 0) >= target_w:
                results.append({"url": v["url"], "duration": hit["duration"]})
                break
    return results


def _search_with_retry(
    client: httpx.Client, search_fn, term: str, keys: list[str], provider: str,
    min_duration: int, aspect: VideoAspect, max_retries: int = 3,
) -> list[dict]:
    """搜索 + 重试 + key 轮转。"""
    for attempt in range(max_retries):
        key = _next_key(keys, provider)
        if not key:
            logger.warning("[engine/material] 无 %s API key", provider)
            return []
        try:
            return search_fn(client, term, key, min_duration, aspect)
        except Exception as e:
            logger.warning("[engine/material] 搜索 '%s' attempt %d/%d 失败: %s", term[:30], attempt + 1, max_retries, e)
    return []


# ---- 下载（异步并发 + 重试 + 幻数校验 + 原子清理）----


def _verify_mp4(file_path: str) -> bool:
    """轻量秒级校验：文件大小 > 50KB + MP4 幻数（ftyp）。

    比 VideoFileClip 快 100 倍（不开 ffmpeg），且能拦截 HTML 错误页。
    """
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 50 * 1024:
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(32)
            return b"ftyp" in header
    except Exception:
        return False


async def _download_one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, save_dir: str, max_retries: int = 3,
) -> str:
    """下载单个素材：流式写入 + 重试 + 幻数校验 + 异常清理。

    sem 由调用方传入(每次 _download_all 现场创建),避免跨 event loop 复用。
    """
    url_hash = hashlib.md5(url.split("?")[0].encode()).hexdigest()
    file_path = os.path.join(save_dir, f"vid-{url_hash}.mp4")

    # 缓存命中：已存在且通过幻数校验 → 跳过
    if os.path.exists(file_path) and _verify_mp4(file_path):
        return file_path

    async with sem:
        for attempt in range(max_retries):
            try:
                # 流式下载（不全量加载内存）
                async with client.stream("GET", url, timeout=httpx.Timeout(15.0, read=120.0), follow_redirects=True) as resp:
                    if resp.status_code != 200:
                        raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    with open(file_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=8192):
                            f.write(chunk)

                # 幻数校验（拦截 HTML 错误页 / 残缺文件）
                if _verify_mp4(file_path):
                    logger.info("[engine/material] 下载成功 %s", os.path.basename(file_path))
                    return file_path
                else:
                    raise ValueError("MP4 幻数校验失败（可能 HTML 错误页或残缺文件）")

            except Exception as e:
                logger.warning("[engine/material] 下载 attempt %d/%d 失败: %s", attempt + 1, max_retries, str(e)[:100])
                # 原子清理：绝不留半吊子文件
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))  # 指数退避

    return ""


# ---- 主入口（同步签名，由 bridge 的 to_thread 调用；内部跑 async 并发）----


def download_videos(
    search_terms: list[str],
    source: str,
    video_aspect: VideoAspect,
    concat_mode: VideoConcatMode,
    audio_duration: float,
    max_clip_duration: int,
    save_dir: str,
    api_keys: list[str],
) -> list[str]:
    """搜索 + 并发下载素材。返回本地文件路径列表。

    流程：同步搜索（httpx.Client + 重试 + key 轮转）→ 异步并发下载（Semaphore 限流 + 重试 + 幻数校验）。
    """
    os.makedirs(save_dir, exist_ok=True)
    search_fn = _search_pexels if source == "pexels" else _search_pixabay
    provider = source

    # ---- Phase 1：搜索（同步，带重试 + key 轮转）----
    found_items: list[dict] = []
    seen_urls: set[str] = set()
    with httpx.Client(limits=_LIMITS, verify=True) as sync_client:
        for term in search_terms:
            items = _search_with_retry(sync_client, search_fn, term, api_keys, provider, max_clip_duration, video_aspect)
            for item in items:
                if item["url"] not in seen_urls:
                    found_items.append(item)
                    seen_urls.add(item["url"])

    if not found_items:
        logger.warning("[engine/material] 未搜到素材")
        return []

    # random → 打乱
    if concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(found_items)

    # 按需取足够覆盖 audio_duration 的素材数（多取 2 个容错）
    needed = int(audio_duration / max_clip_duration) + 2 if audio_duration > 0 else len(found_items)
    to_download = found_items[:max(needed, 3)]

    logger.info("[engine/material] 搜索到 %d 素材，并发下载前 %d 个", len(found_items), len(to_download))

    # ---- Phase 2：并发下载（异步，Semaphore 限流）----
    result_paths = _run_async_download(to_download, save_dir)

    # 按时长累积截取
    paths: list[str] = []
    total_duration = 0.0
    for i, path in enumerate(result_paths):
        if path and total_duration < audio_duration:
            paths.append(path)
            total_duration += min(max_clip_duration, to_download[i].get("duration", max_clip_duration))

    logger.info("[engine/material] 下载完成 %d 个（总时长 %.1fs / 目标 %.1fs）", len(paths), total_duration, audio_duration)
    return paths


def _run_async_download(items: list[dict], save_dir: str) -> list[str]:
    """跑异步并发下载(在 to_thread 的 worker 线程里调用 → 该线程无 loop,直接 asyncio.run)。

    Semaphore 在 _download_all 内现场创建,避免跨 event loop 复用导致
    'bound to a different event loop' 错误。
    """
    urls = [item["url"] for item in items]

    async def _download_all():
        sem = asyncio.Semaphore(_MAX_CONCURRENT)
        async with httpx.AsyncClient(limits=_LIMITS, verify=True, timeout=30.0) as client:
            tasks = [_download_one(client, sem, url, save_dir) for url in urls]
            return await asyncio.gather(*tasks)

    return asyncio.run(_download_all())
