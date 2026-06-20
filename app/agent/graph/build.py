from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from agent.config import settings
from agent.graph.agent_state import AgentState
from agent.nodes.video_nodes import (
    after_batch_dispatch,
    after_redo_start,
    after_render,
    batch_dispatch_node,
    batch_redo_start_node,
    batch_start_node,
    batch_summary_node,
    render_node,
    request_review_node,
    resource_prep_node,
    video_start_node,
)
from agent.subagents.factory import create_subagent_graph
from agent.supervisor.builder import WORKERS, supervisor_route_after, supervisor_route_node
from agent.tools.plan_video import plan_video
from agent.tools.plan_video_batch import plan_video_batch
from agent.tools.redo_failed_videos import redo_failed_videos
from agent.tools.web_search import web_search

logger = logging.getLogger(__name__)

# 北京时间（UTC+8，无夏令时），无需 tzdata / pytz 依赖
CST = timezone(timedelta(hours=8))

SYSTEM_PROMPT = """\
你是 MoneyPrinterClaw，一款本地桌面 AI 生产力助手。
你的使命是帮助用户高效完成复杂工作流，包括联网搜索、信息整理、文件处理、短视频创作等长链条任务。

行为准则：
1. 当用户询问时事、天气、股价、赛事结果或任何可能超出训练截止日期的信息时，必须主动调用 web_search 工具获取最新信息。
2. 当用户要求"做一个 / 生成 / 拍一个关于某主题的短视频、解说视频"时，调用 plan_video 工具启动视频创作流水线（文案 → 分镜 → 人类确认 → 配音/素材 → 渲染）。
3. 调用工具时保持简洁，直接给出执行动作，不要冗长解释。
4. 对于多步骤任务，先拆解步骤，逐步执行，必要时向用户确认关键参数。
5. 回答要结构化、可操作，优先输出结论而非长篇背景。
6. 当用户要求"一次做多个 / 批量做 N 条"视频时，调用 plan_video_batch 工具，并务必把大主题拆成**不同角度的差异化主题**（如"咖啡"→ 历史/手冲教程/健康误区/文化…），严禁雷同。
7. **严禁在同一轮回复中并行调用多个 plan_video**（会触发工具协议错误，导致流水线中断）。需要制作多个视频时，必须用 `plan_video_batch` 一次调用；单次回复最多只调用一个 `plan_video`。
8. 批量视频完成后，若汇总显示有失败的视频（网络抖动、渲染偶发崩溃等），应主动询问用户"是否补做失败的"。用户同意后调用 `redo_failed_videos`（无需参数，系统自动复用原任务、只重做失败步骤）。用户也可在前端失败卡片上点"重试"。
9. 用户提到字幕大小（"小一点/大一点/字号N/字幕调小"）时，调用 plan_video/plan_video_batch 传 `subtitle_size` 参数：模糊词按 小=14、中=20、大=28、超大=36 取值，用户给具体数字就直接用。用户没提字幕大小就不传该参数。\
"""


