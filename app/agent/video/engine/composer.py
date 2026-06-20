"""
视频合成:combine_videos(纯 ffmpeg 拼接)+ generate_video(纯 ffmpeg 烧字幕/混音)。

替代 MPT app.services.video。
combine 用一条 ffmpeg + filtergraph 完成「切片 + scale-letterbox-pad + fps 归一 + concat」;
generate 用一条 ffmpeg + filtergraph 完成「subtitles 烧字幕(libass) + voice/BGM 混音(amix)」,
告别 moviepy TextClip 逐帧像素叠加（33s 视频从 3 分 45 秒 → ~5-10 秒）。
"""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

from agent.video.engine._utils import font_dir, song_dir, get_ffmpeg_binary
from agent.video.engine.schema import VideoAspect, VideoConcatMode, VideoParams

logger = logging.getLogger(__name__)

_VIDEO_CODEC = "libx264"
_AUDIO_CODEC = "aac"
_AUDIO_BITRATE = "192k"
_FPS = 30
_SEGMENT_CAP = 50  # 安全上限,防音频时长异常导致死循环


# ---- combine_videos ----


def _probe_duration(path: str) -> float:
    """获取媒体时长(秒)。优先 ffprobe;回退解析 ffmpeg stderr。"""
    if not path or not os.path.exists(path):
        return 0.0
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except (ValueError, subprocess.TimeoutExpired):
            pass
    # 回退:ffmpeg -i 解析 stderr 里的 Duration
    try:
        r = subprocess.run(
            [get_ffmpeg_binary(), "-i", path],
            capture_output=True, text=True, timeout=15, check=False,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
        if m:
            h, mm, s = m.groups()
            return int(h) * 3600 + int(mm) * 60 + float(s)
    except subprocess.TimeoutExpired:
        pass
    return 0.0


def combine_videos(
    output_path: str,
    video_paths: list[str],
    audio_path: str,
    aspect: VideoAspect,
    concat_mode: VideoConcatMode,
    max_clip_duration: int,
) -> str:
    """一条 ffmpeg 命令完成「切片 + 缩放 + 补黑边 + 拼接」(无 moviepy)。

    每段输入用 input-seek (`-t` 在 `-i` 前) 高速取头部 N 秒;
    filter_complex 对每路做 `scale=decrease + pad black + fps + format + setsar=1`,
    最后 `concat` 合流。Pexels 各种分辨率/帧率/SAR 都能在一次编码里统一。
    """
    if not video_paths:
        raise ValueError("combine_videos: video_paths 为空")

    target_w, target_h = aspect.to_resolution()
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    # 目标时长 = audio duration(无 audio 时跑一遍所有源)
    target_duration = _probe_duration(audio_path)

    # 探测每个源时长,过滤损坏文件
    sources = list(video_paths)
    if concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(sources)
    sources_with_dur: list[tuple[str, float]] = []
    for p in sources:
        d = _probe_duration(p)
        if d > 0:
            sources_with_dur.append((p, d))
        else:
            logger.warning("[engine/composer] 探测失败,跳过素材: %s", os.path.basename(p))
    if not sources_with_dur:
        raise RuntimeError("combine_videos: 无有效素材(全部探测失败)")

    # 构建 segments:循环填充直到覆盖 target_duration
    segments: list[tuple[str, float]] = []
    total = 0.0
    i = 0
    while len(segments) < _SEGMENT_CAP:
        path, dur = sources_with_dur[i % len(sources_with_dur)]
        use = min(dur, float(max_clip_duration))
        segments.append((path, use))
        total += use
        i += 1
        if target_duration > 0:
            if total >= target_duration:
                break
        else:
            # 没 audio target,跑一遍所有源就停
            if i >= len(sources_with_dur):
                break

    # 构建 ffmpeg 命令(input-seek + filter_complex + concat)
    cmd: list[str] = [
        get_ffmpeg_binary(), "-y",
        "-hide_banner", "-loglevel", "error",
    ]
    for path, dur in segments:
        cmd.extend(["-t", f"{dur:.3f}", "-i", path])

    filter_parts: list[str] = []
    for idx in range(len(segments)):
        # scale-decrease(等比缩放至刚好放进容器)+ pad 黑边居中 + fps 归一 + 像素格式 + 方形像素
        filter_parts.append(
            f"[{idx}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={_FPS},format=yuv420p,setsar=1[v{idx}]"
        )
    concat_inputs = "".join(f"[v{idx}]" for idx in range(len(segments)))
    filter_parts.append(
        f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[outv]"
    )
    filtergraph = ";".join(filter_parts)

    cmd.extend([
        "-filter_complex", filtergraph,
        "-map", "[outv]",
        "-c:v", _VIDEO_CODEC,
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        output_path,
    ])

    logger.info(
        "[engine/composer] ffmpeg combine: %d segments, %.1fs (target %.1fs)",
        len(segments), total, target_duration,
    )
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg combine 失败: {result.stderr[-1000:]}")

    logger.info("[engine/composer] combine 完成 -> %s (%.1fs)", output_path, total)
    return output_path


# ---- generate_video ----


def _get_bgm_file(bgm_type: str = "random") -> str:
    """获取 BGM 文件路径。random=随机;""=无。"""
    if not bgm_type:
        return ""
    sdir = song_dir()
    songs = [f for f in os.listdir(sdir) if f.endswith(".mp3")]
    if not songs:
        return ""
    return os.path.join(sdir, random.choice(songs))


# subtitle_position → (libass Alignment, MarginV)。libass numpad: 2=中下 5=正中 8=中上
_POSITION_MAP = {"bottom": (2, 60), "top": (8, 60), "center": (5, 0)}

# font 文件名 → DirectWrite family name（libass FontName 必须用 family name，非文件名）。
# 不在此表的字体回退到「去扩展名的文件名」，可能 fallback 到系统字体但不致命。
_FONT_FAMILY_MAP = {
    "STHeitiMedium.ttc": "Heiti SC",
    "NotoSansCJKsc-Regular.otf": "Noto Sans CJK SC",
}


def _escape_filter_path(p: str) -> str:
    """Windows 盘符冒号转义 + 反斜杠转正斜杠（subtitles 滤镜路径解析要求）。

    E:\\a\\b.srt → E\\:/a/b.srt（冒号在滤镜参数里是分隔符，必须转义）。
    """
    return Path(p).as_posix().replace(":", "\\:")


def _hex_to_libass(hex_color: str) -> str:
    """#RRGGBB → &H00BBGGRR（libass BGR 倒序，8 位，alpha=00）。"""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}"


