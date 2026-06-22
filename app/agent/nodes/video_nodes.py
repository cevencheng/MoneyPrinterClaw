"""
视频创作节点（内联进主 Agent 图）

流程：video_start → chief_editor → director → request_review[interrupt]
      → resource_prep → render → (回 agent 总结）

- chief_editor / director 已迁移到 agent/subagents/factory.py：按 configs/*.yaml
  编译为子图后挂载（YAML 驱动），本模块仅保留其余节点。
- request_review 用 LangGraph interrupt() 暂停等待人类审稿；
  Command(resume={script_text,storyboard}) 恢复并带入用户改稿。
- resource_prep / render：接入 bridge（TTS / 素材下载 / 渲染）。
- 脚本/分镜不写入 messages（避免污染对话），由前端评审卡展示。
"""

from __future__ import annotations

import logging
import os
import uuid

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import interrupt

from agent.config import settings
from agent.video import bridge

logger = logging.getLogger(__name__)


def _extract_plan_video(state) -> tuple[str, str, int]:
    """从 messages 里找最近的 plan_video tool_call，返回 (topic, tool_call_id, subtitle_size)。

    subtitle_size：用户对话指定的字幕字号（0=未指定，用 config 默认）。
    """
    for msg in reversed(state.get("messages", [])):
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("name") == "plan_video":
                args = tc.get("args") or {}
                topic = args.get("topic", "")
                size = args.get("subtitle_size") or 0
                return topic, tc.get("id", ""), int(size)
    return "", "", 0


# 批量失败轮重试上限（共 MAX_RETRY+1 次尝试）
MAX_RETRY = 2


def _prepare_video_round(topic: str, font_size: int = 0) -> dict:
    """为单轮视频准备干净上下文：强制新 task_id（隔离目录）+ 彻底重置创意/生产字段。

    多轮隔离的关键（单视频多轮 & 批量串行共用此函数）：
    - 新 task_id 不复用 state 残留：避免① 同目录产物互相覆盖；② render 的 skip-combine 误命中
      上一轮 combined-1.mp4（产出"画面A+音轨B"错乱视频）。
    - 重置 script_text/storyboard/script_review：避免 Supervisor route 看到上一轮残留直接 FINISH。
    - 断点续传不受影响：续跑从 render/resource_prep 的 checkpoint 继续，不重跑本准备逻辑，
      task_id 在 checkpoint 里持久，续跑仍用本轮 id。

    font_size：用户指定的字幕字号透传到 user_font_size（0=未指定，bridge 回退 config）。
    批量多轮共用同一字号（batch_start 提取一次，每轮透传）。
    """
    task_id = uuid.uuid4().hex[:16]  # 16 位 hex 足矣：父目录已按 session_id 隔离，冲突空间仅限单会话内
    return {
        "video_topic": topic,
        "video_task_id": task_id,
        "research_notes": f"主题：{topic}" if topic else "",
        "script_text": "",
        "script_review": None,
        "storyboard": [],
        "audio_paths": [],
        "video_clips": [],
        "timeline_config": {},
        "final_video_path": "",
        "user_font_size": font_size,
        # 创意期计数器归零：扁平化后 supervisor_iteration 上摆进顶层 AgentState，
        # 跨轮会残留上一轮的值 → 下一轮 supervisor_route 误判迭代上限/计数错位。
        # 每轮新视频开始强制物理清零，斩断上一轮遗毒。
        "supervisor_iteration": 0,
        "supervisor_next": "",
        # 失败重试计数 + 路由标志归零：上一个视频若失败会留下 video_retry_count=2 与
        # video_route="resource_prep"，本视频首次 render 失败时 render_node 会误判"已超上限"
        # 直接彻底失败（甚至死循环）。每轮新视频强制清零，恢复完整重试预算。
        "video_retry_count": 0,
        "video_route": "",
    }


