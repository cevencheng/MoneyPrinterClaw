"""
子图工厂（Sub-graph Factory）：YAML 配置驱动的 LangGraph 子图编译。

设计要点（详见 plan；语义已用 Plan agent 对 LangGraph 1.1.3 实测验证）：
- 每个子 Agent 读一个或多个 AgentState 输入字段、写一个输出字段（非 messages 通信）。
- 两类输出：``text``（纯文本）/ ``structured``（json_mode 结构化，可 flatten 成列表）。
- 工具可选：``tools`` 非空且 mode != structured 时走 ReAct 循环。
- 子图用**私有状态**：只含 input/output 字段（+ ReAct 时的 sub_messages），**绝无 messages 通道** →
  内部消息不会污染父图对话（实测：子图无 messages 通道则写入被丢弃；同名 input/output 通道照常回写）。
- ``compile()`` 不带 checkpointer → 继承父图，父节点里的 interrupt() 照常挂起/恢复。
- 子图内部节点命名 ``run``（非 ``agent``）→ ``chat.py`` 的 ``astream_events`` 过滤生效，token 不侧漏。

YAML 字段（在 loader 的 name/description/tools 之上扩展）：
    model: {temperature: float, structured_output?: {schema, method}}
    input: {field, placeholder}  或  多输入列表 [{field, placeholder}, ...]
           （dict/list 类型的值会渲染成可读 JSON，便于 prompt 引用，如质检反馈）
    output: {field, mode: text|structured, flatten_key?: <属性名>}
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from openai import BadRequestError

from agent.config import settings
from agent.subagents.loader import load_subagent_config
from agent.subagents.schemas import SCHEMAS, make_chat_model

logger = logging.getLogger(__name__)


# ReAct 整理模式追加的通用指令后缀：达到工具调用上限后，让 LLM 基于已检索资料
# 直接整理输出 out_field，不再调工具（对所有 ReAct worker 通用，目前仅 researcher）。
_REACT_FINALIZE_SUFFIX = (
    "\n\n（已达到工具调用次数上限，请基于已获取的信息直接整理输出最终结果，不要再调用任何工具。）"
)


def _content_str(resp: Any) -> str:
    """统一抽取 LLM 响应的文本内容。"""
    content = getattr(resp, "content", resp)
    return content if isinstance(content, str) else str(content)


def _render_val(v: Any) -> str:
    """把状态字段值渲染成 prompt 可用字符串：dict/list → 可读 JSON，None → ""，其余 str()。"""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v if isinstance(v, str) else str(v)


def create_subagent_graph(config_name: str, *, tools_map: dict[str, Any] | None = None):
    """读 YAML → 绑模型/工具/结构化输出 → 编译子图（不带 checkpointer，继承父图）。

    Args:
        config_name: YAML 配置名（不含扩展名，如 ``"chief_editor"``）。
        tools_map: 工具名 → 工具可调用对象 的映射；YAML ``tools`` 列表里的名字必须在此命中，
            否则失败快速抛 ``ValueError``。

    Returns:
        编译好的 ``CompiledStateGraph``，可直接 ``workflow.add_node(name, subgraph)`` 挂载。
    """
    cfg = load_subagent_config(f"{config_name}.yaml")
    tools_map = tools_map or {}

    sys_tmpl: str = cfg["system_prompt"]
    # input：单 {field,placeholder} 或多 [{field,placeholder}...] → 归一为 (field, placeholder) 列表
    in_spec = cfg["input"]
    input_pairs: list[tuple[str, str]] = (
        [(s["field"], s["placeholder"]) for s in in_spec]
        if isinstance(in_spec, list)
        else [(in_spec["field"], in_spec["placeholder"])]
    )
    out_field: str = cfg["output"]["field"]
    mode: str = cfg["output"]["mode"]
    flatten_key: str | None = cfg["output"].get("flatten_key")
    temp: float = float(cfg.get("model", {}).get("temperature", 0.7))

    # 工具解析（失败快速）：未知工具直接报错，避免静默跑空
    tools = []
    for name in cfg.get("tools") or []:
        if name not in tools_map:
            raise ValueError(f"子 Agent '{config_name}' 引用了未知工具: {name}（请先注册进 tools_map）")
        tools.append(tools_map[name])

    is_react = bool(tools) and mode == "text"
    if mode == "structured" and tools:
        # DeepSeek 约束：json_mode 与 forced tool_choice 互斥 → 结构化模式忽略工具
        logger.warning(
            "子 Agent '%s' 同时配置了 structured_output 与 tools，结构化优先、忽略工具 %s",
            config_name,
            [t.name for t in tools],
        )

    def _build_prompt(state) -> str:
        """从 input_pairs 取状态字段、渲染、填充 system_prompt 占位符。"""
        vals = {ph: _render_val(state.get(fld, "")) for fld, ph in input_pairs}
        return sys_tmpl.format(**vals)

    # ---- 节点逻辑 + 模型绑定（按 mode 分支，互斥）----
    if mode == "structured":
        so = cfg["model"]["structured_output"]
        schema = SCHEMAS[so["schema"]]

        async def run(state):  # noqa: ANN202
            # 每次调用时构造,读 settings 最新值,保存后即时生效
            bound = make_chat_model(temp).with_structured_output(
                schema, method=so.get("method", "json_mode")
            )
            prompt = _build_prompt(state)
            try:
                result = await bound.ainvoke([SystemMessage(content=prompt)])
            except (BadRequestError, Exception) as e:
                # DeepSeek 风控（"Content Exists Risk"）/ JSON 破损 / OutputParserException 等：
                # 异常击穿 LangGraph stream 会让 SSE 流断、前端卡片永久卡住。对齐 ReAct 模式的
                # BadRequestError 降级——置空 out_field,让创意期咽喉审计（supervisor_route_node 的
                # _storyboard_valid）判定不合格 → 重派 director 或熔断 creative_fail,不再击穿流。
                msg_text = str(e)
                is_content_risk = "Content Exists Risk" in msg_text or "content_filter" in msg_text.lower()
                logger.warning(
                    "[subagent/%s] structured 解析失败 %s -> 降级 %s='' (空/列表置空)",
                    config_name,
                    "内容风控" if is_content_risk else f"{type(e).__name__}({msg_text[:120]})",
                    out_field,
                )
                # flatten_key 模式（如 director 的 shots）置空 list；否则置空值
                value = [] if flatten_key else ""
                return {out_field: value}
            value = (
                [x.model_dump() for x in getattr(result, flatten_key)]
                if flatten_key
                else result.model_dump()
            )
            logger.info(
                "[subagent/%s] structured -> %s (len=%d)",
                config_name, out_field, len(value) if isinstance(value, list) else 1,
            )
            return {out_field: value}

    elif is_react:
        async def run(state):  # noqa: ANN202
            history = state.get("sub_messages") or []
            # 累计 ToolMessage 数 = 已完成的工具调用次数。达到上限 → 切「整理模式」：
            # 不 bind_tools（LLM 物理上无法再调工具）+ 追加整理指令，把已检索资料整理成
            # out_field 文本。避免原 should_continue 硬截断时 last AIMessage 带 tool_calls、
            # out_field 不被写入 → 检索成果丢失（搜满上限反而不如主动收手）。
            tool_count = sum(1 for m in history if isinstance(m, ToolMessage))
            limit = settings.search_react_tool_call_limit
            finalize = tool_count >= limit
            if finalize:
                bound = make_chat_model(temp)  # 不 bind_tools：强制收尾，不再调工具
                prompt = _build_prompt(state) + _REACT_FINALIZE_SUFFIX
                logger.info(
                    "[subagent/%s] ReAct 达上限 %d（已 %d 调用）→ 整理模式产出 %s",
                    config_name, limit, tool_count, out_field,
                )
            else:
                # 每次调用时构造,读 settings 最新值,保存后即时生效
                bound = make_chat_model(temp).bind_tools(tools)
                prompt = _build_prompt(state)
            msgs = [SystemMessage(content=prompt)] + list(history)
            try:
                resp = await bound.ainvoke(msgs)
            except BadRequestError as e:
                # DeepSeek 风控（"Content Exists Risk"）/ 上下文过长 等 400：
                # 异常击穿 LangGraph stream 会让 SSE 流断、前端卡片永久卡住。
                # 这里降级：写空 out_field + 注入无 tool_calls 的 AIMessage,让 should_continue
                # 见到 last 无 tool_calls 直接 END,editor 凭 video_topic 凑合写文案,视频能产但研究深度降级。
                msg_text = str(e)
                is_content_risk = "Content Exists Risk" in msg_text or "content_filter" in msg_text.lower()
                logger.warning(
                    "[subagent/%s] LLM 400 %s -> 降级 %s='' 退 ReAct",
                    config_name,
                    "内容风控" if is_content_risk else f"BadRequest({msg_text[:120]})",
                    out_field,
                )
                return {
                    out_field: "",
                    "sub_messages": [AIMessage(content="")],
                }
            out: dict[str, Any] = {"sub_messages": [resp]}
            # 整理模式必写 out_field（强制收尾产出）；正常模式仅 last 无 tool_calls 时写
            if finalize or not getattr(resp, "tool_calls", None):
                out[out_field] = _content_str(resp)
                logger.info("[subagent/%s] react done -> %s", config_name, out_field)
            return out

        def should_continue(state):  # noqa: ANN202
            history = state.get("sub_messages") or []
            last = history[-1] if history else None
            if not (last and getattr(last, "tool_calls", None)):
                return END
            # 安全网：已达上限仍带 tool_calls（整理模式未生效的极端防御）→ 强制 END，防死循环。
            # 主路径靠 run 的整理模式（不 bind_tools，LLM 无法返回 tool_calls），此处兜底。
            tool_count = sum(1 for m in history if isinstance(m, ToolMessage))
            if tool_count >= settings.search_react_tool_call_limit:
                logger.warning(
                    "[subagent/%s] ReAct 累计工具调用 %d ≥ %d,安全网强制 END",
                    config_name, tool_count, settings.search_react_tool_call_limit,
                )
                return END
            return "tools"

    elif mode == "text":
        async def run(state):  # noqa: ANN202
            # 每次调用时构造,读 settings 最新值,保存后即时生效
            bound = make_chat_model(temp)
            prompt = _build_prompt(state)
            resp = await bound.ainvoke([SystemMessage(content=prompt)])
            text = _content_str(resp)
            logger.info("[subagent/%s] text -> %s (len=%d)", config_name, out_field, len(text))
            return {out_field: text}

    else:
        raise ValueError(f"子 Agent '{config_name}' 未知 output.mode: {mode}（应为 text|structured）")

    # ---- 私有子图状态：含全部 input + output 字段，绝无 messages 通道（防污染）----
    fields: dict[str, Any] = {fld: Any for fld, _ in input_pairs}
    fields[out_field] = Any
    if is_react:
        fields["sub_messages"] = Annotated[list, add_messages]  # ReAct 私有累加器（父图无此通道 → 不外泄）
    SubState = TypedDict(f"SubState_{config_name}", fields, total=False)

    builder = StateGraph(SubState)
    builder.add_node("run", run)  # 节点名 "run"（非 "agent"）：防 astream_events 把子图 token 侧漏到前端
    builder.set_entry_point("run")
    if is_react:
        builder.add_node("tools", ToolNode(tools, messages_key="sub_messages"))
        builder.add_conditional_edges("run", should_continue, ["tools", END])
        builder.add_edge("tools", "run")
    else:
        builder.add_edge("run", END)

    return builder.compile()  # 不传 checkpointer → 继承父图（HITL interrupt 在父节点照常工作）
