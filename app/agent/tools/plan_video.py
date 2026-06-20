"""
plan_video 虚拟工具

LLM 调用它表示"开拍"意图。注意：该工具不经 ToolNode 执行——
主图 build.py 的 should_continue 检测到 plan_video 调用后，路由到 video 包装节点
（视频子图），由后者执行真正的视频生成，并把结果作为本 tool 的 ToolMessage 回喂主对话。
因此本函数体不会被调用，仅为满足 @tool 装饰器与 schema 而存在。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def plan_video(topic: str, subtitle_size: int | None = None) -> str:
    """
    启动短视频创作流水线。当用户要求"做一个 / 生成 / 拍一个关于 X 的短视频、解说视频"时调用。
    参数 topic：视频主题（如"深圳美食探店"、"AI 发展史"）。
    参数 subtitle_size：可选，字幕字号。用户提到字幕大小（"小一点/大一点/字号N"）时传入；
        模糊词映射：小=14、中=20（默认）、大=28、超大=36；用户给具体数字就直接用。
        不传或 None = 用全局默认（config.toml 的 video_font_size）。
    调用后进入 文案 → 分镜 →（人类确认）→ 配音/素材 → 渲染 的完整流程，完成后返回成片路径。
    """
    return f"已启动视频创作：{topic}"
