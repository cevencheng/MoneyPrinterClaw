"""加载 SOP（标准作业程序）markdown —— 注入 Supervisor route 节点的作战地图。

SOP 是"口语化 Workflow"：用自然语言描述团队成员与执行流程（Blueprint），
由 Supervisor LLM 据此动态路由。MVP 从本目录读 .md 文件；多 SOP/DB 持久化留作未来。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SOP_DIR = Path(__file__).parent


def load_sop(name: str) -> str:
    """按名字（不含扩展名）加载 ``sop/<name>.md``。

    不存在则返回空串并告警（Supervisor 降级为无地图的自由路由）。
    """
    path = SOP_DIR / f"{name}.md"
    if not path.exists():
        logger.warning("[sop] 未找到 SOP %s，Supervisor 将无作战地图（自由路由降级）", path)
        return ""
    return path.read_text(encoding="utf-8")