def _sanitize_messages(messages: list) -> list:
    """补齐孤儿 tool_call，避免 DeepSeek 400。

    两类孤儿都要处理（DeepSeek 严格校验 tool↔tool_calls 配对）：
    1. 正向孤儿：AIMessage 带 tool_calls 但无对应 ToolMessage 回包
       （模型一轮并行发多个 plan_video,ToolNode 只回部分）→ 紧随其后补中性 ToolMessage。
    2. 反向孤儿：ToolMessage 的 tool_call_id 在窗口内找不到对应 AIMessage
       （keep_last_k=10 截断把发出 tool_call 的 AIMessage 切掉了,留下光秃秃的 ToolMessage）→
       丢弃该 ToolMessage。DeepSeek 报 "Messages with role 'tool' must be a response to a
       preceding message with 'tool_calls'" 正是这类。视频成功/彻底失败回 ToolMessage 后
       经 long tail 流程,plan_video 的 AIMessage 易被挤出 K=10 窗口 → 收尾回 agent 触发。
       丢弃安全：窗口内已无对应 tool_call,agent 本就看不到该调用上下文,ToolMessage 失去锚点。
    """
    # 收集窗口内所有 AIMessage 发出的 tool_call_id
    valid_tool_call_ids: set[str] = set()
    for m in messages:
        tcs = getattr(m, "tool_calls", None) or []
        for tc in tcs:
            tcid = tc.get("id")
            if tcid:
                valid_tool_call_ids.add(tcid)

    # 反向孤儿处理：丢掉无对应 AIMessage 的 ToolMessage
    sanitized: list = []
    for m in messages:
        tcid = getattr(m, "tool_call_id", None)
        if tcid and isinstance(m, ToolMessage) and tcid not in valid_tool_call_ids:
            logger.debug("[agent/_sanitize] 丢弃反向孤儿 ToolMessage tcid=%s", tcid)
            continue
        sanitized.append(m)

    # 正向孤儿处理：给带 tool_calls 但无回包的 AIMessage 补中性 ToolMessage
    answered = {getattr(m, "tool_call_id", None) for m in sanitized}
    answered.discard(None)
    result: list = []
    for m in sanitized:
        result.append(m)
        tool_calls = getattr(m, "tool_calls", None) or []
        if not tool_calls:
            continue
        for tc in tool_calls:
            tcid = tc.get("id")
            if tcid and tcid not in answered:
                result.append(ToolMessage(
                    content="(该工具调用未被执行：请勿在同一轮并行调用多个工具，多个视频请改用 plan_video_batch 单次调用)",
                    tool_call_id=tcid,
                ))
                answered.add(tcid)  # 同一条 AIMessage 内防重复补
    return result