async def video_start_node(state, config):
    """单视频入口：从 plan_video 提取主题 + 字号 → 准备干净上下文（新 task_id + 全字段重置）。

    入口处 dispatch creative_started：让前端在 supervisor 派单前 < 1s 就能看到「智能编委会」
    区块出现，而不是干等 30~60s。携带 task_id 让 chat.py 建立 creative_states 跨事件累积位。
    """
    topic, _, font_size = _extract_plan_video(state)
    update = _prepare_video_round(topic, font_size)
    # 会话归属（thread_id = session_id）：bridge 按 video_tasks/<session_id>/<task_id>/ 分组产物。
    # 持久化进 checkpoint → 续跑（/continue）从 state 读，不依赖续跑时 config 是否带 thread_id。
    update["video_session_id"] = (config.get("configurable") or {}).get("thread_id", "")
    logger.info("[video/start] topic=%s task_id=%s font_size=%s", topic, update["video_task_id"], font_size or "默认")
    try:
        await adispatch_custom_event(
            "creative_started",
            {
                "task_id": update["video_task_id"],
                "topic": topic,
                "batch_index": None,
                "batch_total": None,
            },
        )
    except Exception:
        logger.debug("[video/start] creative_started skipped (no stream context)")
    return update


def request_review_node(state):
    """人类审稿断点：interrupt 暂停；Command(resume={script_text,storyboard}) 恢复并带入改稿。"""
    payload = {
        "script_text": state.get("script_text", ""),
        "storyboard": state.get("storyboard", []),
    }
    edited = interrupt(payload)
    if isinstance(edited, dict):
        return {
            "script_text": edited.get("script_text", payload["script_text"]),
            "storyboard": edited.get("storyboard", payload["storyboard"]),
        }
    return {}


async def resource_prep_node(state):
    """
    资源准备：TTS → ASR 字幕 → 素材下载（顺序执行）。
    失败不抛异常，而是写空产物，render 阶段回 ToolMessage 说明原因。

    manifest 三步独立（升级前是 audio 步同时背 audio_path+subtitle_path）：
    - audio：仅 audio.mp3 + audio_duration（TTS 退化为纯 text→audio）
    - subtitle：subtitle.ass（faster-whisper ASR 词级时间戳 → ASS）
    - materials：clip 列表

    向后兼容：升级前留下的 task 目录 steps.audio.subtitle_path 不再读,会触发 ASR 单步重做。
    单次 1-2 秒可接受;老 srt 文件不删,任务目录隔离无副作用。
    """
    task_id = state.get("video_task_id", "")
    session_id = state.get("video_session_id", "")
    script = state.get("script_text", "")
    shots = state.get("storyboard", [])
    batch_index = state.get("batch_index")
    batch_total = state.get("batch_total")
    topic = state.get("video_topic", "")
    if not task_id or not script:
        return {"audio_paths": [], "video_clips": [], "timeline_config": {"error": "missing task_id/script"}}

    search_terms = [s.get("search_prompt", "") for s in shots if s.get("search_prompt")]

    # 任务级 meta：把 topic + 原始文案 + 分镜 + 搜索关键词写到 progress.json 顶层。
    # storyboard / search_terms 落盘便于事后排查"为何这条视频素材为空"类问题
    # （director 生成空分镜 → search_terms 空 → resource_prep 跳过下载 → videos=0）。
    # mark_meta 幂等,每次 resource_prep（含补做重跑）覆盖写最新值。
    bridge.mark_meta(
        task_id, session_id=session_id,
        topic=topic, script_text=script,
        storyboard=shots, search_terms=search_terms,
    )

    # ---- audio 步（TTS 纯音频）----
    if bridge.step_is_done(task_id, "audio", session_id=session_id):
        m = bridge.load_progress(task_id, session_id=session_id)["steps"]["audio"]
        audio_path = m["audio_path"]
        audio_duration = m.get("audio_duration", 0.0)
        logger.info("[video/resource_prep] audio 步跳过（manifest done + 文件有效）")
    else:
        audio_path = await bridge.tts(
            task_id, script, session_id=session_id, batch_index=batch_index, batch_total=batch_total, topic=topic,
        )
        audio_duration = await bridge.get_audio_duration(audio_path) if audio_path else 0.0
        if audio_path:
            bridge.mark_step(task_id, "audio", "done", session_id=session_id,
                             audio_path=audio_path, audio_duration=audio_duration)

    # ---- subtitle 步（faster-whisper ASR → ASS）----
    if bridge.step_is_done(task_id, "subtitle", session_id=session_id):
        s = bridge.load_progress(task_id, session_id=session_id).get("steps", {}).get("subtitle", {})
        subtitle_path = s.get("subtitle_path", "")  # 字幕 disabled 时 step_is_done True 但 path 空
        if subtitle_path:
            logger.info("[video/resource_prep] subtitle 步跳过（manifest done + 文件有效）")
    elif audio_path:
        subtitle_path = await bridge.asr_subtitle(
            task_id, script, audio_path, session_id=session_id,
            batch_index=batch_index, batch_total=batch_total, topic=topic,
        )
        if subtitle_path:
            bridge.mark_step(task_id, "subtitle", "done", session_id=session_id,
                             subtitle_path=subtitle_path, engine="asr")
    else:
        subtitle_path = ""

    # 🛡️ 文件级兜底:即便 bridge.asr_subtitle 返回 ""(异常路径,如 engine 写完 ass 后
    # logger 编码异常被外层 try 吞了),只要 task 目录里 subtitle.ass 真实存在且非空,
    # 就认它并补登记 manifest。修复"产物已可用却被当失败丢弃 → render 没字幕"的边界。
    if not subtitle_path and task_id and settings.video_subtitle_enabled:
        candidate = os.path.join(bridge.task_dir(task_id, session_id=session_id), "subtitle.ass")
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            logger.warning(
                "[video/resource_prep] subtitle_path 空但 subtitle.ass 在磁盘(%d B),接收兜底",
                os.path.getsize(candidate),
            )
            subtitle_path = candidate
            bridge.mark_step(task_id, "subtitle", "done", session_id=session_id,
                             subtitle_path=subtitle_path, engine="asr-recovered")

    # ---- materials 步 ----
    video_paths: list[str] = []
    if bridge.step_is_done(task_id, "materials", session_id=session_id):
        video_paths = bridge.load_progress(task_id, session_id=session_id)["steps"]["materials"].get("clips", [])
        logger.info("[video/resource_prep] materials 步跳过（manifest done, %d clips）", len(video_paths))
    else:
        try:
            if search_terms and audio_duration > 0 and settings.video_source in ("pexels", "pixabay"):
                # 默认 random/sequential 用关键词池；aligned 会在 Phase D+ 用 download_videos_per_sentence
                video_paths = await bridge.download_materials(
                    task_id, search_terms, audio_duration, session_id=session_id,
                    batch_index=batch_index, batch_total=batch_total, topic=topic,
                )
        except Exception as e:
            logger.exception("[video/resource_prep] material download failed: %s", e)
            # 继续跑，render 会处理无素材的情况
        # 仅当实际下载到素材才标 done（空=软失败，保持 pending，下次可重试；避免空结果被永久跳过）
        if video_paths:
            bridge.mark_step(task_id, "materials", "done", session_id=session_id, clips=video_paths)

    logger.info(
        "[video/resource_prep] audio=%s duration=%.2f subtitle=%s videos=%d",
        audio_path,
        audio_duration,
        subtitle_path,
        len(video_paths),
    )
    return {
        "audio_paths": [audio_path] if audio_path else [],
        "video_clips": video_paths,
        "timeline_config": {"subtitle_path": subtitle_path, "audio_duration": audio_duration},
    }


