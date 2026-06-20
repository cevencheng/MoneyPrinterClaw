"""[已废弃] Supervisor 创意中枢子图的状态定义。

历史用途：原 Supervisor 子图（``create_supervisor_graph``）的 StateGraph 状态。
2026-06-20 扁平化重构后，Supervisor 子图被拆解——``supervisor_route`` 成为主图裸节点，
``next`` / ``supervisor_iteration`` 上摆进顶层 ``AgentState``（见
``agent/graph/agent_state.py`` 的 ``supervisor_next`` / ``supervisor_iteration``）。

本文件不再被任何代码引用，保留作历史归档。如需彻底清理可直接删除本文件。
"""

from __future__ import annotations

from typing import Any, TypedDict


class SupervisorState(TypedDict, total=False):
    """[已废弃] 创意中枢子图状态。字段已并入顶层 AgentState。"""

    video_topic: str            # 输入：视频主题（researcher 读；由顶层 video_start 写入）
    research_notes: str         # researcher 出 / editor 读（上透顶层）
    script_text: str            # editor 出 / reviewer、director 读（上透顶层）
    script_review: Any          # reviewer 出 {passed, feedback}（上透顶层）
    storyboard: list            # director 出（上透顶层）
    next: str                   # route 出：下一个 worker 或 FINISH（子图局部）→ 现为 supervisor_next
    supervisor_iteration: int   # route 出：循环计数（子图局部，硬守卫用）→ 现并入顶层
