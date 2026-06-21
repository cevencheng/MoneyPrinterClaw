"""
Agent Skills 标准 client（agentskills.io 规范实现）。

渐进式披露三层模型：
1. discovery：启动扫描 SKILL.md → catalog [{name, description, location, requires_env}]
2. activate_skill 工具：按 description 语义匹配 → 加载完整指令 + 资源清单（不预读）
3. run_skill_script / read_skill_resource：受控执行脚本 / 读参考文件

模块对外接口：
- discover_skills / refresh_skills / get_catalog：发现 + COW 缓存
- activate：单个 skill 的指令 + 资源清单加载
- render_catalog_prompt：catalog 文本渲染（拼进主 agent system prompt）
"""

from agent.skills.catalog import render_catalog_prompt
from agent.skills.discovery import SkillMeta, discover_skills, get_catalog, refresh_skills
from agent.skills.loader import activate

__all__ = [
    "SkillMeta",
    "discover_skills",
    "get_catalog",
    "refresh_skills",
    "activate",
    "render_catalog_prompt",
]