async def render_node(state):
    """
    渲染：combine_videos（拼接）+ generate_video（叠音轨/字幕/BGM）。

    失败自愈（"一任务一卡"）：TTS/render 偶发失败时复用同 task_id 局部重试,不新建任务、
    不新建卡 —— 卡原地从红(failed)翻回绿(running),progress.json 跳过已完成子步只重试失败的。
    - 失败 + retry 未耗尽：不发 ToolMessage(避免重试期间孤儿消息 + 防 agent 提前收到失败盲目"二胎"
      下发新 plan_video),dispatch render_failed → after_render 回 resource_prep 重做;
      回 resource_prep 前发一条 self_heal 弹幕把"红→绿"闪烁变成"系统正在自愈"的产品秀。
    - 彻底失败(retry 耗尽)：回 ToolMessage 唤醒 agent —— tool_call_id 严格=plan_video 下发 ID(真理 ID),
      content 注入战败宣言,钳制 agent 向用户报错、封死盲目"二胎"。
    - 成功：回 ToolMessage(不变)+ bridge.render 内部已发 render_done(翻播放器);retry_count reset 0。
    """
    task_id = state.get("video_task_id", "")
    session_id = state.get("video_session_id", "")
    audio_paths = state.get("audio_paths", [])
    video_clips = state.get("video_clips", [])
    timeline = state.get("timeline_config", {}) or {}
    subtitle_path = timeline.get("subtitle_path", "")
    batch_index = state.get("batch_index")
    batch_total = state.get("batch_total")
    topic = state.get("video_topic", "")
    # _extract_plan_video 返回 (topic, tool_call_id, size);取第二项 = plan_video 下发 ID(真理 ID)。
    # 解包顺序不能错:写成 `tool_call_id, _, _` 会拿 topic 当 id → ToolMessage.tool_call_id 变主题串
    # → DeepSeek 400 "tool must be response to preceding tool_calls"(反向孤儿)。
    _, tool_call_id, _ = _extract_plan_video(state)
    retry_count = state.get("video_retry_count", 0)

    error_msg = ""
    final_path = ""
    if not task_id:
        error_msg = "视频任务 ID 丢失，无法渲染。"
    elif not audio_paths:
        error_msg = "TTS 配音未生成，无法渲染。"
    elif not video_clips:
        error_msg = "未下载到任何视频素材（可能缺少 Pexels/Pixabay API Key），无法渲染。"
    else:
        try:
            final_path = await bridge.render(
                task_id=task_id,
                audio_path=audio_paths[0],
                subtitle_path=subtitle_path,
                video_paths=video_clips,
                topic=topic,
                session_id=session_id,
                batch_index=batch_index,
                batch_total=batch_total,
                font_size=state.get("user_font_size", 0),
            )
        except Exception as e:
            # 保留完整异常（含 ffmpeg stderr）便于诊断 combine/generate 崩溃根因
            logger.exception("[video/render] render failed (task=%s): %s", task_id, e)
            error_msg = f"渲染失败：{type(e).__name__}: {e}"

    # ---- 成功：回 ToolMessage + retry reset（bridge.render 内部已发 render_done 翻播放器）----
    if final_path:
        content = f"视频创作完成。主题：{topic}；产物路径：{final_path}"
        msgs = []
        if tool_call_id:
            msgs.append(ToolMessage(content=content, tool_call_id=tool_call_id))
        return {
            "final_video_path": final_path,
            "messages": msgs,
            "script_text": "",
            "storyboard": [],
            "video_retry_count": 0,  # 成功 reset
            "video_route": "agent",  # 成功回总结
        }

    # ---- 失败：能重试就发 render_failed + 自愈弹幕,回 resource_prep(不发 ToolMessage)----
    # 彻底失败(retry 耗尽)才回带真理 ID + 战败宣言的 ToolMessage 唤醒 agent 报错。
    logger.warning(
        "[video/render] 失败 task=%s retry=%d/%d error=%s",
        task_id, retry_count, MAX_RETRY, error_msg,
    )
    await bridge._emit_render_failed(
        task_id, retry_count=retry_count, max_retry=MAX_RETRY,
        batch_index=batch_index, batch_total=batch_total, topic=topic,
    )
    if retry_count < MAX_RETRY:
        # 回 resource_prep 前先发自愈弹幕,把"红→绿"闪烁变成"系统正在自愈"的产品秀。
        await bridge._emit_self_heal(
            task_id, retry_count,
            batch_index=batch_index, batch_total=batch_total, topic=topic,
        )
        return {
            "video_retry_count": retry_count + 1,  # resource_prep 重做,同 task_id 同卡
            "video_route": "resource_prep",  # 复用同 task_id 重做,不唤醒 agent
            # 不回 ToolMessage:重试循环在 resource_prep↔render 内,不唤醒 agent,无孤儿风险
        }

    # ---- 彻底失败:真理 ID + 战败宣言,钳制 agent 报错、封死盲目"二胎"----
    defeat_content = (
        f"Error: 视频渲染因底层不可抗力硬件/网络异常，在连续重试 {MAX_RETRY + 1} 次后宣告彻底失败。"
        f"失败原因：{error_msg}。请停止重试，直接向用户报错并陈述原因。"
    )
    msgs = []
    if tool_call_id:
        msgs.append(ToolMessage(content=defeat_content, tool_call_id=tool_call_id))
    # 锁3:把失败任务登记进 batch_results(单视频也写,仅一条)→ batch_redo_start 的
    # redo_failed 逻辑天然能取到 → 前端 failed 卡"重试"按钮(/redo-failed 入口)对单视频也生效,
    # 用户手动重试可原地复活(复用 task_id,progress.json 跳过已完成子步)。
    batch_results = list(state.get("batch_results", []) or [])
    existing = next((r for r in batch_results if r.get("task_id") == task_id), None)
    if existing:
        existing["final_video_path"] = ""
        existing["script_text"] = existing.get("script_text") or state.get("script_text", "")
        existing["storyboard"] = existing.get("storyboard") or state.get("storyboard", [])
    else:
        batch_results.append({
            "topic": topic,
            "task_id": task_id,
            "final_video_path": "",
            "script_text": state.get("script_text", ""),
            "storyboard": state.get("storyboard", []),
        })
    return {
        "messages": msgs,
        "batch_results": batch_results,  # 单视频失败也登记,供 batch_redo_start 手动重试
        "video_retry_count": 0,  # 彻底失败回 agent,reset 供下次新任务
        "video_route": "agent",  # 彻底失败回 agent（已带战败 ToolMessage 钳制,封死盲目"二胎"）
    }


