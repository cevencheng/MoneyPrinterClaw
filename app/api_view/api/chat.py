"""
SSE 流式对话 + LangGraph Agent + 工具调用事件 + HITL 断点

提供：
- POST /api/chat/send    — 发送消息，SSE 流式返回。若视频流程在 request_review 挂起，
                          流末追加 review_video_plan tool_call 事件，供前端渲染评审卡。
- POST /api/chat/resume  — 用户审稿确认后，Command(resume={script_text,storyboard}) 恢复。

业务消息表（messages_store）是 reload UI 的真相源；本模块在 SSE 流末把累积事件
写入业务消息表（user 在流首写,assistant 在流末写）。Checkpointer 仅用于状态机
断点续传,流末额外做一次 prune（保留最近 K 个 checkpoint）。

SSE 事件格式：
- data: {"delta": "..."}              LLM 文本流（仅主 agent 节点）
- data: {"tool_call": {...}}          工具开始 / review_video_plan 评审卡
- data: {"tool_result": {...}}        工具执行完成
- data: {"usage": {...}}              Token 消耗统计
- data: {}                            流结束标志
"""

import json
import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from api_view.agent_runtime import get_agent_graph, prune_checkpoints
from api_view.messages_store import (
    AssistantMessageBuilder,
    append_assistant_message,
    append_user_message,
    get_last_assistant,
    replace_assistant_message,
    update_last_assistant_tool_result,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 并发守卫：正在渲染的 thread_id 集合，防止 reload-during-live 双渲染写串文件
_active_video_threads: set[str] = set()

# ===== 多 Agent UIUX 常量 =====
# supervisor worker 节点白名单（用 metadata.langgraph_node 匹配）。
# 扁平化后 worker 仍为子图挂主图：langgraph_node 可能是裸名 "researcher"，也可能带子图 ns 前缀
# （如 "researcher:run"）。统一用后缀匹配，对两种形态都鲁棒（螺母2：平台级横向扩展友好）。
SUPERVISOR_WORKERS = {"researcher", "editor", "reviewer", "director"}


def _match_worker(node: str) -> str | None:
    """langgraph_node → 命中的 worker 名；不命中返回 None。

    后缀匹配：`researcher` / `xxx:researcher` 都识别为 researcher。
    """
    if not node:
        return None
    tail = node.rsplit(":", 1)[-1]
    return tail if tail in SUPERVISOR_WORKERS else None

# route 节点决策的下游对应文案（剧组动态弹幕用）
ROUTE_LOG_TEMPLATES = {
    "researcher": "🔍 派 researcher 联网检索素材...",
    "editor":     "✍️ 让 editor 拆解爆款文案...",
    "reviewer":   "⚖️ 送 reviewer 做指标质检...",
    "director":   "🎬 派 director 编排高清分镜...",
    "FINISH":     "✅ 创意期完成,转交渲染流水线...",
}

# worker 进入 active 时的初始 meta 文案
WORKER_INIT_META = {
    "researcher": "🔍 已检索 0 次",
    "editor":     "✍️ 拆解中…",
    "reviewer":   "⚖️ 质检中…",
    "director":   "🎬 编排中…",
}


def _extract_worker_meta(node: str, output, snap: dict) -> str:
    """on_chain_end 时从 worker 子图输出里提取战果 meta。

    output 由 LangGraph 在子图结束时给出（通常是 dict {field: value}）。
    任何字段缺失/类型异常 → 退回到通用 ✅ 完成。
    """
    if not isinstance(output, dict):
        return "✅ 完成"
    if node == "researcher":
        n = snap.get("_researcher_tool_count", 0)
        if n > 0:
            return f"✅ 已检索 {n} 次"
        notes = output.get("research_notes") or ""
        if notes:
            return f"✅ 笔记 {len(notes)} 字"
        return "✅ 完成"
    if node == "editor":
        script = output.get("script_text") or ""
        if script:
            return f"📝 {len(script)} 字文案"
        return "✅ 完成"
    if node == "reviewer":
        review = output.get("script_review")
        if isinstance(review, dict):
            return "✅ 质检通过" if review.get("passed") else "❌ 已打回"
        return "⚖️ 质检完成"
    if node == "director":
        sb = output.get("storyboard") or []
        if isinstance(sb, list) and sb:
            return f"🎬 {len(sb)} 个分镜"
        return "✅ 完成"
    return "✅ 完成"


def _card_id(data: dict, fallback: str) -> str:
    """render_video 卡片 id：批量模式用 batch_index（与占位卡 render-batch-{i} 一致）；
    单视频用 task_id。fallback 为流级 render_id（兜底）。"""
    bidx, btotal = data.get("batch_index"), data.get("batch_total")
    if bidx and btotal:
        return f"render-batch-{bidx}"
    return f"render-{data.get('task_id') or fallback}"


async def _stream_response(
    input_obj,
    thread_id: str,
    render_id: str | None = None,
    builder: AssistantMessageBuilder | None = None,
    preseed_creative_states: dict[str, dict] | None = None,
) -> AsyncGenerator[str, None]:
    """
    流式运行 LangGraph Agent，生成 SSE 事件。

    - input_obj：send 传 {"messages":[HumanMessage]}；resume 传 Command(resume=...)；continue 传 None（从 checkpoint 续跑）。
    - delta 只转发主 agent 节点的 token（过滤 chief_editor / director 等内部 LLM 流）。
    - 流结束后探测 interrupt：若停在 request_review，发 review_video_plan tool_call。
    - render_id 可由调用方传入（continue 用同一 id 串接 hydration 与真实进度）。
    - builder：业务消息累积器,流末由调用方 flush 到 messages 表。本函数沿途喂事件。
    - preseed_creative_states：/continue 时由调用方从 DB 既有 assistant 消息的 args 反向推出
      （含 agent_state 等创意期累积状态）,本函数沿用。这是续跑时编委会区块能继续刷新的关键 ——
      没有 preseed,supervisor 子图 worker 切换的事件因 _current_snap() 为空全被跳过,卡片冻在
      最后一帧不动。
    """
    graph = get_agent_graph()
    # recursion_limit：默认 25 不够批量多轮（每条视频 creative 循环 + resource_prep + render ≈ 10-15 superstep，
    # N 条累加远超 25）。提到 500 覆盖大批量；supervisor iteration 守卫(8/条) + batch 队列是自然停止条件，不会死循环。
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 500}
    # 每条流唯一的渲染进度 id（避免同会话多次视频 id 冲突）
    if render_id is None:
        render_id = f"render-{uuid.uuid4().hex[:8]}"

    # ===== 多 Agent 创意期状态快照（per task_id 跨事件累积 args.agent_state）=====
    # 防止 progress 事件覆盖 agent_state，所有 render_video tool_call 的 args 都从这里
    # 取一份完整 snapshot 后 emit。supervisor 子图节点切换 → 增量更新本字典。
    # /continue 走 preseed 把 DB 里的旧 snap 反向恢复 → 续跑事件能接着刷而不是从 None 起步。
    creative_states: dict[str, dict] = dict(preseed_creative_states or {})

    def _ensure_snap(task_id: str, *, topic: str = "", batch_index=None, batch_total=None) -> dict:
        snap = creative_states.get(task_id)
        if snap is None:
            snap = {
                "task_id": task_id,  # 透传给前端,卡片标题下显示「任务ID：xxx」
                "stage": "creative",
                "topic": topic,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "agent_state": {
                    "active_node": None,
                    "node_history": {},
                    "log_message": "🚀 supervisor 派单中...",
                },
                "_researcher_tool_count": 0,  # 内部计数,不 emit 到前端
            }
            creative_states[task_id] = snap
        return snap

    def _current_snap() -> tuple[str, dict] | None:
        """取最近一次 dispatch creative_started 的 task_id（批量串行：同一时刻只有一个 active）。"""
        if not creative_states:
            return None
        tid = next(reversed(creative_states))
        return tid, creative_states[tid]

    def _snap_args(snap: dict) -> dict:
        """从 snap 导出对外 args（剔除内部 _ 字段）。"""
        return {k: v for k, v in snap.items() if not k.startswith("_")}

    def _snap_card_id(task_id: str, snap: dict) -> str:
        return _card_id(
            {"task_id": task_id, "batch_index": snap.get("batch_index"), "batch_total": snap.get("batch_total")},
            render_id,
        )

    async for event in graph.astream_events(input_obj, config, version="v2"):
        kind = event["event"]
        name = event.get("name", "")
        node = (event.get("metadata") or {}).get("langgraph_node", "")

        # A. LLM token 流 —— 仅主 agent 节点（过滤视频内部 LLM）
        if kind == "on_chat_model_stream":
            if node != "agent":
                continue
            chunk = event["data"].get("chunk")
            if chunk and chunk.content:
                if builder is not None:
                    builder.add_delta(chunk.content)
                payload = json.dumps({"delta": chunk.content}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        # B. 工具开始（web_search 等 ToolNode 工具）
        elif kind == "on_tool_start":
            # researcher 阶段的 web_search 不弹独立工具卡 —— 归并到编委会区块的计数 meta
            cur = _current_snap()
            if cur and cur[1]["agent_state"]["active_node"] == "researcher":
                continue
            tool_input = event["data"].get("input", {})
            run_id = event.get("run_id", "")
            if builder is not None:
                builder.add_tool_call(run_id, name, tool_input if isinstance(tool_input, dict) else {})
            payload = json.dumps(
                {"tool_call": {"id": run_id, "name": name, "args": tool_input, "status": "running"}},
                ensure_ascii=False,
            )
            yield f"data: {payload}\n\n"

        # C. 工具结束（output 是 ToolMessage，提取 .content）
        elif kind == "on_tool_end":
            # researcher 阶段的工具结束 → 计数 +1 + re-emit render_video card,不发独立 tool_result
            cur = _current_snap()
            if cur and cur[1]["agent_state"]["active_node"] == "researcher":
                tid, snap = cur
                snap["_researcher_tool_count"] += 1
                snap["agent_state"]["node_history"]["researcher"] = {
                    "status": "active",
                    "meta": f"🔍 已检索 {snap['_researcher_tool_count']} 次",
                }
                cid = _snap_card_id(tid, snap)
                args = _snap_args(snap)
                if builder is not None:
                    builder.add_tool_call(cid, "render_video", args)
                payload = json.dumps(
                    {"tool_call": {"id": cid, "name": "render_video", "args": args, "status": "running"}},
                    ensure_ascii=False, default=str,
                )
                yield f"data: {payload}\n\n"
                continue
            output = event["data"].get("output")
            run_id = event.get("run_id", "")
            tool_result = getattr(output, "content", output)
            if builder is not None:
                builder.add_tool_result(run_id, tool_result)
            payload = json.dumps(
                {"tool_result": {"id": run_id, "name": name, "result": tool_result, "status": "complete"}},
                ensure_ascii=False,
                default=str,
            )
            yield f"data: {payload}\n\n"

        # D. Token 用量（仅主 agent）
        elif kind == "on_chat_model_end":
            if node != "agent":
                continue
            output = event.get("data")
            usage = getattr(output, "usage_metadata", None)
            if usage:
                payload = json.dumps({"usage": dict(usage)}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        # F. 创意期种子（video_start / batch_dispatch creative 入口触发）→ 提前挂卡片
        elif kind == "on_custom_event" and name == "creative_started":
            data = event.get("data", {}) or {}
            tid = data.get("task_id") or render_id
            snap = _ensure_snap(
                tid,
                topic=data.get("topic", ""),
                batch_index=data.get("batch_index"),
                batch_total=data.get("batch_total"),
            )
            cid = _snap_card_id(tid, snap)
            args = _snap_args(snap)
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {"tool_call": {"id": cid, "name": "render_video", "args": args, "status": "running"}},
                ensure_ascii=False, default=str,
            )
            yield f"data: {payload}\n\n"

        # G. supervisor worker 节点开始 → worker 切到 active（后缀匹配 langgraph_node，兼容扁平化裸名/带 ns 前缀）
        elif kind == "on_chain_start":
            worker = _match_worker(node)
            if not worker:
                continue
            cur = _current_snap()
            if not cur:
                continue
            tid, snap = cur
            snap["agent_state"]["active_node"] = worker
            # 强制原地重置：避免 reviewer 打回触发的 editor 重写、整轮 redo 等场景累积脏数据
            snap["agent_state"]["node_history"][worker] = {
                "status": "active",
                "meta": WORKER_INIT_META.get(worker, ""),
            }
            if worker == "researcher":
                snap["_researcher_tool_count"] = 0
            cid = _snap_card_id(tid, snap)
            args = _snap_args(snap)
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {"tool_call": {"id": cid, "name": "render_video", "args": args, "status": "running"}},
                ensure_ascii=False, default=str,
            )
            yield f"data: {payload}\n\n"

        # I. supervisor_route 节点结束 → 取 supervisor_next 写剧组动态弹幕
        # （须在 H 之前判断：supervisor_route 不是 worker，H 的 _match_worker 会 continue 掉）
        elif kind == "on_chain_end" and node == "supervisor_route":
            cur = _current_snap()
            if not cur:
                continue
            tid, snap = cur
            output = (event.get("data") or {}).get("output") or {}
            nxt = output.get("supervisor_next") if isinstance(output, dict) else ""
            if not nxt:
                continue
            log_msg = ROUTE_LOG_TEMPLATES.get(nxt)
            # 打回特例：reviewer.passed=False 后下一次 route → editor
            rev = snap["agent_state"]["node_history"].get("reviewer")
            if nxt == "editor" and rev and rev.get("status") == "done" and "❌" in (rev.get("meta") or ""):
                log_msg = "❌ 质检打回,editor 重写..."
            if not log_msg:
                continue
            snap["agent_state"]["log_message"] = log_msg
            cid = _snap_card_id(tid, snap)
            args = _snap_args(snap)
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {"tool_call": {"id": cid, "name": "render_video", "args": args, "status": "running"}},
                ensure_ascii=False, default=str,
            )
            yield f"data: {payload}\n\n"

        # H. supervisor worker 节点结束 → worker 切到 done + 提取战果 meta
        elif kind == "on_chain_end":
            worker = _match_worker(node)
            if not worker:
                continue
            cur = _current_snap()
            if not cur:
                continue
            tid, snap = cur
            output = (event.get("data") or {}).get("output")
            meta = _extract_worker_meta(worker, output, snap)
            snap["agent_state"]["node_history"][worker] = {"status": "done", "meta": meta}
            if snap["agent_state"]["active_node"] == worker:
                snap["agent_state"]["active_node"] = None
            cid = _snap_card_id(tid, snap)
            args = _snap_args(snap)
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {"tool_call": {"id": cid, "name": "render_video", "args": args, "status": "running"}},
                ensure_ascii=False, default=str,
            )
            yield f"data: {payload}\n\n"

        # E. 视频制作进度 → render_video tool_call（原地推进同 id 卡片）
        # 与 creative_states 合并：保留 agent_state 区块,转入 tts/materials/... 时编委会切灰化
        elif kind == "on_custom_event" and name == "progress":
            data = event.get("data", {}) or {}
            tid = data.get("task_id") or render_id
            snap = creative_states.get(tid)
            if snap is not None:
                # 累积合并：stage 切换、batch 元信息、label 透传
                if "stage" in data:
                    snap["stage"] = data["stage"]
                if "topic" in data:
                    snap["topic"] = data["topic"]
                if "label" in data:
                    snap["label"] = data["label"]
                if "batch_index" in data:
                    snap["batch_index"] = data["batch_index"]
                if "batch_total" in data:
                    snap["batch_total"] = data["batch_total"]
                # 进入工业渲染线 → 编委会进入「完成态灰化」
                if data.get("stage") in {"tts", "asr", "materials", "combine", "generate"}:
                    if snap["agent_state"]["active_node"] is not None:
                        snap["agent_state"]["active_node"] = None
                    snap["agent_state"]["log_message"] = "✅ 编委会已交付,渲染流水线启动..."
                args = _snap_args(snap)
                cid = _snap_card_id(tid, snap)
            else:
                # fallback：补做轮跳过 creative 直接 progress（无编委会上下文）→ 用原始 data
                args = data
                cid = _card_id(data, render_id)
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {"tool_call": {"id": cid, "name": "render_video", "args": args, "status": "running"}},
                ensure_ascii=False,
                default=str,
            )
            yield f"data: {payload}\n\n"

        # E'. 渲染完成 → 该轮 tool_result（翻播放器；url 用 task_id，id 用卡片 id）
        elif kind == "on_custom_event" and name == "render_done":
            data = event.get("data", {}) or {}
            tid = data.get("task_id") or render_id
            cid = _card_id(data, render_id)
            result_obj = {
                "url": f"/api/files/{thread_id}/{tid}/final-1.mp4",
                "filename": f"{tid}.mp4",
            }
            if builder is not None:
                builder.add_tool_result(cid, result_obj)
            payload = json.dumps(
                {
                    "tool_result": {
                        "id": cid,
                        "name": "render_video",
                        "result": result_obj,
                        "status": "complete",
                    }
                },
                ensure_ascii=False,
                default=str,
            )
            yield f"data: {payload}\n\n"

        # E'''. 渲染失败 → 该轮 render_video 卡发 stage=failed（翻红）。
        # retry 未耗尽 → render_node 已紧跟发 self_heal(progress, stage=tts)把卡从红翻回绿
        # ("无损自愈秀",同 task_id 同卡原地推进,不新建任务/卡);retry 耗尽 → 卡停红,render_node
        # 回战败 ToolMessage 唤醒 agent 报错。
        elif kind == "on_custom_event" and name == "render_failed":
            data = event.get("data", {}) or {}
            tid = data.get("task_id") or render_id
            cid = _card_id(data, render_id)
            # 累积合并现有 snap 的 agent_state(若在),仅覆盖 stage=failed
            snap = creative_states.get(tid)
            if snap is not None:
                snap["stage"] = "failed"
                snap["label"] = "❌ 制作失败,正在自愈重试..."
                args = _snap_args(snap)
            else:
                args = {
                    "stage": "failed",
                    "label": "❌ 制作失败,正在自愈重试...",
                    "task_id": tid,
                    "topic": data.get("topic", ""),
                    "batch_index": data.get("batch_index"),
                    "batch_total": data.get("batch_total"),
                }
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {
                    "tool_call": {
                        "id": cid,
                        "name": "render_video",
                        "args": args,
                        "status": "running",
                    }
                },
                ensure_ascii=False,
                default=str,
            )
            yield f"data: {payload}\n\n"

        # D'. 创意期熔断（creative_failed）→ 分镜不合格且 director 重试超限,创意期就熔断,
        #     未进 TTS/ASR/素材/渲染车间。复用 stage=failed 渲染（失败卡 + 重试按钮）,
        #     label 区分"创意期熔断"避免与 render_failed 的"自愈重试"文案混淆。
        elif kind == "on_custom_event" and name == "creative_failed":
            data = event.get("data", {}) or {}
            tid = data.get("task_id") or render_id
            cid = _card_id(data, render_id)
            reason = data.get("reason", "") or "分镜不合格"
            snap = creative_states.get(tid)
            if snap is not None:
                snap["stage"] = "failed"
                snap["label"] = f"❌ 创意期熔断：{reason}"
                args = _snap_args(snap)
            else:
                args = {
                    "stage": "failed",
                    "label": f"❌ 创意期熔断：{reason}",
                    "task_id": tid,
                    "topic": data.get("topic", ""),
                    "batch_index": data.get("batch_index"),
                    "batch_total": data.get("batch_total"),
                }
            if builder is not None:
                builder.add_tool_call(cid, "render_video", args)
            payload = json.dumps(
                {
                    "tool_call": {
                        "id": cid,
                        "name": "render_video",
                        "args": args,
                        "status": "running",
                    }
                },
                ensure_ascii=False,
                default=str,
            )
            yield f"data: {payload}\n\n"

        # E''. 批量占位（batch_queued）→ 预发 N 个排队卡（render-batch-{i}，stage=queued），
        #       前端一开始就看到所有任务（1/2 排队中、2/2 排队中…），串行推进时原地更新
        elif kind == "on_custom_event" and name == "batch_queued":
            data = event.get("data", {}) or {}
            total = data.get("total", 0)
            for item in data.get("items", []) or []:
                idx = item.get("index")
                cid = f"render-batch-{idx}"
                args = {
                    "stage": "queued",
                    "label": "⏳ 排队中",
                    "batch_index": idx,
                    "batch_total": total,
                    "topic": item.get("topic", ""),
                }
                if builder is not None:
                    builder.add_tool_call(cid, "render_video", args)
                payload = json.dumps(
                    {
                        "tool_call": {
                            "id": cid,
                            "name": "render_video",
                            "args": args,
                            "status": "running",
                        }
                    },
                    ensure_ascii=False,
                    default=str,
                )
                yield f"data: {payload}\n\n"

    # E. 流结束 —— 探测 HITL 中断 / 渲染完成，合成对应 tool_call
    state = await graph.aget_state(config)
    values = state.values or {}

    # E1. HITL 中断 → 合成 review_video_plan（前端渲染评审卡）
    review_value = None
    for task in state.tasks:
        for intr in task.interrupts:
            review_value = intr.value
            break
        if review_value is not None:
            break
    if isinstance(review_value, dict) and review_value:
        cid = f"review-{thread_id}"
        if builder is not None:
            builder.add_tool_call(cid, "review_video_plan", review_value)
        payload = json.dumps(
            {
                "tool_call": {
                    "id": cid,
                    "name": "review_video_plan",
                    "args": review_value,
                    "status": "running",
                }
            },
            ensure_ascii=False,
            default=str,
        )
        yield f"data: {payload}\n\n"

    # E2 已由 render_done 事件（每轮 render 完成时即时发 tool_result）取代，无需流末统一发

    yield "data: {}\n\n"


