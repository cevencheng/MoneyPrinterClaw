"""
历史会话管理（业务消息表 → reload 真相源；checkpointer → 状态机断点续传）

提供会话历史的增删改查接口：
- GET    /api/history/list           — 列出所有会话（与 /api/threads 等价,保留兼容）
- GET    /api/history/{session_id}   — 获取指定会话的消息记录
- DELETE /api/history/{session_id}   — 删除指定会话

reload 优先从业务消息表（messages）读取；老会话尚未双写则回退到 checkpointer
（_serialize_messages 沿用旧逻辑兼容）。状态机字段（is_pending_review / video_state）
仍从 checkpointer 读取,因为 reload UI 还要补"排队中"等动态卡片。
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_view.agent_runtime import delete_thread_state, get_agent_graph
from api_view.database import Message, Session, get_db
from api_view.messages_store import has_messages, list_messages

router = APIRouter()


def _stable_id(msg: Any, idx: str) -> str:
    """优先用 LangChain 消息自带 id，缺失则确定性回退。"""
    mid = getattr(msg, "id", None)
    return mid if isinstance(mid, str) and mid else f"{type(msg).__name__}-{idx}"


def _extract_text(content: Any) -> str:
    """LangChain content 可能是 str 或 content block list，统一抽出纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _rebuild_done_agent_state(values_or_state: dict) -> dict:
    """从 checkpoint values（或 video_state 同形 dict）反推"创意期 4 worker 全 done"的 agent_state。

    用于续跑场景：DB 无 assistant 但 checkpoint 在 resource_prep / render 阶段，
    说明 supervisor creative phase 早已完成，state 里 research_notes / script_text /
    script_review / storyboard 都已就位。给前端进行中卡补一份 done 状态的编委会区块,
    避免显示成"旧 4 阶段视图"。
    """
    notes = values_or_state.get("research_notes", "") or ""
    script = values_or_state.get("script_text", "") or ""
    review = values_or_state.get("script_review", {}) or {}
    storyboard = values_or_state.get("storyboard", []) or []

    history: dict[str, dict] = {}
    if notes:
        history["researcher"] = {"status": "done", "meta": "✅ 已捕获事实"}
    if script:
        history["editor"] = {"status": "done", "meta": f"📝 {len(script)}字文案"}
    if review:
        passed = bool(review.get("passed", True))
        history["reviewer"] = {"status": "done", "meta": "✅ 质检通过" if passed else "❌ 已打回"}
    if storyboard:
        history["director"] = {"status": "done", "meta": f"🎬 {len(storyboard)} 个分镜"}

    return {
        "active_node": None,
        "node_history": history,
        "log_message": "✅ 编委会已交付,渲染流水线启动...",
    }


