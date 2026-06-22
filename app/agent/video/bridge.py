"""
视频桥接层

封装自研 video engine（edge-tts / moviepy / ffmpeg / Pexels）的异步调用 +
SSE 进度推送 + progress.json 子步清单 + 任务目录隔离。
所有产物写到隔离的 storage/video_tasks/<session_id>/<task_id>/（按会话分组）；
session_id 缺省时退回扁平 <task_id>/（老 checkpoint 续跑降级）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

from agent.config import settings
from agent.video.engine import composer as engine_composer
from agent.video.engine import material as engine_material
from agent.video.engine import schema as engine_schema
from agent.video.engine import subtitle_asr as engine_subtitle_asr
from agent.video.engine import voice as engine_voice
from langchain_core.callbacks import adispatch_custom_event

logger = logging.getLogger(__name__)

# ---- ASR 全局互斥锁（硬化点 #1）----
# 批量场景下用户可能 5-10 路并发，5 个 task 同时 to_thread WhisperModel.transcribe：
#   CPU 模式：CTranslate2 默认抢所有核心 → 上下文切换爆 100% → FastAPI heartbeat 丢失主进程假死
#   GPU 模式：5 份模型权重并行分配显存 → CUDA OOM 整个 Python 进程蒸发
# small 模型 30s 音频只需 ~1s,串行排队是性价比最高的钢铁防线（vs 并发 OOM 后全员重跑）。
_asr_global_lock = asyncio.Lock()

# ---- progress.json 子步进度清单（断点续传的子步级补充） ----
# 每个任务目录下一个 progress.json，记录各子步 done 状态 + 产物路径。
# 续跑时 step_is_done 双保险判断（manifest done + 产物文件校验），只重做未完成的。
StepName = Literal["audio", "subtitle", "materials", "combine", "generate"]

# 产物文件最小字节数阈值（防残缺小文件被误判完整）
_MIN_BYTES = {
    "audio": 1024,          # audio.mp3 下限
    "subtitle": 0,          # ass 文件无下限（空 lines 不写文件,有写就有效）
    "combine": 1_000_000,   # 与既有 >1MB 一致
    "generate": 1_000_000,
    "materials": 0,         # materials 逐 clip 校验，整体无阈值
}

_PROGRESS_VERSION = 1


def progress_path(task_id: str, *, session_id: str = "") -> str:
    """progress.json 路径（在任务目录内）。"""
    return os.path.join(task_dir(task_id, session_id=session_id), "progress.json")


def load_progress(task_id: str, *, session_id: str = "") -> dict[str, Any]:
    """读 manifest。缺失 / JSON 损坏 → {}（= 全 pending = 当前行为，向后兼容、自愈）。"""
    try:
        with open(progress_path(task_id, session_id=session_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_progress(task_id: str, manifest: dict[str, Any], *, session_id: str = "") -> None:
    """原子写 manifest：progress.json.tmp + os.replace（同 combine 的 .tmp 模式，防 manifest 自身残缺）。"""
    manifest.setdefault("schema_version", _PROGRESS_VERSION)
    manifest["task_id"] = task_id
    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p = progress_path(task_id, session_id=session_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def mark_step(task_id: str, step: StepName, status: Literal["done", "pending"] = "done", *, session_id: str = "", **artifact_fields: Any) -> dict[str, Any]:
    """load → set steps[step] = {status, **fields} → 原子 save → 返回完整 manifest。"""
    m = load_progress(task_id, session_id=session_id)
    m.setdefault("steps", {})[step] = {"status": status, **artifact_fields}
    save_progress(task_id, m, session_id=session_id)
    return m


def mark_meta(task_id: str, *, session_id: str = "", **fields: Any) -> dict[str, Any]:
    """把任务级 meta 字段写到 progress.json 顶层（与 steps 同级）。

    用途：保存 topic / script_text 等"输入级"信息,方便事后对比 ASR 字幕（subtitle.ass）
    与原始 TTS 文案。这些字段不是步骤产物,故不放 steps 下;调用幂等,重复写覆盖。
    保留字（不允许覆盖）：steps / schema_version / task_id / updated_at（save_progress 自管）。
    """
    reserved = {"steps", "schema_version", "task_id", "updated_at"}
    safe = {k: v for k, v in fields.items() if k not in reserved}
    m = load_progress(task_id, session_id=session_id)
    m.update(safe)
    save_progress(task_id, m, session_id=session_id)
    return m


def _artifact_valid(path: str, min_bytes: int) -> bool:
    """产物文件存在 + size ≥ 阈值。"""
    return bool(path) and os.path.exists(path) and os.path.getsize(path) >= min_bytes


def step_is_done(task_id: str, step: StepName, *, session_id: str = "") -> bool:
    """双保险：manifest 标 done **且** 产物文件 exists+valid → True；否则 False（重做）。

    - audio：audio.mp3 有效（字幕已拆到独立 subtitle 步,本步只看音频）。
    - subtitle：subtitle 文件存在；未启用字幕时按 done 处理（整步跳过）。
    - materials：所有 clip 文件存在。
    - combine/generate：产物文件 size ≥ 阈值。
    - manifest 缺失/损坏 → False（安全 = 全重跑）。
    """
    s = load_progress(task_id, session_id=session_id).get("steps", {}).get(step)
    if step == "subtitle":
        # 字幕未启用：直接视为 done,resource_prep 整步跳过 ASR
        if not settings.video_subtitle_enabled:
            return True
        # 启用：必须 manifest done + 文件有效
        if not s or s.get("status") != "done":
            return False
        return _artifact_valid(s.get("subtitle_path", ""), _MIN_BYTES["subtitle"])
    if not s or s.get("status") != "done":
        return False
    if step == "audio":
        return _artifact_valid(s.get("audio_path", ""), _MIN_BYTES["audio"])
    if step == "materials":
        clips = s.get("clips", []) or []
        return bool(clips) and all(_artifact_valid(c, 0) for c in clips)
    if step == "combine":
        return _artifact_valid(s.get("combined_path", ""), _MIN_BYTES["combine"])
    if step == "generate":
        return _artifact_valid(s.get("final_path", ""), _MIN_BYTES["generate"])
    return False


async def _emit_progress(
    stage: str,
    label: str,
    *,
    task_id: str = "",
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> None:
    """向 SSE 流推送制作进度（adispatch_custom_event → chat.py 的 on_custom_event）。

    携带 task_id（chat.py 据此生成独立 render 卡片 id）+ batch_index/total/topic
    （前端卡片标题显示 `1/2 · 咖啡`；单视频时 batch_* 为 None）。
    必须在 bridge 的 async 封装层调用（to_thread 之外）以保留 langchain contextvars。
    非流式上下文（隔离子图测试）下 adispatch_custom_event 抛 RuntimeError，兜底忽略。"""
    logger.info("[video/progress] ▶ %s (%s) task=%s batch=%s/%s", label, stage, task_id, batch_index, batch_total)
    try:
        await adispatch_custom_event(
            "progress",
            {
                "stage": stage,
                "label": label,
                "task_id": task_id,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "topic": topic,
            },
        )
    except RuntimeError:
        logger.debug("[bridge] progress skipped (no stream context): %s", stage)


async def _emit_render_done(
    task_id: str,
    final_path: str,
    *,
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> None:
    """渲染完成：发 render_done 事件 → chat.py 合成该轮 render_video 的 tool_result（翻播放器）。
    每轮 render 完成立刻发，替代流末统一发；task_id 区分各轮卡片。"""
    logger.info("[video/render_done] task=%s final=%s batch=%s/%s", task_id, final_path, batch_index, batch_total)
    try:
        await adispatch_custom_event(
            "render_done",
            {
                "task_id": task_id,
                "final_path": final_path,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "topic": topic,
            },
        )
    except RuntimeError:
        logger.debug("[bridge] render_done skipped (no stream context): %s", task_id)

async def _emit_render_failed(
    task_id: str,
    *,
    retry_count: int,
    max_retry: int,
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> None:
    """渲染失败：发 render_failed 事件 → chat.py 给该轮 render_video 卡发 stage=failed（翻红）。

    retry_count < max_retry → 图路由会回 resource_prep 复用同 task_id 重做（progress.json 跳过
    已完成子步、只重试 TTS/失败的），同张卡原地从红翻回 running（"一任务一卡"）。
    retry_count >= max_retry → 彻底失败,卡停在红色,render_node 回战败 ToolMessage 唤醒 agent 报错。
    """
    logger.info(
        "[video/render_failed] task=%s retry=%d/%d batch=%s/%s",
        task_id, retry_count, max_retry, batch_index, batch_total,
    )
    try:
        await adispatch_custom_event(
            "render_failed",
            {
                "task_id": task_id,
                "retry_count": retry_count,
                "max_retry": max_retry,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "topic": topic,
            },
        )
    except RuntimeError:
        logger.debug("[bridge] render_failed skipped (no stream context): %s", task_id)


async def _emit_creative_failed(
    task_id: str,
    *,
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
    reason: str = "",
) -> None:
    """创意期熔断：分镜不合格且超限 → 发 creative_failed 事件 → chat.py 给该轮 render_video 卡
    发 stage=failed（翻红）。区别于 render_failed（渲染车间失败），这是创意期就熔断、未进车间。

    复用前端 stage=failed 渲染（失败卡 + 重试按钮），无需前端新增态。reason 透传失败原因
    （分镜为空 / search_prompt 缺失）便于排查。
    """
    logger.info(
        "[video/creative_failed] task=%s batch=%s/%s reason=%s",
        task_id, batch_index, batch_total, reason or "(未提供)",
    )
    try:
        await adispatch_custom_event(
            "creative_failed",
            {
                "task_id": task_id,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "topic": topic,
                "reason": reason,
            },
        )
    except RuntimeError:
        logger.debug("[bridge] creative_failed skipped (no stream context): %s", task_id)


async def _emit_self_heal(
    task_id: str,
    retry_count: int,
    *,
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> None:
    """局部重试回滚瞬间的"无损自愈弹幕"：发一条 stage 仍是 tts 的 progress 事件,
    但 label 显式标注自愈,把"红→绿"闪烁变成"系统正在自愈"的产品秀。

    复用 progress 通道(前端按 task_id 原地更新同一张卡);stage 用 tts 是因为重试入口
    永远从 resource_prep 的 TTS 重新跑起(progress.json 会跳过已完成的 materials/combine)。
    """
    label = f"⚠️ 监测到渲染网络抖动,正在启动第 {retry_count + 1} 次无损自愈重试..."
    logger.info("[video/self_heal] task=%s retry=%d", task_id, retry_count)
    try:
        await adispatch_custom_event(
            "progress",
            {
                "stage": "tts",
                "label": label,
                "task_id": task_id,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "topic": topic,
            },
        )
    except RuntimeError:
        logger.debug("[bridge] self_heal skipped (no stream context): %s", task_id)


_SRC_DIR = Path(__file__).resolve().parents[2]  # app/
_TASKS_ROOT = (_SRC_DIR / settings.video_tasks_dir).resolve()

# 路径段安全字符集（session_id 来自客户端可控的 thread_id，防路径遍历 ../../etc）。
# task_id 是内部 uuid4 生成本就安全，但统一校验做防御纵深。
_SAFE_SEG = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_segment(name: str, what: str) -> str:
    """校验路径段：仅允许 [A-Za-z0-9_-]，拒 / \\ .. 空等。非法直接 raise（绝不静默拼路径）。"""
    if not name or not _SAFE_SEG.match(name):
        raise ValueError(f"非法 {what} 路径段: {name!r}")
    return name


def task_dir(task_id: str, *, session_id: str = "") -> str:
    """隔离的视频任务目录（绝对路径，自动创建）。

    session_id 非空 → ``video_tasks/<session_id>/<task_id>/``（按会话分组，用户易定位）；
    session_id 空 → 退回扁平 ``video_tasks/<task_id>/``（老 checkpoint 续跑 / 单测降级）。
    """
    _safe_segment(task_id, "task_id")
    base = _TASKS_ROOT / _safe_segment(session_id, "session_id") / task_id if session_id else _TASKS_ROOT / task_id
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def tasks_root() -> str:
    """视频任务根目录（绝对路径，确保存在）。供 StaticFiles 挂载，与 task_dir 写入路径一致。"""
    _TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    return str(_TASKS_ROOT)


async def tts(
    task_id: str,
    script: str,
    *,
    session_id: str = "",
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> str:
    """edge-tts 合成配音。返回 audio_path（失败返回 ""）。

    退化为纯 text→audio：sub_maker（edge-tts 词边界）不再透传到上层。字幕由独立的
    asr_subtitle 步从音频反向识别（faster-whisper 词级时间戳 → ASS）。
    engine_voice.tts 仍返回 sub_maker（保持引擎层向后兼容）,bridge 直接丢弃。
    """
    audio_path = os.path.join(task_dir(task_id, session_id=session_id), "audio.mp3")
    await _emit_progress(
        "tts", "🎙️ 配音合成",
        task_id=task_id, batch_index=batch_index, batch_total=batch_total, topic=topic,
    )
    sub_maker = await asyncio.to_thread(
        engine_voice.tts,
        text=script,
        voice_name=settings.video_voice_name,
        voice_rate=settings.video_voice_rate,
        voice_file=audio_path,
    )
    # ⚠️ 不能用 os.path.exists 判失败 —— engine_voice.tts 用 `with open(..., "wb")` 在 SSL/网络
    # 失败前就已创建 0-byte 空文件,残骸 always 存在。必须看大小:< _MIN_BYTES["audio"]
    # 视为失败,清残骸（不清的话下次续跑 step_is_done 误判 audio 已 done）。
    if not _artifact_valid(audio_path, _MIN_BYTES["audio"]):
        size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
        logger.error(
            "[bridge] tts 失败: %s 大小 %d B < 阈值 %d B（典型原因：edge-tts SSL 连不上 speech.platform.bing.com）",
            audio_path, size, _MIN_BYTES["audio"],
        )
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass
        return ""
    logger.info("[bridge] tts done -> %s (%d B)", audio_path, os.path.getsize(audio_path))
    return audio_path


async def get_audio_duration(audio_path: str) -> float:
    return await asyncio.to_thread(engine_voice.get_audio_duration, audio_path)


async def asr_subtitle(
    task_id: str,
    script: str,
    audio_path: str,
    *,
    session_id: str = "",
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> str:
    """faster-whisper ASR + 语义切片 → ASS 字幕。返回 ass 路径,禁用/失败返回 ""。

    全局串行（_asr_global_lock）：批量并发场景保住主进程不假死、显存不爆。
    progress emit 在锁外：让排队任务的卡片先显示"字幕识别中",而不是看起来卡死。

    失败容忍：任何异常 → 返回 ""，render 阶段 has_subtitle=False 视频无字幕但仍可产出
    （比"整轮失败"对用户友好）。
    """
    if not settings.video_subtitle_enabled:
        return ""
    if not audio_path or not os.path.exists(audio_path):
        logger.warning("[bridge] asr_subtitle 跳过：audio_path 不存在 %s", audio_path)
        return ""

    ass_path = os.path.join(task_dir(task_id, session_id=session_id), "subtitle.ass")
    await _emit_progress(
        "asr", "🔤 字幕识别",
        task_id=task_id, batch_index=batch_index, batch_total=batch_total, topic=topic,
    )
    try:
        async with _asr_global_lock:
            await asyncio.to_thread(
                engine_subtitle_asr.run,
                audio_path,
                script,
                ass_path,
                font_name=settings.video_font_name,
                font_size=settings.video_font_size,
                margin_v=settings.video_subtitle_margin_v,
                secondary_color=settings.video_subtitle_secondary_color,
            )
    except Exception as e:
        # 关键防御：即便 run() 抛异常,只要 ass 文件已经落盘(异常发生在
        # _segments_to_ass_karaoke 写文件之后的微操作,如 logger.info 编码异常),
        # 仍认为成功,避免把可用产物误判为失败。
        if os.path.exists(ass_path) and os.path.getsize(ass_path) > 0:
            logger.warning(
                "[bridge] asr_subtitle 抛异常但 ass 已落盘(%d B),接收为成功 task=%s err=%s",
                os.path.getsize(ass_path), task_id, e,
            )
            return ass_path
        logger.exception("[bridge] asr_subtitle 失败 task=%s: %s", task_id, e)
        return ""
    return ass_path if os.path.exists(ass_path) else ""


async def download_materials(
    task_id: str,
    search_terms: list[str],
    audio_duration: float,
    *,
    session_id: str = "",
    batch_index: int | None = None,
    batch_total: int | None = None,
    topic: str = "",
) -> list[str]:
    """下载素材（需 Pexels/Pixabay key）。返回素材绝对路径列表。"""
    td = task_dir(task_id, session_id=session_id)
    aspect = engine_schema.VideoAspect(settings.video_aspect)
    concat_mode = engine_schema.VideoConcatMode(settings.video_concat_mode)
    await _emit_progress(
        "materials", "🔍 搜索 & 下载素材",
        task_id=task_id, batch_index=batch_index, batch_total=batch_total, topic=topic,
    )
    # API keys 从 settings 取（按 source 选 pexels/pixabay）
    keys_str = settings.video_pixabay_api_keys if settings.video_source == "pixabay" else settings.video_pexels_api_keys
    api_keys = [k.strip() for k in keys_str.split(",") if k.strip()]
    paths = await asyncio.to_thread(
        engine_material.download_videos,
        search_terms=search_terms,
        source=settings.video_source,
        video_aspect=aspect,
        concat_mode=concat_mode,
        audio_duration=audio_duration,
        max_clip_duration=settings.video_clip_duration,
        save_dir=td,
        api_keys=api_keys,
    )
    return paths or []


async def render(
    task_id: str,
    audio_path: str,
    subtitle_path: str,
    video_paths: list[str],
    topic: str,
    *,
    session_id: str = "",
    batch_index: int | None = None,
    batch_total: int | None = None,
    font_size: int = 0,
) -> str:
    """combine_videos（拼接）+ generate_video（叠音轨/字幕/BGM）→ final mp4。返回最终路径。
    末尾发 render_done 事件 → chat.py 合成该轮 render_video tool_result（翻播放器）。"""
    td = task_dir(task_id, session_id=session_id)
    combined = os.path.join(td, "combined-1.mp4")
    combined_tmp = combined + ".tmp.mp4"  # 中间文件：.tmp 语义（未完成）+ .mp4 扩展（ffmpeg/moviepy 需要）
    final = os.path.join(td, "final-1.mp4")
    final_tmp = final + ".tmp.mp4"  # 同理
    aspect = engine_schema.VideoAspect(settings.video_aspect)
    concat_mode = engine_schema.VideoConcatMode(settings.video_concat_mode)

    # combine：step_is_done 双保险（manifest done + combined 有效）→ skip；否则 combine 写 .tmp + 原子 rename
    if step_is_done(task_id, "combine", session_id=session_id):
        logger.info("[bridge] combine 跳过（manifest done + 文件有效）")
    else:
        # 清理上次 combine 中断留下的残缺中间文件
        if os.path.exists(combined_tmp):
            try:
                os.remove(combined_tmp)
            except OSError:
                pass
        await _emit_progress(
            "combine", "🎬 拼接素材片段",
            task_id=task_id, batch_index=batch_index, batch_total=batch_total, topic=topic,
        )
        await asyncio.to_thread(
            engine_composer.combine_videos,
            output_path=combined_tmp,
            video_paths=video_paths,
            audio_path=audio_path,
            aspect=aspect,
            concat_mode=concat_mode,
            max_clip_duration=settings.video_clip_duration,
        )
        # combine 完整完成 → 原子 rename .tmp → 正式 combined
        os.replace(combined_tmp, combined)
        mark_step(task_id, "combine", "done", session_id=session_id, combined_path=combined)
        logger.info("[bridge] combine 完成，原子 rename + manifest -> %s", combined)
    # VideoParams
    # font_size 优先级：对话指定(state.user_font_size) > config.toml(video_font_size)
    params = engine_schema.VideoParams(
        video_subject=topic,
        subtitle_position="bottom",
        font_name=settings.video_font_name,
        voice_volume=1.0,
        font_size=font_size or settings.video_font_size,
    )
    # generate：step_is_done 双保险 → skip；否则 generate 写 .tmp + 原子 rename（根治 partial-final）
    if step_is_done(task_id, "generate", session_id=session_id):
        logger.info("[bridge] generate 跳过（manifest done + final 有效）")
    else:
        await _emit_progress(
            "generate", "🎞️ 叠音轨 / 字幕 / BGM",
            task_id=task_id, batch_index=batch_index, batch_total=batch_total, topic=topic,
        )
        await asyncio.to_thread(
            engine_composer.generate_video,
            video_path=combined,
            audio_path=audio_path,
            subtitle_path=subtitle_path or "",
            output_path=final_tmp,
            params=params,
        )
        # generate 完整完成 → 原子 rename .tmp → 正式 final
        os.replace(final_tmp, final)
        mark_step(task_id, "generate", "done", session_id=session_id, final_path=final)
        logger.info("[bridge] generate 完成，原子 rename + manifest -> %s", final)
    logger.info("[bridge] render done -> %s", final)
    await _emit_render_done(task_id, final, batch_index=batch_index, batch_total=batch_total, topic=topic)
    return final
