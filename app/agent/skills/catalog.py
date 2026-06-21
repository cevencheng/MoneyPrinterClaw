"""
Catalog 文本渲染：把 catalog 摘要拼成 system prompt 片段。

被 agent_node 在每次构造 prompt 时调用,从 COW 缓存读当前快照——
新增 skill 后 refresh_skills() 即生效,无需重新 build_agent。
"""

from __future__ import annotations

from agent.config import settings
from agent.skills.discovery import get_catalog


def render_catalog_prompt() -> str:
    """渲染 catalog 为主 agent system prompt 片段。

    skills 关闭 / 无可用 skill → 空串（不污染 prompt）。
    """
    if not settings.skills_enabled:
        return ""
    catalog = get_catalog()
    if not catalog:
        return ""
    lines = [
        "## 可用技能（Skills）",
        "当用户任务匹配下列某技能的描述时,先调用 activate_skill(name) 加载完整指令,再按指令执行。",
        "技能由项目维护者预先放置,绝对禁止虚构不在清单内的 skill name。",
        "",
    ]
    for m in catalog:
        # 单行格式：- name: description
        # description 可能很长,这里不截断（标准建议每条 50-100 token,实际由 SKILL.md 作者控制）
        lines.append(f"- {m.name}: {m.description}")
    return "\n".join(lines)
