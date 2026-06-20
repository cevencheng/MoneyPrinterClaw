"""
语音处理:tts(edge-tts)+ 音频时长(moviepy)+ 字幕生成(edge-tts 词边界 → SRT)。

替代 MPT app.services.voice 的 tts / get_audio_duration / create_subtitle,
精简为只走 edge-tts 路径(MPT 1400 行含 5 种 TTS 后端,我们只用 edge-tts)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re

import edge_tts

logger = logging.getLogger(__name__)


def _convert_rate(voice_rate: float) -> str:
    """float 语速 → edge-tts 百分比格式(1.0→'+0%', 1.2→'+20%', 0.8→'-20%')。"""
    pct = round((voice_rate - 1.0) * 100)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def tts(text: str, voice_name: str, voice_rate: float, voice_file: str):
    """edge-tts 合成配音。写 audio 到 voice_file,返回 SubMaker(含词边界时间轴)或 None。

    在 to_thread 中调用(同步上下文),内部用临时 event loop 跑 edge-tts 的 async stream。
    """
    text = text.strip()
    rate_str = _convert_rate(voice_rate)
    os.makedirs(os.path.dirname(voice_file), exist_ok=True)

    async def _do_tts() -> object:
        communicate = edge_tts.Communicate(text, voice_name, rate=rate_str)
        sub_maker = edge_tts.SubMaker()
        with open(voice_file, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                    sub_maker.feed(chunk)
        return sub_maker

    for attempt in range(3):
        try:
            loop = asyncio.new_event_loop()
            try:
                sub_maker = loop.run_until_complete(_do_tts())
            finally:
                loop.close()
            # 校验 SubMaker 有内容
            if hasattr(sub_maker, "cues") and sub_maker.cues:
                logger.info("[engine/voice] tts done -> %s (cues=%d)", voice_file, len(sub_maker.cues))
                return sub_maker
            logger.warning("[engine/voice] tts attempt %d: SubMaker 无 cues,重试", attempt + 1)
        except Exception as e:
            logger.warning("[engine/voice] tts attempt %d 失败: %s", attempt + 1, e)
    logger.error("[engine/voice] tts 3 次重试均失败")
    return None


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长(秒)。用 moviepy AudioFileClip。"""
    if not os.path.exists(audio_path):
        return 0.0
    try:
        from moviepy import AudioFileClip

        with AudioFileClip(audio_path) as audio:
            return float(audio.duration)
    except Exception as e:
        logger.error("[engine/voice] get_audio_duration 失败: %s", e)
        return 0.0


# ---- 字幕生成（句子级直接落盘，适配 edge-tts 7.x SentenceBoundary）----


def _mktimestamp(time_100ns: int) -> str:
    """100 纳秒单位 → HH:MM:SS.mmm（与 MPT mktimestamp 一致）。"""
    hour = math.floor(time_100ns / 10**7 / 3600)
    minute = math.floor((time_100ns / 10**7 / 60) % 60)
    seconds = (time_100ns / 10**7) % 60
    return f"{hour:02d}:{minute:02d}:{seconds:06.3f}"


# 句内细分用标点（停顿点：标点保留在前一段尾部）
_INNER_PUNCT_RE = re.compile(r"——|[，,、；;：:。．？?！!…]")
_INNER_TOKENIZE_RE = re.compile(r"(——|[，,、；;：:。．？?！!…])")


def _split_cue_into_lines(text: str, min_chars: int = 6) -> list[str]:
    """把句子级 cue 文本按内部停顿点切成 ≥6 字的小段(标点附在段尾)。

    edge-tts 7.x 中文 cue 是整句,直接当字幕会出现 3-4 行折行。按 `，、；：。——…`
    等停顿点切,只在累计 ≥ `min_chars` 时 flush,避免出现 "嗯，" 这种 1-2 字片段。
    """
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for tok in _INNER_TOKENIZE_RE.split(text):
        if not tok:
            continue
        cur += tok
        if _INNER_PUNCT_RE.fullmatch(tok) and len(cur) >= min_chars:
            chunks.append(cur)
            cur = ""
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def create_subtitle(sub_maker, script: str, srt_path: str) -> None:
    """⚠️ DEPRECATED：旧路径基于 edge-tts SubMaker 生成 SRT。Phase 2 删除。

    新管线由 engine/subtitle_asr.py 替代：faster-whisper 词级时间戳 → ASS 字幕。
    保留此函数仅为向后兼容（升级期老 task 目录回滚 / 字幕引擎切回 edge 调试）。
    bridge.py 已不再调用此函数；resource_prep_node 走 bridge.asr_subtitle 路径。

    实现：从 edge-tts SubMaker cues 生成 SRT 字幕（句子级 → 内部停顿点二次细分）。
    edge-tts 7.x 对中文返回 SentenceBoundary（每个 cue 即一整句含标点），
    一句直接当字幕会出现 3-4 行折行。这里再按 `，、；：。——` 等停顿点
    把 cue 切成更短的小段，按字符数比例分配 cue 时间，更适合短视频阅读。
    `script` 参数保留仅为向后兼容（句子级落盘无需脚本对齐）。
    """
    from html import unescape

    _ = script
    if not sub_maker or not hasattr(sub_maker, "cues") or not sub_maker.cues:
        logger.warning("[engine/voice] create_subtitle: 无 cues，跳过")
        return

    sub_items: list[str] = []
    idx = 1
    for cue in sub_maker.cues:
        text = unescape(cue.content or "").strip()
        if not text:
            continue
        lines = _split_cue_into_lines(text)
        if not lines:
            continue

        cue_start = int(cue.start.total_seconds() * 10**7)
        cue_end = int(cue.end.total_seconds() * 10**7)
        cue_dur = max(1, cue_end - cue_start)
        total_chars = sum(len(ln) for ln in lines)

        # 按字符比例分配 cue 时间，最后一段对齐到 cue_end（避免累积误差）
        running = cue_start
        for i, ln in enumerate(lines):
            if i == len(lines) - 1:
                seg_end = cue_end
            else:
                seg_end = running + int(cue_dur * len(ln) / total_chars)
            start_t = _mktimestamp(running).replace(".", ",")
            end_t = _mktimestamp(seg_end).replace(".", ",")
            sub_items.append(f"{idx}\n{start_t} --> {end_t}\n{ln}\n")
            idx += 1
            running = seg_end

    if not sub_items:
        logger.warning("[engine/voice] create_subtitle: cues 内容全空，跳过")
        return

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sub_items))
    logger.info("[engine/voice] subtitle 写入 %s (%d 条)", srt_path, len(sub_items))