def _is_idle_for_pruning(state) -> bool:
    """流末 thread 是否处于"完全空闲"态,可安全裁剪 checkpoint。
    - state.next 为空：没有 pending 节点（不是 HITL 中断,不是续跑前停留）
    """
    if state is None:
        return False
    return not getattr(state, "next", None)


@router.post("/send")
async def send_message(request: Request) -> StreamingResponse:
    """发送消息并获取 SSE 流式响应。"""
    body = await request.json()
    message = body.get("message", "")
    thread_id = body.get("thread_id", "")
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")

    logger.info("[chat/send] thread_id=%s", thread_id)
    # 用户在 UI 选择的运行模式：auto_review=False（⚡全自动）→ 跳过审稿。
    # 随 HumanMessage.additional_kwargs 持久化，agent_node 提取后写入 state.user_auto_review。
    auto_review = body.get("auto_review", True)
    user_msg = HumanMessage(content=message, additional_kwargs={"auto_review": bool(auto_review)})
    input_obj = {"messages": [user_msg]}

    # 业务消息表写入：流首写 user 消息（即使流崩了也保留用户输入）
    try:
        await append_user_message(thread_id, message)
    except Exception as e:
        logger.exception("[chat/send] append_user_message failed: %s", e)

    builder = AssistantMessageBuilder()

    async def _gen():
        try:
            async for chunk in _stream_response(input_obj, thread_id, builder=builder):
                yield chunk
        finally:
            await _flush_and_prune(thread_id, builder)

    return StreamingResponse(_gen(), media_type="text/event-stream")