def _resolve_font_family(font_name: str) -> str:
    """font 文件名 → DirectWrite family name（libass FontName 用）。"""
    return _FONT_FAMILY_MAP.get(font_name, os.path.splitext(font_name)[0])


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_path: str,
    params: VideoParams,
) -> None:
    """叠音轨 + 字幕 + BGM → 最终成片（纯 ffmpeg，无 moviepy）。

    输入映射：[0]=combined video（无声），[1]=voice，[2]=bgm（-stream_loop -1 无限循环，可选）。
    视频烧字幕用 subtitles 滤镜（libass）；音频用 volume + amix；输出 -shortest 以 voice 时长对齐。
    """
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    # ---- 构建输入 ----
    cmd: list[str] = [
        get_ffmpeg_binary(), "-y",
        "-hide_banner", "-loglevel", "error",
        "-i", video_path,            # [0:v]（无声）
        "-i", audio_path,            # [1:a]
    ]
    bgm_path = _get_bgm_file(params.bgm_type)
    has_bgm = bool(bgm_path) and os.path.exists(bgm_path)
    if has_bgm:
        cmd.extend(["-stream_loop", "-1", "-i", bgm_path])  # [2:a] 无限循环

    filter_parts: list[str] = []

    # ---- 视频烧字幕（libass）----
    has_subtitle = bool(subtitle_path) and os.path.exists(subtitle_path)
    if has_subtitle:
        safe_sub = _escape_filter_path(subtitle_path)
        safe_fontdir = _escape_filter_path(font_dir())
        ext = os.path.splitext(subtitle_path)[1].lower()
        if ext == ".ass":
            # ASS 自带完整样式（lines_to_ass 烧进 header）。再叠 force_style 会覆盖 ASS 内置
            # 字号/颜色/边距,反而损坏。仅传 fontsdir 让 libass 找字体文件即可。
            filter_parts.append(
                f"[0:v]subtitles='{safe_sub}':fontsdir='{safe_fontdir}'[outv]"
            )
        else:
            # .srt 兼容老路径（升级前残留任务 / 字幕引擎切回 edge 时）：拼 force_style
            font_family = _resolve_font_family(params.font_name)
            align, marginv = _POSITION_MAP.get(params.subtitle_position, (2, 60))
            outline = max(1, round(params.stroke_width))
            force_style = (
                f"FontName={font_family},"
                f"FontSize={params.font_size},"
                f"PrimaryColour={_hex_to_libass(params.text_color)},"
                f"OutlineColour={_hex_to_libass(params.stroke_color)},"
                f"Outline={outline},"
                f"Alignment={align},"
                f"MarginV={marginv}"
            )
            filter_parts.append(
                f"[0:v]subtitles='{safe_sub}':fontsdir='{safe_fontdir}'"
                f":force_style='{force_style}'[outv]"
            )
    else:
        # 无字幕：直通（仍走 filter_complex，因 -shortest 要重封装）
        filter_parts.append("[0:v]copy[outv]")

    # ---- 音频混音 ----
    if has_bgm:
        voice_dur = _probe_duration(audio_path)
        fade = f",afade=t=out:st={voice_dur - 3:.3f}:d=3" if voice_dur > 3 else ""
        # ⚠️ amix 默认 normalize=1 会把 2 路各乘 0.5 → 人声变小；末尾 volume=2.0 恢复原始音量
        filter_parts.append(
            f"[1:a]volume={params.voice_volume}[tts];"
            f"[2:a]volume={params.bgm_volume}{fade}[bgm];"
            f"[tts][bgm]amix=inputs=2:duration=first:dropout_transition=2,volume=2.0[outa]"
        )
    else:
        filter_parts.append(f"[1:a]volume={params.voice_volume}[outa]")

    filtergraph = ";".join(filter_parts)
    cmd.extend([
        "-filter_complex", filtergraph,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", _VIDEO_CODEC,
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", _AUDIO_CODEC,
        "-b:a", _AUDIO_BITRATE,
        "-shortest",  # 以 amix 输出（=voice 时长）为准，切掉 combined 尾部静帧
        output_path,
    ])

    logger.info(
        "[engine/composer] ffmpeg generate: subtitle=%s bgm=%s voice_vol=%.2f bgm_vol=%.2f",
        has_subtitle, has_bgm, params.voice_volume, params.bgm_volume,
    )
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("[engine/composer] ffmpeg generate 失败 stderr:\n%s", result.stderr[-1500:])
        raise RuntimeError(f"ffmpeg generate 失败: {result.stderr[-1500:]}")
    logger.info("[engine/composer] generate 完成 -> %s", output_path)