async def build_agent(checkpointer):
    """编译带 Checkpointer 的 LangGraph Agent（主图内联视频流程 + HITL 断点）"""

    tools = [web_search, plan_video, plan_video_batch, redo_failed_videos]

    # 创意中枢扁平化：4 个 worker 子图平铺为主图节点 + supervisor_route 裸节点。
    # tools_map 透传给 worker 工厂（researcher 需要 web_search）。
    tools_map = {"web_search": web_search}

    async def agent_node(state: AgentState):
        # 每次调用时按需构造 ChatOpenAI —— 读 settings 最新值,使 /api/settings 保存后
        # 下一次对话即用新 key / model / temperature,无需重启后端(ChatOpenAI 构造轻量无网络)。
        model = ChatOpenAI(
            model=settings.model_name,
            temperature=settings.model_temperature,
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
            max_tokens=settings.model_max_tokens,
        ).bind_tools(tools)
        # 动态注入当前北京时间，作为模型处理时效性任务的唯一时间锚点。
        current_time = datetime.now(CST).strftime("%Y年%m月%d日 %H:%M:%S %A")
        dynamic_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"【系统时间】现在是北京时间：{current_time}。\n"
            "处理天气、赛事、股市、新闻等一切时效性任务时，"
            "必须严格以此时间为「今天」的唯一基准，绝不假设或使用过期日期。"
        )
        # ---- 日志：本轮输入 ----
        last_user = ""
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                c = m.content
                last_user = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                break
        logger.info("========== [agent] 开始思考 ==========")
        logger.info("[agent] 用户: %s", (last_user or "(无用户消息，可能是工具结果回填)")[:300])

        messages = [SystemMessage(content=dynamic_prompt)] + list(state["messages"])
        messages = _sanitize_messages(messages)
        response = await model.ainvoke(messages)

        # ---- 日志：思考过程（DeepSeek thinking 的 reasoning_content）----
        ak = getattr(response, "additional_kwargs", None) or {}
        reasoning = ak.get("reasoning_content") or getattr(response, "reasoning_content", "") or ""
        if reasoning:
            logger.info("[agent] 思考: %s", str(reasoning)[:800])

        # ---- 日志：决策（工具调用 or 文本回复）----
        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                args_str = json.dumps(tc.get("args", {}), ensure_ascii=False)
                logger.info("[agent] 调用工具 %s | 参数: %s", tc.get("name"), args_str[:300])
        else:
            text = response.content if isinstance(response.content, str) else json.dumps(response.content, ensure_ascii=False)
            logger.info("[agent] 回复: %s", text[:500])

        usage = getattr(response, "usage_metadata", None)
        if usage:
            logger.info("[agent] tokens 输入=%s 输出=%s", usage.get("input_tokens"), usage.get("output_tokens"))

        # 用户在 UI 选择的运行模式（随最新 HumanMessage.additional_kwargs 携带）→ 写入 state。
        # user_auto_review=False（⚡全自动）时，after_creative 跳过 request_review（单视频 & 批量都跳）。
        update = {"messages": [response]}
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                ar = (getattr(m, "additional_kwargs", None) or {}).get("auto_review")
                if ar is not None:
                    update["user_auto_review"] = bool(ar)
                break
        return update

    def should_continue(state: AgentState):
        """路由：plan_video_batch → 批量队列；redo_failed_videos → 补做失败轮；plan_video → 单视频链；其它工具 → ToolNode；无工具 → 结束"""
        last_message = state["messages"][-1]
        tool_calls = getattr(last_message, "tool_calls", None)
        if not tool_calls:
            logger.info("[route] agent -> END（回答完成）")
            return END
        names = [tc.get("name") for tc in tool_calls]
        if "plan_video_batch" in names:
            logger.info("[route] agent -> batch_start（批量视频队列）")
            return "batch_start"
        if "redo_failed_videos" in names:
            logger.info("[route] agent -> batch_redo_start（补做失败轮）")
            return "batch_redo_start"
        if "plan_video" in names:
            logger.info("[route] agent -> video_start（单视频流水线）")
            return "video_start"
        logger.info("[route] agent -> tools（执行 %s）", names)
        return "tools"

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", ToolNode([web_search]))
    # 单视频入口
    workflow.add_node("video_start", video_start_node)
    # 批量入口 + 循环控制器 + 收尾 + 补做入口（Phase 1 串行队列）
    workflow.add_node("batch_start", batch_start_node)
    workflow.add_node("batch_dispatch", batch_dispatch_node)
    workflow.add_node("batch_summary", batch_summary_node)
    workflow.add_node("batch_redo_start", batch_redo_start_node)
    # 创意中枢扁平化：supervisor_route 裸节点 + 4 个 worker 子图平铺为主图节点（单视频 & 批量每轮共用）
    workflow.add_node("supervisor_route", supervisor_route_node)
    for w in WORKERS:
        workflow.add_node(w, create_subagent_graph(w, tools_map=tools_map))
    workflow.add_node("request_review", request_review_node)
    workflow.add_node("resource_prep", resource_prep_node)
    workflow.add_node("render", render_node)

    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", should_continue)
    workflow.add_edge("tools", "agent")
    # 单视频：video_start → supervisor_route
    workflow.add_edge("video_start", "supervisor_route")
    # 批量：batch_start → batch_dispatch →(派发) supervisor_route / (失败重试) resource_prep / (队列空) batch_summary → agent
    workflow.add_edge("batch_start", "batch_dispatch")
    workflow.add_conditional_edges("batch_dispatch", after_batch_dispatch, ["supervisor_route", "resource_prep", "batch_summary"])
    # 每个 worker 干完回 supervisor_route（微观调度循环）
    for w in WORKERS:
        workflow.add_edge(w, "supervisor_route")
    # supervisor_route →(命中 worker) 该 worker / (FINISH/超上限) after_creative 判定 → request_review（逐个审）/ resource_prep（跳审稿）
    workflow.add_conditional_edges("supervisor_route", supervisor_route_after, [*WORKERS, "request_review", "resource_prep"])
    workflow.add_edge("request_review", "resource_prep")
    workflow.add_edge("resource_prep", "render")
    # render → batch_dispatch（批量循环）/ resource_prep（单视频失败局部重试,复用同 task_id 同卡）/ agent（单视频成功或彻底失败回总结）
    workflow.add_conditional_edges("render", after_render, ["batch_dispatch", "resource_prep", "agent"])
    workflow.add_edge("batch_summary", "agent")
    # 补做失败轮：batch_redo_start →(有失败轮) batch_dispatch / (无失败轮) agent
    workflow.add_conditional_edges("batch_redo_start", after_redo_start, ["batch_dispatch", "agent"])

    return workflow.compile(checkpointer=checkpointer)