async def _flush_and_prune(
    thread_id: str,
    builder: AssistantMessageBuilder,
    replace_message_id: str | None = None,
) -> None:
    """流末统一收尾：把累积 parts 写入业务消息表 + 裁剪 checkpoint（仅在 thread idle 时）。

    replace_message_id：/continue 传入既有 assistant 行 id → REPLACE 同行（不再 INSERT
    新行）,避免续跑后 DB 多出一条 assistant 消息（界面上"DB 旧卡 + 续跑新卡"两条 bubble 的根因）。
    """
    # 1. 写入 / 替换 assistant 业务消息（空消息会被忽略）
    if not builder.is_empty():
        try:
            parts = builder.build()
            if replace_message_id:
                ok = await replace_assistant_message(replace_message_id, parts)
                if not ok:
                    # 既有行不知何故消失（人工删/migrate）→ 兜底 INSERT,不让续跑数据丢
                    logger.warning(
                        "[chat] replace_assistant_message id=%s 失败,兜底 INSERT 新行",
                        replace_message_id,
                    )
                    await append_assistant_message(thread_id, parts)
            else:
                await append_assistant_message(thread_id, parts)
        except Exception as e:
            logger.exception("[chat] flush assistant 消息失败: %s", e)
    # 2. 流末延迟裁剪（仅在状态机已 idle —— 不裁掉 HITL/续跑前的关键 checkpoint）
    try:
        graph = get_agent_graph()
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if _is_idle_for_pruning(state):
            await prune_checkpoints(thread_id, keep=3)
    except Exception as e:
        logger.exception("[chat] prune_checkpoints failed: %s", e)


