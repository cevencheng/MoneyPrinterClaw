"""
redo_failed_videos 虚拟工具（批量失败轮补做入口）

LLM 调用它表示"补做失败轮"意图。与 plan_video / plan_video_batch 同：该工具不经 ToolNode
执行——主图 build.py 的 should_continue 检测到 redo_failed_videos 调用后，路由到 batch_redo_start，
后者从 batch_results 挑出失败轮（final_video_path 空），复用其原 task_id 重新走 resource_prep→render
（progress.json 自动跳过已完成的 audio/materials/combine 子步，只重做失败的）。
函数体不会被调用，仅为 @tool schema 而存在。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def redo_failed_videos() -> str:
    """
    补做上一批批量任务中失败的视频。当用户在批量完成后说"补做失败的 / 重试失败的那几个 /
    把没做成的再试一次"时调用。

    补做会复用每个失败视频的原任务（不重新写文案、不重新下载已下好的素材），
    只重做当时崩掉的步骤（拼接/渲染/或重新下载素材），直到成功或用户放弃。

    无需参数：系统自动从当前会话的批量结果中挑出失败项。
    """
    return "已开始补做失败的视频"