# ===== 批量视频（Phase 1 串行队列，plan_video_batch 触发）=====
def _extract_plan_video_batch(state) -> tuple[list[str], bool, str, int]:
    """从 messages 找最近的 plan_video_batch tool_call，返回 (topics, auto_review, tool_call_id, subtitle_size)。"""
    for msg in reversed(state.get("messages", [])):
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("name") == "plan_video_batch":
                args = tc.get("args") or {}
                topics = args.get("topics") or []
                if isinstance(topics, str):
                    topics = [topics]
                auto_review = bool(args.get("auto_review", True))
                size = args.get("subtitle_size") or 0
                return list(topics), auto_review, tc.get("id", ""), int(size)
    return [], True, "", 0


async def batch_start_node(state):
    """批量入口：从 plan_video_batch 提取主题列表 + 字号，初始化队列；预发 batch_queued 占位事件，
    让前端一开始就看到所有 N 个任务的排队卡片（1/2 · 咖啡 排队中、2/2 · 猫咪 排队中）。"""
    topics, auto_review, tool_call_id, font_size = _extract_plan_video_batch(state)
    # 用户在 UI 选的运行模式优先于模型在 tool_call 里填的 auto_review（⚡全自动 = 跳审稿）
    if state.get("user_auto_review") is not None:
        auto_review = state.get("user_auto_review")
    topics = [t for t in topics if t and isinstance(t, str)]
    logger.info("[video/batch_start] topics=%d auto_review=%s font_size=%s", len(topics), auto_review, font_size or "默认")
    if topics:
        try:
            await adispatch_custom_event(
                "batch_queued",
                {
                    "items": [{"index": i + 1, "topic": t} for i, t in enumerate(topics)],
                    "total": len(topics),
                },
            )
        except Exception:
            logger.debug("[video/batch_start] batch_queued skipped (no stream context)")
    return {
        "batch_queue": topics,
        "batch_index": 0,
        "batch_total": len(topics),
        "batch_results": [],
        "batch_auto_review": auto_review,
        "user_font_size": font_size,  # 整批统一字号（0=未指定，bridge 回退 config）
        # 持久化 tool_call_id：messages 被 keep_last_k 截断后,batch_summary 反查不到也能拿到
        "batch_tool_call_id": tool_call_id,
    }


