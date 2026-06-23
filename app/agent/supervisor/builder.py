"""
创意中枢（扁平化）：Supervisor 路由节点 + 辅助。

历史：原为 ``create_supervisor_graph`` 编译的 Supervisor 子图（route ↔ {researcher,
editor, reviewer, director}），挂在主图 ``creative`` 节点下。现扁平化为单层主图——
``supervisor_route_node`` 是主图裸节点，4 个 worker 由 ``create_subagent_graph``
编译后平铺为主图节点，每个 worker 完成后经刚性边回到 ``supervisor_route``。

- ``supervisor_route_node``：读状态 + 注入 SOP，用 ``SupervisorDecision``（json_mode）
  结构化输出 {next, reasoning}，写回 ``supervisor_next`` / ``supervisor_iteration``。
- ``supervisor_route_after``：主图条件边。命中 worker → 派发；FINISH/超迭代上限/
  未知 → 走 ``after_creative`` 的跳审稿判定（request_review 或 resource_prep）。
- ``make_chat_model`` 锁在 ``supervisor_route_node`` 函数体内，保配置热生效。
- 迭代硬守卫 ``supervisor_iteration >= MAX_ITERATIONS`` 强制 FINISH（不依赖 LLM）。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import SystemMessage

from agent.subagents.schemas import SupervisorDecision, make_chat_model
from agent.supervisor.prompts import SUPERVISOR_BASE_PROMPT
from agent.supervisor.sop.loader import load_sop

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8
# director 分镜合格性熔断的局部重试上限（共 MAX_DIRECTOR_RETRY+1 次 director 调用）。
# 超限仍不合格 → creative_fail 熔断登记失败，不再推进到 TTS/ASR/素材/渲染车间。
MAX_DIRECTOR_RETRY = 2
WORKERS = ["researcher", "editor", "reviewer", "director"]

# worker 花名册（拼入 route 的 prompt，让 Supervisor 认识手下）
_ROSTER = {
    "researcher": "资料员（挂联网搜索，检索热点/痛点/趣味事实）",
    "editor": "编剧（写/改 150-200 字口播文案）",
    "reviewer": "质检员（审文案，给通过或具体修改意见）",
    "director": "分镜导演（将通过质检的文案拆成带英文检索词的分镜）",
}

# SOP / 花名册不依赖 settings，模块级加载一次即可（不同于 make_chat_model 需按需构造）。
_SOP_TEXT = load_sop("standard_video_sop")
_ROSTER_TEXT = "\n".join(f"- `{k}`：{v}" for k, v in _ROSTER.items())


def _state_summary(state: dict[str, Any]) -> dict[str, str]:
    """把当前状态摘要成 route prompt 可读字段。"""
    review = state.get("script_review")
    if isinstance(review, dict) and review:
        review_summary = f"passed={review.get('passed')} feedback={str(review.get('feedback', ''))[:60]!r}"
    elif review:
        review_summary = str(review)
    else:
        review_summary = "(尚未质检)"
    script = state.get("script_text") or ""
    storyboard = state.get("storyboard") or []
    return {
        "topic": state.get("video_topic", "") or "(未知)",
        "iteration": str(state.get("supervisor_iteration", 0)),
        "max_iter": str(MAX_ITERATIONS),
        "has_research": "已有资料" if state.get("research_notes") else "无",
        "has_script": f"已写文案({len(script)}字)" if script else "无",
        "review_summary": review_summary,
        "has_storyboard": f"已出分镜({len(storyboard)}镜)" if storyboard else "无",
    }


async def _invoke_route_decision(model, prompt: str, attempts: int = 3):
    """route 结构化调用 + 重试。

    DeepSeek json_mode 对 ``reasoning`` 内未转义的双引号（如 ``"主题"咖啡"``）偶尔解析失败
    （OutputParserException）。重试让 LLM 重新生成换种表述即可；全失败返回 ``("", "")``
    由调用方走启发式 fallback。
    """
    for i in range(attempts):
        try:
            d = await model.ainvoke([SystemMessage(content=prompt)])
            nxt = (d.next or "").strip()
            if nxt:
                return nxt, d.reasoning or ""
        except Exception as e:  # OutputParserException 等
            logger.warning("[supervisor/route] 结构化解析失败(第%d次): %s", i + 1, str(e)[:120])
    logger.error("[supervisor/route] %d 次重试均失败，启用 fallback", attempts)
    return "", ""


def _fallback_next(state) -> str:
    """结构化全失败时的启发式路由（基于状态推进，绝不卡死）。"""
    if state.get("storyboard"):
        return "FINISH"
    if state.get("script_text"):
        return "director"
    return "editor"


def _storyboard_valid(state) -> bool:
    """分镜合格性审计（创意期→生产期咽喉闸门）。

    合格 = storyboard 非空 且 每个 shot 都有非空 search_prompt。
    search_prompt 是 resource_prep 唯一的素材检索词来源（video_nodes.py L154），
    任一缺失 → 该镜头搜不到素材 → render 注定 videos=0 失败。在此拦截避免浪费
    TTS/ASR/下载/渲染车间算力。空 storyboard（director 返空 / 解析降级置空）同样不合格。
    """
    shots = state.get("storyboard") or []
    if not shots:
        return False
    return all(isinstance(s, dict) and (s.get("search_prompt") or "").strip() for s in shots)


async def supervisor_route_node(state) -> dict:
    """主图创意中枢路由节点：按 SOP 决策下一步派哪个 worker。

    返回 ``supervisor_next`` / ``supervisor_iteration``（写回主图 AgentState）。
    ``make_chat_model`` 锁在函数体内第一行，保配置保存后下次对话即用新 key/model。
    """
    route_model = make_chat_model(0.4).with_structured_output(
        SupervisorDecision, method="json_mode"
    )
    prompt = SUPERVISOR_BASE_PROMPT.format(
        roster=_ROSTER_TEXT,
        sop=_SOP_TEXT or "(无 SOP，按常识自由调度)",
        **_state_summary(state),
    )
    # DeepSeek json_mode 对 reasoning 内未转义双引号偶尔解析失败 → 重试 + 启发式 fallback
    nxt, reasoning = await _invoke_route_decision(route_model, prompt)
    if not nxt:
        nxt = _fallback_next(state)
        reasoning = "(结构化解析失败，启发式 fallback)"
    it = int(state.get("supervisor_iteration", 0)) + 1
    sr = state.get("script_review")

    # ===== 咽喉审计：即将进生产期（nxt 非 worker = FINISH/超限/未知）时刚性拦截 storyboard =====
    # 不合格且未超限 → 改派 director 重干 + 清空残次 storyboard + 计数+1（director 无状态,
    #   清空 storyboard 防止 _fallback_next 误判已出分镜而 FINISH；重干时 director 拿到 script 重拆）。
    # 不合格且超限 → 设 video_route="creative_fail" 标志,supervisor_route_after 据此变轨熔断节点,
    #   不再推进到 resource_prep/render,避免对注定失败的残次品浪费 TTS/ASR/素材/渲染算力。
    update = {"supervisor_next": nxt, "supervisor_iteration": it}
    if nxt not in WORKERS and not _storyboard_valid(state):
        retry = int(state.get("director_retry_count", 0))
        if retry < MAX_DIRECTOR_RETRY:
            update["supervisor_next"] = "director"
            update["storyboard"] = []  # 清空残次品,director 重干
            update["director_retry_count"] = retry + 1
            logger.warning(
                "[supervisor/route] 分镜不合格（retry %d/%d）→ 重派 director 重干",
                retry + 1, MAX_DIRECTOR_RETRY,
            )
        else:
            update["video_route"] = "creative_fail"
            logger.error(
                "[supervisor/route] 分镜不合格且超限（retry %d/%d）→ 熔断 creative_fail",
                retry, MAX_DIRECTOR_RETRY,
            )

    logger.info(
        "[supervisor/route] iter=%d next=%s | script=%s review_passed=%s storyboard=%s | %s",
        it, update["supervisor_next"], bool(state.get("script_text")),
        sr.get("passed") if isinstance(sr, dict) else None,
        bool(state.get("storyboard")),
        (reasoning or "")[:80],
    )
    return update


def supervisor_route_after(state) -> str:
    """主图条件边：supervisor_route 之后派谁。

    - 命中 worker → 派发该 worker（每个 worker 经刚性边回 supervisor_route 形成微观循环）。
    - FINISH / 超迭代上限 / 未知 → 走 after_creative 的跳审稿判定（request_review 或
      resource_prep）。after_creative 复用原逻辑，按 user_auto_review / batch_auto_review
      决定是否跳过人工审稿。
    - 分镜熔断（supervisor_route_node 审计 storyboard 不合格且超限时设 video_route=
      "creative_fail"）→ 变轨 creative_fail 节点登记失败,不再推进到 resource_prep/render。
    """
    # 分镜熔断优先：creative_fail 标志由 supervisor_route_node 咽喉审计设置
    if state.get("video_route") == "creative_fail":
        return "creative_fail"
    # 硬守卫优先：超迭代上限强制走 after_creative（不依赖 LLM 自觉，即使 next 仍是 worker 也打断）
    if int(state.get("supervisor_iteration", 0)) >= MAX_ITERATIONS:
        logger.warning("[supervisor] 达迭代上限 %d，强制 FINISH", MAX_ITERATIONS)
        nxt = "FINISH"
    else:
        nxt = state.get("supervisor_next", "FINISH")
    if nxt in WORKERS:
        return nxt
    # FINISH / 未知 / 超上限 → 交下游审稿判定
    from agent.nodes.video_nodes import after_creative
    return after_creative(state)