def _enrich_db_msgs_with_missing_batch_cards(
    db_msgs: list[dict[str, Any]], values: dict, session_id: str = ""
) -> None:
    """如果 DB 末条 assistant 的 render_video 卡数 < checkpoint.batch_total,从
    batch_results 补全缺失的卡片(成功 → 播放器,失败 → 失败卡)。原地修改 db_msgs。

    场景：旧版 /continue 双 bubble bug 留下的损伤会话 —— builder 只 flush 了 1 张
    当前进行中卡到 DB,前 N-1 张已完成卡仅在 history reload 的 checkpoint 重建里展示
    过(临时,非持久),从未落进 messages 表。本函数读 batch_results 的 task_id +
    final_video_path 对照磁盘文件名,把它们补回 DB messages 视图。
    """
    bt = values.get("batch_total")
    if not bt:
        return
    batch_results = values.get("batch_results", []) or []
    if not batch_results:
        return

    # 找末条 assistant
    asst = None
    for m in reversed(db_msgs):
        if m.get("role") == "assistant":
            asst = m
            break
    if asst is None:
        return

    content = asst.get("content", []) or []
    existing_cids = {
        p.get("toolCallId") for p in content
        if p.get("type") == "tool-call" and p.get("toolName") == "render_video"
    }

    rv_count = len(existing_cids)
    if rv_count >= bt:
        return  # 已完整

    appended = 0
    for i, r in enumerate(batch_results[:bt]):
        cid = f"render-batch-{i + 1}"
        if cid in existing_cids:
            continue
        tid = r.get("task_id", "")
        path = r.get("final_video_path", "")
        topic = r.get("topic", "")
        if tid and path:
            content.append({
                "type": "tool-call",
                "toolCallId": cid,
                "toolName": "render_video",
                "args": {"batch_index": i + 1, "batch_total": bt, "topic": topic},
                "result": {"url": f"/api/files/{session_id}/{tid}/final-1.mp4", "filename": f"{tid}.mp4"},
            })
            appended += 1
        elif tid:
            content.append({
                "type": "tool-call",
                "toolCallId": cid,
                "toolName": "render_video",
                "args": {"stage": "failed", "batch_index": i + 1, "batch_total": bt, "topic": topic},
            })
            appended += 1
    if appended:
        # 按 batch_index 排序所有 render_video 卡（不破坏 text/其它 part 顺序）
        # batch_index 从 toolCallId 提取:render-batch-{i} → i
        def _bidx_key(cid: str) -> int:
            try:
                return int(cid.replace("render-batch-", ""))
            except (ValueError, AttributeError):
                return 99999  # 非批量卡放最后

        rv_parts = [p for p in content if p.get("type") == "tool-call" and p.get("toolName") == "render_video"]
        non_rv = [p for p in content if not (p.get("type") == "tool-call" and p.get("toolName") == "render_video")]
        rv_parts.sort(key=lambda p: _bidx_key(p.get("toolCallId", "")))
        asst["content"] = non_rv + rv_parts