async def batch_dispatch_node(state, config):
    """批量循环控制器：记录上一轮结果（index>0），pop 下一轮主题并准备干净上下文；
    队列空 → batch_route=summary。

    失败重试：上一轮 final_video_path 空（render 失败）且 retry_count < MAX_RETRY → 不进下一轮，
    复用 task_id/script/storyboard，路由到 resource_prep（跳过 creative），让 progress.json
    skip 已完成子步、只重做失败的（combine 崩→redo combine；materials 空→重新下载）。
    成功或重试耗尽 → 记录结果 + 派发下一轮（新 task_id）+ 重置 retry_count。

    每当本节点决定 batch_route="supervisor_route"（正常轮 + 补做轮无 script 降级），就 dispatch
    creative_started 让前端那一轮的卡片立刻挂上「智能编委会」区块。"""
    results = list(state.get("batch_results", []))
    queue = list(state.get("batch_queue", []))
    index = state.get("batch_index", 0)
    retry_count = state.get("batch_retry_count", 0)

    # 记录上一轮（index>0 = 刚从 render 回来）
    if index > 0:
        prev_final = state.get("final_video_path", "")
        # 失败重试：final 空 + retry 未耗尽 → 复用本轮，重做 resource_prep + render
        if not prev_final and retry_count < MAX_RETRY:
            logger.warning(
                "[video/batch_dispatch] 轮 %d 失败（final 空），重试 %d/%d（复用 task_id=%s）",
                index, retry_count + 1, MAX_RETRY, state.get("video_task_id"),
            )
            return {
                "batch_retry_count": retry_count + 1,
                "batch_route": "resource_prep",  # 跳过 creative，重做 resource_prep + render
            }
        # 成功 或 重试耗尽 → 记录结果（upsert：补做轮 task_id 已存在则更新，否则 append）
        prev_tid = state.get("video_task_id", "")
        existing = next((r for r in results if r.get("task_id") == prev_tid), None)
        if existing:
            # 补做回路：原地更新 final_video_path（成功由 "" 变有效路径；仍失败保持 ""）
            existing["final_video_path"] = prev_final
            if not prev_final:
                # 仍失败：确保 script/storyboard 在（供下次补做复用）
                existing["script_text"] = existing.get("script_text") or state.get("script_text", "")
                existing["storyboard"] = existing.get("storyboard") or state.get("storyboard", [])
            logger.info("[video/batch_dispatch] 轮 task=%s upsert final=%s", prev_tid, "成功" if prev_final else "仍失败")
        else:
            entry = {
                "topic": state.get("video_topic", ""),
                "task_id": prev_tid,
                "final_video_path": prev_final,
            }
            # 失败轮补存 script/storyboard，供 batch_redo_start 构造补做队列（跳过 creative）
            if not prev_final:
                entry["script_text"] = state.get("script_text", "")
                entry["storyboard"] = state.get("storyboard", [])
                logger.warning("[video/batch_dispatch] 轮 %d 重试耗尽，记录失败（已存 script/storyboard）→ 下一轮", index)
            else:
                logger.info("[video/batch_dispatch] 轮 %d 成功", index)
            results.append(entry)
        retry_count = 0  # 进下一轮，重置

    if queue:
        head = queue[0]
        rest = queue[1:]
        # 会话归属（thread_id = session_id），与本批所有轮一致；bridge 按 video_tasks/<session_id>/<task_id>/ 分组。
        session_id = (config.get("configurable") or {}).get("thread_id", "")
        # 补做轮（dict）：复用原 task_id + 注入 script/storyboard，跳过 creative 直接 resource_prep
        if isinstance(head, dict):
            topic = head.get("topic", "")
            script = head.get("script_text", "")
            update = {
                "video_topic": topic,
                "video_task_id": head["task_id"],  # 复用原 task_id（progress.json 命中 → 跳已完成子步）
                "video_session_id": session_id,
                "research_notes": f"主题：{topic}" if topic else "",
                "script_text": script,
                "script_review": None,
                "storyboard": head.get("storyboard", []),
                "audio_paths": [],   # resource_prep 会按 progress.json 恢复或重做后回填
                "video_clips": [],
                "timeline_config": {},
                "final_video_path": "",
                "user_font_size": state.get("user_font_size", 0),  # 补做时继承批次字号
                "batch_queue": rest,
                # batch_index 用原批次位置 → 前端事件在原失败卡片上原地更新（不抖到 1/N）
                "batch_index": head.get("original_index", index + 1),
                "batch_results": results,
                "batch_retry_count": retry_count,
                # 创意期计数器归零（同 _prepare_video_round）：降级 creative 重生成时斩断上一轮遗毒
                "supervisor_iteration": 0,
                "supervisor_next": "",
                # 失败重试计数 + 路由标志归零（同 _prepare_video_round）：补做轮恢复完整重试预算
                "video_retry_count": 0,
                "video_route": "",
                # 有 script → 跳 creative 直接 resource_prep；无 script（历史数据/creative 崩）→ 降级 creative 重生成
                "batch_route": "resource_prep" if script else "supervisor_route",
            }
            logger.info(
                "[video/batch_dispatch] 补做轮 %s task=%s topic=%s route=%s",
                update["batch_index"], head["task_id"], topic, update["batch_route"],
            )
            # 仅在降级 creative（无 script）时 dispatch；正常补做跳 creative，不需要编委会区块
            if update["batch_route"] == "supervisor_route":
                try:
                    await adispatch_custom_event(
                        "creative_started",
                        {
                            "task_id": update["video_task_id"],
                            "topic": topic,
                            "batch_index": update["batch_index"],
                            "batch_total": state.get("batch_total"),
                        },
                    )
                except Exception:
                    logger.debug("[video/batch_dispatch] creative_started skipped")
            return update
        # 正常轮（str）：新 task_id + 全字段重置 + creative
        topic = head
        total = index + len(queue)
        update = _prepare_video_round(topic, state.get("user_font_size", 0))
        update.update({
            "video_session_id": session_id,
            "batch_queue": rest,
            "batch_index": index + 1,
            "batch_results": results,
            "batch_retry_count": retry_count,
            "batch_route": "supervisor_route",
        })
        logger.info("[video/batch_dispatch] 派发 %d/%d topic=%s", index + 1, total, topic)
        try:
            await adispatch_custom_event(
                "creative_started",
                {
                    "task_id": update["video_task_id"],
                    "topic": topic,
                    "batch_index": update["batch_index"],
                    "batch_total": state.get("batch_total"),
                },
            )
        except Exception:
            logger.debug("[video/batch_dispatch] creative_started skipped")
        return update

    logger.info("[video/batch_dispatch] 队列空，%d 轮完成 → batch_summary", index)
    return {"batch_results": results, "batch_route": "batch_summary"}


