from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.graph.message import add_messages


# 状态机 messages 窗口（per-superstep checkpoint blob 的主要瘦身手段）。
# 业务消息真相源在 messages 表（reload 用），状态机里只保留最近 K 条供 LLM 构造 prompt。
# 单视频/批量的 plan_video[_batch] 反查一定在最近几条之内（K=10 远超）；
# batch_summary 改用 state.batch_tool_call_id 字段,不依赖反查。
KEEP_LAST_K_MESSAGES = 10


def keep_last_k_messages(left, right):
    """messages reducer：先 add_messages 合并，再保留尾部最多 K 条；
    安全切点：丢弃头部 ToolMessage（其前置 AIMessage 已被截掉,会变孤儿）。

    剩余 messages 内的 AIMessage(tool_calls) 与 ToolMessage 配对：
    - 若 AIMessage 在窗口内但 ToolMessage 已被丢 → _sanitize_messages 会补占位
      （build.py 已有该保护，DeepSeek 不会因孤儿 tool_call 报 400）
    """
    merged = add_messages(left, right)
    if len(merged) <= KEEP_LAST_K_MESSAGES:
        return merged
    tail = list(merged[-KEEP_LAST_K_MESSAGES:])
    while tail and isinstance(tail[0], ToolMessage):
        tail.pop(0)
    return tail


class AgentState(TypedDict):
    """主图状态：消息 + 内联视频流程字段。

    视频节点（chief_editor/director/...）内联在主图中（非独立子图），
    这样 LangGraph interrupt() 能直接挂起唯一的 checkpointed 主图。
    非视频对话时视频字段缺省，节点用 state.get(...) 读取。
    """

    # messages 用 keep_last_k 截断到最近 K 条，防 checkpoint blob O(N²) 膨胀。
    # 业务消息（reload UI 用）已落到 messages 表，状态机不需要保留全量历史。
    messages: Annotated[Sequence[BaseMessage], keep_last_k_messages]

    # 视频流程字段
    video_task_id: str  # bridge 隔离任务目录的 key（video_start 生成）
    video_session_id: str  # 会话 thread_id（= sessions 表主键）；bridge 拼任务目录的 session 段 video_tasks/<session_id>/<task_id>/
    video_topic: str
    research_notes: str  # researcher 产出的资料（Supervisor 创意阶段；video_start 预置主题种子；达搜索上限时由整理模式产出，不丢失）
    script_text: str
    script_review: dict  # reviewer 质检结论 {passed, feedback}（Supervisor 创意阶段）
    storyboard: list
    audio_paths: list
    video_clips: list
    timeline_config: dict  # 承载 subtitle_path 等中间产物
    final_video_path: str

    # 创意中枢扁平化路由（原 Supervisor 子图的 next/iteration，上摆为主图字段）。
    # supervisor_route_node 每次写入；supervisor_route_after 条件边读取决定下一步派哪个 worker / FINISH。
    supervisor_next: str  # route 决策：researcher/editor/reviewer/director/FINISH
    supervisor_iteration: int  # 微观调度循环计数（硬守卫 MAX_ITERATIONS=8）

    # 批量视频（Phase 1 串行队列，plan_video_batch 触发）。单视频路径不写这些字段。
    batch_queue: list  # 待处理主题队列（batch_start 初始化，batch_dispatch 逐个 pop）
    batch_index: int  # 已派发轮次（0=未开始；>0 表示已完成相应轮）
    batch_total: int  # 批量总数（batch_start 设 = len(topics)；单视频为 None）
    batch_results: list  # 累积每轮结果 [{topic, task_id, final_video_path}, ...]
    batch_auto_review: bool  # True=逐条人工审稿（默认）；False=静默排队全自动
    batch_route: str  # batch_dispatch 的临时路由标志："supervisor_route"（派发下一轮）/ "summary"（队列空）
    batch_retry_count: int  # 当前轮失败重试计数（成功/进下一轮时重置；上限 MAX_RETRY）
    # 单视频失败局部重试计数（仿 batch_retry_count）：TTS/render 偶发失败时复用同 task_id
    # 回 resource_prep 重做（progress.json 跳过已完成子步、只重试失败的），同张卡原地推进,
    # 不新建任务、不新建卡（恢复"一任务一卡"）。成功/彻底失败回 agent 时重置。
    video_retry_count: int
    # render_node 的临时路由标志（仿 batch_route）：单视频 render 后由 render_node 自己决定下一步——
    # "resource_prep"（重试,复用同 task_id）/ "agent"（成功回总结 或 彻底失败回战败 ToolMessage）。
    # 必要：原 after_render 用 video_retry_count 计数器推断路由,与 render_node 的重试/彻底失败分支
    # 错位一格(2<MAX_RETRY 走重试分支但 after_render 见 vrc>=MAX_RETRY 回 agent),导致单视频第 2 次
    # 失败时不带战败 ToolMessage 唤醒 agent → 未应答的 plan_video tool_call → agent 盲目"二胎"下发
    # 新 plan_video → 幽灵卡 + 双卡。显式 route 字段消除计数器推断的歧义。
    video_route: str
    # plan_video_batch 的 tool_call_id（batch_start 写入,batch_summary 用之回 ToolMessage）。
    # 必要：messages 被 keep_last_k 截断后,batch_summary 反查 messages 可能找不到原 tool_call。
    batch_tool_call_id: str
    # 用户在 UI 选择的运行模式（每条用户消息携带，agent_node 写入 state）：
    # True=专家审核（默认）/ False=⚡全自动（单视频 & 批量都跳过 request_review）
    user_auto_review: bool
    # 用户对话指定的字幕字号（plan_video/plan_video_batch 的 subtitle_size）。
    # 0/None=未指定，bridge.render 回退 config.toml 的 video_font_size。
    user_font_size: int