@router.post("/resume")
async def resume_conversation(request: Request) -> StreamingResponse:
    """HITL 恢复：用户审稿确认后，带入改稿恢复视频流水线。

    请求体：{ thread_id, script_text, storyboard }
    thread_id 必须与挂起时完全一致（LangGraph 靠它唤醒冻结线程）。
    """
    body = await request.json()
    thread_id = body.get("thread_id", "")
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")
    if thread_id in _active_video_threads:
        raise HTTPException(status_code=409, detail="already rendering")

    resume_value = {
        "script_text": body.get("script_text", ""),
        "storyboard": body.get("storyboard", []),
    }
    logger.info("[chat/resume] thread_id=%s", thread_id)
    input_obj = Command(resume=resume_value)

    # resume 把上一条 review 卡的 result 写实（审稿完成态）
    try:
        await update_last_assistant_tool_result(
            thread_id, f"review-{thread_id}", resume_value
        )
    except Exception as e:
        logger.exception("[chat/resume] update review card failed: %s", e)

    builder = AssistantMessageBuilder()

    async def _gen():
        _active_video_threads.add(thread_id)
        try:
            async for chunk in _stream_response(input_obj, thread_id, builder=builder):
                yield chunk
        finally:
            _active_video_threads.discard(thread_id)
            await _flush_and_prune(thread_id, builder)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/redo-failed")
