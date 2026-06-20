"""
plan_video_batch 虚拟工具（批量视频创作入口）

LLM 调用它表示"批量开拍"意图。与 plan_video 同：该工具不经 ToolNode 执行——
主图 build.py 的 should_continue 检测到 plan_video_batch 调用后，路由到 batch_start，
进入串行队列（Phase 1 路线 A）：逐个主题复用单视频流水线（creative→review→render），
每轮独立 task_id + 重置上下文（多轮隔离），全部完成后 batch_summary 汇总。
函数体不会被调用，仅为 @tool schema 而存在。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def plan_video_batch(topics: list[str], auto_review: bool = True, subtitle_size: int | None = None) -> str:
    """
    批量启动多条短视频创作流水线。当用户要求"一次做 N 条 / 批量做关于 X 的多个视频"时调用。

    参数：
      topics：差异化主题列表（必须从不同角度切入，严禁雷同。例：主题"咖啡"应拆成
              ["咖啡的历史与起源", "手冲咖啡入门教程", "空腹喝咖啡的健康误区", ...]，
              而非 10 个"咖啡"）。建议 2-10 条。
      auto_review：是否逐条人工审稿。True=每条视频创意后弹审稿卡，用户确认再渲染下一条
                   （默认，稳）；False=静默排队，全自动跑完不阻断。
      subtitle_size：可选，字幕字号（整批统一）。用户提到字幕大小（"小一点/大一点/字号N"）时传入；
          模糊词映射：小=14、中=20（默认）、大=28、超大=36；用户给具体数字就直接用。
          不传或 None = 用全局默认（config.toml 的 video_font_size）。

    调用后系统串行处理每个主题：文案 → 分镜 →（人工确认，可选）→ 配音/素材 → 渲染，
    全部完成后返回每条的成片路径汇总。
    """
    return f"已启动批量视频创作：{len(topics)} 条"