def batch_summary_node(state):
    """批量收尾：汇总各轮结果 → 一条 ToolMessage（对准 plan_video_batch 的 tool_call_id）。

    tool_call_id 优先取 state.batch_tool_call_id（batch_start 持久化）；
    回退反查 messages 仅作兼容（messages 被 keep_last_k 截断后反查可能失败,故不依赖之）。

    补做收尾的重复应答防护：首次 batch_summary 已写过一条 ToolMessage(PVB_id)（对准
    plan_video_batch 的 tool_call_id）。用户点重试 → batch_redo_start 重跑补做 → 补做收尾
    batch_summary 再写一条同 id 的 ToolMessage → 同 tool_call_id 出现两条,且第二条前面
    不是 AIMessage(tool_calls) → DeepSeek 严格校验 400 "tool must be response to preceding
    tool_calls"。检测到 state.messages 已有同 tool_call_id 的 ToolMessage → 改回 HumanMessage
    （无需配对 AIMessage(tool_calls),不触发 400）。首次 batch_summary 窗口内无重复 → 仍走
    ToolMessage 路径,与此防护不冲突。
    """
    results = state.get("batch_results", [])
    tool_call_id = state.get("batch_tool_call_id", "") or _extract_plan_video_batch(state)[2]
    ok = sum(1 for r in results if r.get("final_video_path"))
    lines = [
        f"{i + 1}. {r.get('topic', '?')} → "
        + (r.get("final_video_path", "").split("video_tasks/")[-1] if r.get("final_video_path") else "失败")
        for i, r in enumerate(results)
    ]
    content = f"批量创作完成：{ok}/{len(results)} 个视频成功。\n" + "\n".join(lines)

    already_responded = False
    if tool_call_id:
        for m in state.get("messages", []):
            if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", "") == tool_call_id:
                already_responded = True
                break

    if already_responded:
        # 补做收尾：首次的 ToolMessage(PVB_id) 已在窗口内,改 HumanMessage 避免同 id 双响 → 400
        msgs = [HumanMessage(content=content)]
        logger.info("[video/batch_summary] %d/%d ok（改 HumanMessage：同 tool_call_id 已应答过）", ok, len(results))
    else:
        msgs = [ToolMessage(content=content, tool_call_id=tool_call_id)] if tool_call_id else []
        logger.info("[video/batch_summary] %d/%d ok", ok, len(results))
    return {"messages": msgs}