async def redo_failed_videos(request: Request) -> StreamingResponse:
    """补做失败轮：批量完成后，复用失败视频的原 task_id 重新走 resource_prep→render，
    progress.json 自动跳过已完成的 audio/materials/combine，只重做失败的。

    请求体：{ thread_id }。用 Command(goto="batch_redo_start") 直接跳转补做入口，
    绕过 agent 不烧 token。复用 _active_video_threads 并发守卫（同 thread 渲染中 → 409）。
    """
    body = await request.json()
    thread_id = body.get("thread_id", "")
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")
    if thread_id in _active_video_threads:
        raise HTTPException(status_code=409, detail="already rendering")

    logger.info("[chat/redo-failed] thread_id=%s (补做失败轮)", thread_id)
    input_obj = Command(goto="batch_redo_start")
    builder = AssistantMessageBuilder()

    async def _gen():
        _active_video_threads.add(thread_id)
        try:
            async for chunk in _stream_response(input_obj, thread_id, builder=builder):
                yield chunk
        finally:
            _active_video_threads.discard(thread_id)
            await _flush_and_prune(thread_id, builder)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/continue")
async def continue_conversation(request: Request) -> StreamingResponse:
    """断点续传：后端重启后，从 checkpoint 续跑未完成的视频渲染。

    请求体：{ thread_id }。astream_events(None) 从最后 checkpoint 继续（重跑 render，
    复用已缓存的 audio/素材）。

    去重核心（修正 https://chat 出现"两张卡片"的 bug）：
    - 之前每次 /continue 都用 fresh builder + 流末 INSERT,导致 DB 多出一条 assistant 行,
      reload 后界面上出现 [DB 旧创意卡] + [续跑新管线卡] 两条 bubble。
    - 现在：先取 DB 末条 assistant → seed 进 builder（续跑事件 update 在已有 parts 之上）
      → 流末 REPLACE 同一行（不 INSERT）。同时把 render_video tool-call 的 args.agent_state
      反向推回 creative_states dict,让续跑期间 supervisor worker 切换的事件能继续刷编委会区块。
    """
    body = await request.json()
    thread_id = body.get("thread_id", "")
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")
    if thread_id in _active_video_threads:
        raise HTTPException(status_code=409, detail="already rendering")

    # 硬性闸门:复用 history._compute_can_continue 的同一判定,根治"已成功任务被续传重跑"。
    # 即使前端 can_continue=False 没自动触发,任何直接 POST /continue 也在此物理拦截:
    # final_video_path 非空(成片已落盘)或 next 只剩 agent(只剩文本总结)→ 409 拒绝续传。
    graph = get_agent_graph()
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    from api_view.api.history import _compute_can_continue
    if not _compute_can_continue(snapshot):
        logger.info("[chat/continue] thread=%s 不满足续传条件(已成功/只剩总结),拒绝", thread_id)
        raise HTTPException(
            status_code=409,
            detail="该会话无需续传（视频已完成或仅剩文本总结），请发送新消息继续对话",
        )
    logger.info("[chat/continue] thread_id=%s (断点续传,已通过闸门)", thread_id)
    render_id = f"render-{uuid.uuid4().hex[:8]}"
    builder = AssistantMessageBuilder()

    # ① 取 DB 末条 assistant 消息 → seed builder（续跑后 REPLACE 同行,不再多一条）
    last_assistant = await get_last_assistant(thread_id)
    replace_id: str | None = None
    preseed: dict[str, dict] = {}
    if last_assistant and last_assistant.get("parts"):
        builder.seed(last_assistant["parts"])
        replace_id = last_assistant.get("id")
        # ② 反向把 render_video tool-call 的 args 推回 creative_states,让续跑事件能接着刷
        for p in last_assistant["parts"]:
            if p.get("type") != "tool-call" or p.get("toolName") != "render_video":
                continue
            args = p.get("args") or {}
            tid = (args.get("task_id") or "").strip()
            # 单视频卡 toolCallId 形如 "render-{tid}"; tid 为空时回退从 toolCallId 解析
            if not tid:
                tcid = p.get("toolCallId") or ""
                if tcid.startswith("render-") and not tcid.startswith("render-batch-"):
                    tid = tcid[len("render-"):]
            if not tid:
                continue
            ag = args.get("agent_state") or {
                "active_node": None,
                "node_history": {},
                "log_message": "🚀 supervisor 派单中...",
            }
            preseed[tid] = {
                "stage": args.get("stage", "creative"),
                "topic": args.get("topic", ""),
                "batch_index": args.get("batch_index"),
                "batch_total": args.get("batch_total"),
                "agent_state": {
                    "active_node": ag.get("active_node"),
                    "node_history": dict(ag.get("node_history") or {}),
                    "log_message": ag.get("log_message", ""),
                },
                "_researcher_tool_count": 0,  # 续跑时计数从零开始（researcher 子图也会重跑）
            }
        logger.info(
            "[chat/continue] seeded builder from DB msg id=%s parts=%d preseed_tids=%s",
            replace_id, len(last_assistant["parts"]), list(preseed.keys()),
        )

    # ②' DB 没 assistant（中途崩溃 builder 未 flush）→ 从 checkpoint 反推 preseed,
    #     防 progress 事件走 fallback path 时 `args = data` 把 history 重建的 agent_state 冲掉。
    if not preseed:
        try:
            graph = get_agent_graph()
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            vals = st.values or {}
            cur_tid = vals.get("video_task_id", "") or ""
            if cur_tid:
                # 复用 history.py 的同款 helper 反推 4 worker done 状态
                from api_view.api.history import _rebuild_done_agent_state
                preseed[cur_tid] = {
                    "stage": "creative",  # 兜底,真实事件按 toolCallId 推进 stage
                    "topic": vals.get("video_topic", ""),
                    "batch_index": vals.get("batch_index"),
                    "batch_total": vals.get("batch_total"),
                    "agent_state": _rebuild_done_agent_state(vals),
                    "_researcher_tool_count": 0,
                }
                logger.info(
                    "[chat/continue] DB 无 assistant,从 checkpoint preseed cur_tid=%s "
                    "batch=%s/%s",
                    cur_tid, vals.get("batch_index"), vals.get("batch_total"),
                )
        except Exception as e:
            logger.warning("[chat/continue] checkpoint preseed 失败: %s", e)

    async def _gen():
        _active_video_threads.add(thread_id)
        try:
            # ③ Hydration：复用 DB 既有 render_video 卡的 args 重新 emit 到 SSE。
            #    流期间前端的"续跑 bubble"和"DB bubble"会短暂同时显示,但 args 完全一致 → 视觉一致。
            #    流末 REPLACE 同行后,reload 只剩 1 条 assistant message → 永久去重。
            #    不再写死 stage="combine"——续跑可能停在 creative/resource_prep/render 的任一段,
            #    用真实 args 比硬编码更准确。
            if last_assistant and last_assistant.get("parts"):
                for p in last_assistant["parts"]:
                    if p.get("type") != "tool-call":
                        continue
                    payload = json.dumps(
                        {
                            "tool_call": {
                                "id": p.get("toolCallId", ""),
                                "name": p.get("toolName", ""),
                                "args": p.get("args") or {},
                                "status": "running",
                            }
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    yield f"data: {payload}\n\n"
            else:
                # DB 没有未完成 assistant（罕见：seed 失败 / 老会话）→ 退回硬编码 hydration
                graph = get_agent_graph()
                st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
                vals = st.values or {}
                tid = vals.get("video_task_id", "") or render_id
                bidx, btotal = vals.get("batch_index"), vals.get("batch_total")
                card_id = f"render-batch-{bidx}" if (bidx and btotal) else f"render-{tid}"
                hydration_args = {
                    "stage": "combine",
                    "label": "🎬 拼接素材片段",
                    "task_id": tid,
                    "batch_index": bidx,
                    "batch_total": btotal,
                    "topic": vals.get("video_topic", ""),
                }
                builder.add_tool_call(card_id, "render_video", hydration_args)
                yield "data: " + json.dumps({
                    "tool_call": {
                        "id": card_id,
                        "name": "render_video",
                        "args": hydration_args,
                        "status": "running",
                    }
                }, ensure_ascii=False) + "\n\n"
            async for chunk in _stream_response(
                None, thread_id, render_id,
                builder=builder, preseed_creative_states=preseed,
            ):
                yield chunk
        finally:
            _active_video_threads.discard(thread_id)
            await _flush_and_prune(thread_id, builder, replace_message_id=replace_id)

    return StreamingResponse(_gen(), media_type="text/event-stream")