def _serialize_messages(messages: list[Any], video_state: dict | None = None, session_id: str = "") -> list[dict[str, Any]]:
    """
    将 LangChain 消息序列化为前端统一格式 {id, role, content: Part[]}。

    - ToolMessage 结果内联合并进前一条 AIMessage 的 tool-call part.result。
    - SystemMessage 跳过（动态注入，前端不需要）。
    - 视频线程（video_state 非空）：把 plan_video tool-call 重建为 review_video_plan
      （实时流不显示 plan_video 而显示合成的评审卡，这里对齐）；若已出片，给最后一条
      assistant 追加 render_video 播放器。使 reload 与实时流 UI 逐气泡一致。
    """
    # 先按 tool_call_id 索引所有 ToolMessage 结果
    tool_results: dict[str, Any] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            tool_results[m.tool_call_id] = m.content

    is_video = bool(video_state)
    final_path = (video_state or {}).get("final_video_path", "")
    vtid = (video_state or {}).get("video_task_id", "")
    script = (video_state or {}).get("script_text", "")
    storyboard = (video_state or {}).get("storyboard", [])
    is_pending_review = bool((video_state or {}).get("is_pending_review"))

    out: list[dict[str, Any]] = []
    for idx, m in enumerate(messages):
        if isinstance(m, (SystemMessage, ToolMessage)):
            continue  # system 后端专用；tool 已合并到前一条 AIMessage
        if isinstance(m, HumanMessage):
            out.append({
                "id": _stable_id(m, str(idx)),
                "role": "user",
                "content": [{"type": "text", "text": _extract_text(m.content)}],
            })
        elif isinstance(m, AIMessage):
            parts: list[dict[str, Any]] = []
            # 文本在前、tool-call 在后（与前端流式 buildContent 顺序一致）
            text = _extract_text(m.content)
            if text:
                parts.append({"type": "text", "text": text})
            # tool_calls：优先 .tool_calls，回退 additional_kwargs
            tcs = getattr(m, "tool_calls", None)
            if not tcs and getattr(m, "additional_kwargs", None):
                tcs = m.additional_kwargs.get("tool_calls", [])
            for tc in tcs or []:
                tc_id = tc.get("id") or tc.get("tool_call_id") or ""
                tc_name = tc.get("name", "")
                # 视频线程：plan_video → review_video_plan（重建评审卡，与实时流一致）
                if is_video and tc_name == "plan_video":
                    parts.append({
                        "type": "tool-call",
                        "toolCallId": tc_id,
                        "toolName": "review_video_plan",
                        "args": {"script_text": script, "storyboard": storyboard},
                        # 已越过 request_review 断点（已确认/渲染中/已完成）→result 在（✅已确认）；
                        # 仍停在断点（pending 未确认）→None（可编辑）。与 render 是否完成无关。
                        "result": None if is_pending_review else {"script_text": script, "storyboard": storyboard},
                    })
                else:
                    parts.append({
                        "type": "tool-call",
                        "toolCallId": tc_id,
                        "toolName": tc_name,
                        "args": tc.get("args", {}) or {},
                        "result": tool_results.get(tc_id),  # 原样，与实时一致
                    })
            if parts:
                out.append({
                    "id": _stable_id(m, str(idx)),
                    "role": "assistant",
                    "content": parts,
                })

    # 找最后一条 assistant，把播放器卡片追加到它（与实时流"一条 assistant 多卡片"一致）
    target = None
    if is_video and out:
        for msg in reversed(out):
            if msg["role"] == "assistant":
                target = msg
                break

    batch_results = (video_state or {}).get("batch_results") or []
    batch_total = (video_state or {}).get("batch_total")
    batch_index = (video_state or {}).get("batch_index") or 0
    batch_queue = (video_state or {}).get("batch_queue") or []
    cur_topic = (video_state or {}).get("video_topic", "")
    cur_task_id = (video_state or {}).get("video_task_id", "")

    if target is not None and batch_total:
        # 批量：重建 已完成轮（成功→播放器 / 失败→失败卡）+ 进行中卡(带 done agent_state)+ 排队卡。
        # 防御：batch_results 可能因 continue 续跑重入等边界超过 batch_total，只取前 batch_total 个（序号不溢出）
        results_capped = batch_results[:batch_total] if batch_total else batch_results
        # 1. 已完成轮（results_capped），序号 i+1/batch_total
        for i, r in enumerate(results_capped):
            tid = r.get("task_id", "")
            path = r.get("final_video_path", "")
            topic = r.get("topic", "")
            if tid and path:
                # 成功：播放器卡
                target["content"].append({
                    "type": "tool-call",
                    "toolCallId": f"render-batch-{i + 1}",
                    "toolName": "render_video",
                    "args": {"batch_index": i + 1, "batch_total": batch_total, "topic": topic},
                    "result": {"url": f"/api/files/{session_id}/{tid}/final-1.mp4", "filename": f"{tid}.mp4"},
                })
            elif tid:
                # 失败（render 未出片）：失败卡（不跳过，让用户看到该轮状态，否则序号会缺）
                target["content"].append({
                    "type": "tool-call",
                    "toolCallId": f"render-batch-{i + 1}",
                    "toolName": "render_video",
                    "args": {"stage": "failed", "batch_index": i + 1, "batch_total": batch_total, "topic": topic},
                })
        # 2. 进行中轮（batch_index > len(results_capped) 且 ≤ batch_total)：续传场景必须重建,
        # 否则 reload 看不到当前正在跑的卡片。带 done 态 agent_state（从 video_state 反推）→
        # 显示完整智能体编委会区块（已交付灰化态）+ 渲染流水线就绪。
        if batch_index and batch_index > len(results_capped) and batch_index <= batch_total:
            target["content"].append({
                "type": "tool-call",
                "toolCallId": f"render-batch-{batch_index}",
                "toolName": "render_video",
                "args": {
                    "stage": "creative",  # 兜底 stage,/continue 真实事件会按 toolCallId 原地推进
                    "batch_index": batch_index,
                    "batch_total": batch_total,
                    "topic": cur_topic,
                    "task_id": cur_task_id,
                    "agent_state": _rebuild_done_agent_state(video_state or {}),
                    "label": "🚀 续跑准备中...",
                },
            })
        # 3. 排队卡（batch_queue 剩余主题，序号从 batch_index+1 起递增）
        for j, topic in enumerate(batch_queue):
            idx = batch_index + 1 + j
            target["content"].append({
                "type": "tool-call",
                "toolCallId": f"render-batch-{idx}",
                "toolName": "render_video",
                "args": {"stage": "queued", "label": "⏳ 排队中", "batch_index": idx, "batch_total": batch_total, "topic": topic},
            })
    elif target is not None and vtid:
        # 单视频：成功 → 播放器卡;未完成(final_path 空)→ 进行中卡(含 done agent_state)
        if final_path:
            target["content"].append({
                "type": "tool-call",
                "toolCallId": f"render-{vtid}",
                "toolName": "render_video",
                "args": {},
                "result": {
                    "url": f"/api/files/{session_id}/{vtid}/final-1.mp4",
                    "filename": f"{vtid}.mp4",
                },
            })
        else:
            # 续传场景:checkpoint 在 resource_prep / render 阶段,creative phase 已结束。
            # /continue 真实事件按相同 toolCallId 原地推进 stage(tts→asr→...)
            target["content"].append({
                "type": "tool-call",
                "toolCallId": f"render-{vtid}",
                "toolName": "render_video",
                "args": {
                    "stage": "creative",
                    "task_id": vtid,
                    "topic": cur_topic,
                    "agent_state": _rebuild_done_agent_state(video_state or {}),
                    "label": "🚀 续跑准备中...",
                },
            })
    return out


