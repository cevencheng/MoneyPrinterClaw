"""
子 Agent 共享的结构化 schema + LLM 构造（独立无环模块）。

供 factory.py 与 build.py 复用：
- Shot / Storyboard：导演结构化输出的分镜模型（DeepSeek json_mode 目标）。
- SCHEMAS：YAML ``output.structured_output.schema`` 名 → Pydantic 模型 的注册表，
  factory 按名字解析，避免硬编码导入视频专用模型（解耦、防导入环）。
- make_chat_model：从 settings 构造 OpenAI 兼容 ChatOpenAI（原 video_nodes._make_model）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent.config import settings


# ---- 结构化分镜 schema（标准 Pydantic v2，跨模型最稳）----
class Shot(BaseModel):
    """单个分镜：一句口播文案 + 一个 Pexels 英文检索词。"""

    text: str = Field(description="该镜头对应的口播文案句子")
    search_prompt: str = Field(
        description="用于在 Pexels 搜索的高质量英文关键词，4k / cinematic 风格"
    )


class Storyboard(BaseModel):
    """整段分镜：4-6 个镜头。导演 ``with_structured_output(method="json_mode")`` 的目标模型。"""

    shots: list[Shot] = Field(description="将文案拆分为 4-6 个分镜")


class ScriptReview(BaseModel):
    """文案质检结论：reviewer 子 Agent 的结构化输出（json_mode）。"""

    passed: bool = Field(description="文案是否通过质检")
    feedback: str = Field(description="未通过时的具体修改意见；通过时可为空或简短肯定")


class SupervisorDecision(BaseModel):
    """Supervisor 路由决策：route 节点的结构化输出（json_mode）。"""

    next: str = Field(description="下一步派给哪个 worker（researcher/editor/reviewer/director），或 FINISH")
    reasoning: str = Field(description="本次路由的简短理由")


# YAML ``output.structured_output.schema`` 名 → Pydantic 模型
SCHEMAS: dict[str, type[BaseModel]] = {
    "storyboard": Storyboard,
    "script_review": ScriptReview,
}


def make_chat_model(temperature: float):
    """构造 OpenAI 兼容 ChatOpenAI（实测后端为 DeepSeek）。子图按各自 temperature 调用。"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.model_name,
        temperature=temperature,
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        max_tokens=settings.model_max_tokens,
    )