def _extract_redo_failed_tool_call_id(state) -> str:
    """从 messages 找最近的 redo_failed_videos tool_call，返回其 id（入口 A 有；入口 B Command goto 无）。"""
    for msg in reversed(state.get("messages", [])):
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("name") == "redo_failed_videos":
                return tc.get("id", "")
    return ""


def batch_redo_start_node(state):
    """补做入口：从 batch_results 挑失败轮（final_video_path 空 + 有 task_id），构造补做队列。

    复用失败轮原 task_id（progress.json 命中 → 跳过已完成 audio/materials/combine 子步，
    只重做失败的）；带 original_index 让前端事件在原失败卡片上原地更新（batch_total 不变）。
    无失败轮 → 回 agent 提示，不进循环。

    入口 A（redo_failed_videos 工具）需回对应 tool_call_id 的 ToolMessage（避免孤儿 tool_call）；
    入口 B（/redo-failed 端点 Command goto）无 tool_call_id → 回 HumanMessage。

    入口 A 重复应答防护：agent 可能多次调用 redo_failed_videos（补做收尾后 agent 见仍有失败再调一次），
    每次进 batch_redo_start 时 _extract_redo_failed_tool_call_id 反查 messages 都命中同一条
    redo_failed_videos 的 AIMessage → 用同一 tool_call_id 写第二条 ToolMessage → 同 id 双响,
    第二条前面不是 AIMessage(tool_calls) → DeepSeek 400。检测 messages 已有同 redo_id 的
    ToolMessage → 改 HumanMessage（无需配对 AIMessage(tool_calls)）。首次入口 A 窗口内无重复
    → 仍走 ToolMessage 路径。
    """
    results = list(state.get("batch_results", []))
    failed = [(i, r) for i, r in enumerate(results) if not r.get("final_video_path") and r.get("task_id")]
    redo_id = _extract_redo_failed_tool_call_id(state)

    # 入口 A 重复应答检测：messages 已有同 redo_id 的 ToolMessage → 改 HumanMessage
    redo_already_responded = False
    if redo_id:
        for m in state.get("messages", []):
            if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", "") == redo_id:
                redo_already_responded = True
                break

    def _reply(content: str):
        if not redo_id:
            return HumanMessage(content=content)
        if redo_already_responded:
            # 同 redo_id 已应答过,改 HumanMessage 避免第二条 ToolMessage 触发 400
            return HumanMessage(content=content)
        return ToolMessage(content=content, tool_call_id=redo_id)

    if not failed:
        logger.info("[video/batch_redo_start] 无失败轮，跳过补做 → agent")
        return {"messages": [_reply("当前没有失败的视频需要补做。")]}

    redo_queue = [
        {
            "topic": r.get("topic", ""),
            "task_id": r["task_id"],
            "script_text": r.get("script_text", ""),
            "storyboard": r.get("storyboard", []),
            "original_index": i + 1,  # 原批次位置（1-based，前端卡片 id）
        }
        for i, r in failed
    ]
    logger.info("[video/batch_redo_start] 补做 %d 个失败轮（复用原 task_id）", len(redo_queue))
    return {
        "batch_queue": redo_queue,
        "batch_index": 0,  # batch_dispatch 会用 original_index 覆盖
        "batch_results": results,  # 保留成功轮；补做成功后由 batch_dispatch upsert 原地更新
        "messages": [_reply(f"开始补做 {len(redo_queue)} 个失败视频（复用原任务，跳过已完成子步）。")],
    }