@router.get("/list")
async def list_histories(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """列出所有历史会话（按更新时间倒序）"""
    count_stmt = select(func.count()).select_from(Session)
    total = (await db.execute(count_stmt)).scalar() or 0

    list_stmt = (
        select(Session)
        .order_by(Session.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    sessions = (await db.execute(list_stmt)).scalars().all()

    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in sessions
        ],
        "total": total,
    }


def _compute_can_continue(snapshot) -> bool:
    """断点续传触发判定 —— 刚性钳制,根治"已成功任务被续传重跑"灾难。

    续传本意是恢复【未完成】的渲染(render 中途后端崩了)。但旧的 `bool(snapshot.next)`
    判定太宽:视频成功后流程停在 `next=('agent',)`(只剩文本总结没跑)也会被判成"需续传",
    而 /continue 从 agent 续跑时 LLM 可能重调 plan_video → 整条流水线重跑,把已成功的
    成片状态覆盖、task_id 重置,用户看到"做好的视频又从头来一遍"。

    硬化规则(满足任一即禁止续传,can_continue=False):
    1. 无 snapshot / 无 next → 本就空闲,不续传。
    2. pending review 中断(HITL 等用户确认)→ 不续传(走 /resume)。
    3. final_video_path 非空 → 视频成片已成功落盘,生产线已交付,严禁续传。
    4. next 仅剩主对话节点 'agent' → 生产线已交付,只剩非核心文本总结没跑,严禁续传
       (用户重发一条消息走正常 /send 即可让 agent 回复,不会重跑视频)。

    只有 render 未出片(final_path 空)且 checkpoint 真停在 creative/resource_prep/render
    等生产节点时,才允许续传恢复渲染。
    """
    if not snapshot or not snapshot.next:
        return False
    if any(getattr(t, "interrupts", None) for t in snapshot.tasks):
        return False  # HITL pending → 走 /resume,不续传
    values = snapshot.values or {}
    # 规则3:成片已落盘 → 严禁续传
    if values.get("final_video_path"):
        return False
    # 规则4:只剩 agent 文本总结 → 严禁续传(用户重发消息即可,不会重跑视频)
    if set(snapshot.next) <= {"agent"}:
        return False
    return True


@router.get("/{session_id}")
async def get_history(session_id: str, db: AsyncSession = Depends(get_db)):
    """获取指定会话的完整消息记录。

    优先从业务消息表（messages）读取（reload 主路径）；表内为空则回退到 checkpointer
    用 _serialize_messages 重建（兼容老会话或迁移前数据）。
    can_continue 始终读 checkpointer snapshot —— 它是状态机层语义,与业务消息表无关。
    """
    graph = get_agent_graph()
    config = {"configurable": {"thread_id": session_id}}
    snapshot = await graph.aget_state(config)
    values = snapshot.values if snapshot else {}
    is_pending_review = bool(snapshot and any(getattr(t, "interrupts", None) for t in snapshot.tasks))
    can_continue = _compute_can_continue(snapshot)

    # 主路径：业务消息表（必须有 assistant message 才视作权威 reload 源）
    db_msgs = await list_messages(session_id, db)
    has_assistant = any(m.get("role") == "assistant" for m in db_msgs)
    if has_assistant:
        # 完整性兜底：DB 里 render_video 卡数若 < checkpoint 的 batch_total（历史
        # /continue 双 bubble bug 留下的损伤数据）→ 用 batch_results 把缺失的卡补全。
        # 缺失播放器卡来自 batch_results 的 final_video_path（仍存在磁盘上的 mp4）;
        # 失败卡来自 final_video_path 为空的 entry。保留 DB 已有 text 摘要不动。
        if values and values.get("batch_total"):
            _enrich_db_msgs_with_missing_batch_cards(db_msgs, values, session_id)
        return {
            "session_id": session_id,
            "messages": db_msgs,
            "can_continue": can_continue,
            "messages_source": "db",
        }
    # ⚠️ 关键修复: 仅有 user message(中途崩溃 builder 未 flush)时绝不能直接返回 db_msgs ——
    # 否则用户看到的就只有自己刚发的那句话,所有续传需要的批量卡 / 单视频卡全部消失。
    # 走 checkpoint 重建路径,_serialize_messages 会从 LangChain messages + video_state
    # 完整生成 user message + assistant 消息(含已完成轮播放器卡 / 进行中卡 / 排队卡)。

    # 回退：业务消息表无 assistant → 用 checkpoint 重建（兼容老会话/迁移前 + 续传场景）
    if not values:
        return {
            "session_id": session_id,
            "messages": [],
            "can_continue": can_continue,
            "messages_source": "empty",
        }
    messages = values.get("messages", [])
    video_state = None
    if values.get("video_task_id"):
        video_state = {
            "video_task_id": values.get("video_task_id", ""),
            "video_topic": values.get("video_topic", ""),
            "is_pending_review": is_pending_review,
            # research_notes / script_review 透传给 _serialize_messages,用于重建续跑场景
            # 进行中卡的 agent_state（4 worker 全 done）
            "research_notes": values.get("research_notes", ""),
            "script_text": values.get("script_text", ""),
            "script_review": values.get("script_review", {}) or {},
            "storyboard": values.get("storyboard", []),
            "final_video_path": values.get("final_video_path", ""),
            "batch_results": values.get("batch_results", []),
            "batch_total": values.get("batch_total"),
            "batch_index": values.get("batch_index"),
            "batch_queue": values.get("batch_queue", []),
        }
    return {
        "session_id": session_id,
        "messages": _serialize_messages(messages, video_state, session_id),
        "can_continue": can_continue,
        "messages_source": "checkpoint(legacy)",
    }


@router.delete("/{session_id}")
async def delete_history(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """彻底删除会话相关全部数据：sessions + messages + checkpoints + writes 四张表。

    与 DELETE /api/threads/{id} 行为对称（前端用的是 threads 端点,本端点保兼容）。
    旧实现只删 sessions+messages,checkpoints 残留会让 stuck 会话通过 continue 续跑复活。
    """
    await db.execute(delete(Session).where(Session.session_id == session_id))
    await db.execute(delete(Message).where(Message.session_id == session_id))
    await db.commit()
    state_stats = await delete_thread_state(session_id)
    return {"status": "deleted", "session_id": session_id, **state_stats}