# ===== 批量路由函数（build.py 的条件边用）=====
def after_batch_dispatch(state):
    """batch_dispatch 派发了下一轮 → creative；队列空 → batch_summary。
    返回值必须是条件边 path map 里的节点名（creative / batch_summary）。"""
    return state.get("batch_route", "batch_summary")


def after_redo_start(state):
    """batch_redo_start：有补做队列 → batch_dispatch；无失败轮 → agent。"""
    return "batch_dispatch" if state.get("batch_queue") else "agent"


def after_creative(state):
    """用户选 ⚡全自动（user_auto_review=False）→ 单视频 & 批量都跳过 request_review；
    否则：批量静默模式（batch_auto_review=False）跳过；单视频/批量逐个审 → 正常 request_review。"""
    if state.get("user_auto_review") is False:
        return "resource_prep"
    if state.get("batch_queue") is not None and not state.get("batch_auto_review", True):
        return "resource_prep"
    return "request_review"


def after_render(state):
    """渲染后路由。
    批量模式（batch_queue 字段存在）回 batch_dispatch 继续循环。
    单视频：读 render_node 写入的显式 video_route 字段（不再用 video_retry_count 计数器推断——
    计数器推断与 render_node 的重试/彻底失败分支错位一格,会导致第 2 次失败时不带战败
    ToolMessage 唤醒 agent,触发盲目"二胎"与幽灵卡）：
    - 成功（final_video_path 非空）→ agent（回总结）。
    - 失败 + retry 未耗尽 → resource_prep（复用同 task_id 重做,progress.json 跳过已完成子步只重试
      TTS/失败的,同张卡原地从红翻绿,不新建任务/卡）。render_node 失败时已发 render_failed + self_heal 弹幕。
    - 彻底失败（retry 耗尽）→ agent（render_node 已回带真理 ID + 战败宣言的 ToolMessage 唤醒报错）。
    """
    if state.get("batch_queue") is not None:
        return "batch_dispatch"
    if state.get("final_video_path"):
        return "agent"
    # 单视频失败：render_node 已设 video_route。retry→resource_prep / 彻底失败→agent。
    # video_route 缺失（老 checkpoint 续跑 / 防御）默认 agent,宁可报错也不死循环。
    return state.get("video_route") or "agent"
