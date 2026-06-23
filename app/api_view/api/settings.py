"""
全局配置读写 API

GET  /api/settings  → 读 config.toml 返回前端可见字段(key 字段返回明文,由前端按密码框显示)
POST /api/settings  → 校验 + 写回 config.toml(原子 .tmp + os.replace)+ 即时刷新内存单例

即时生效(无需重启):
    写完 toml 后重新实例化 Settings()(env>toml>defaults 同优先级重读)并把字段拷回现存单例。
    博查/Pexels/Pixabay 消费点本就每次调用读 settings.*,单例刷新后下一次工具调用即用新值;
    openai_api_key + model_* 由节点每次调用时按需构造 ChatOpenAI,下一次对话即用新值。
    进行中的请求用旧客户端跑完,不受影响。

字段白名单:
    暴露给前端的字段是"用户必须配的依赖"那一组,系统内部字段(sandbox_*, vector_db_path,
    *_dir, langgraph 相关...)一律不暴露,防止用户改坏运行时。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()


# 项目根 config.toml(与 agent/config.py 同源解析)
# 路径深度:本文件 app/api_view/api/settings.py → parents[3] = 项目根
# (agent/config.py 用 parents[2] 是因为它只在 app/agent/ 下,少一层)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_TOML = _PROJECT_ROOT / "config.toml"

# 暴露给前端可读 / 可写的字段白名单。3 组:LLM / 联网搜索 / 视频素材。
# value: ("group", type) — type 用于 POST 校验
_EXPOSED_FIELDS: dict[str, tuple[str, type]] = {
    # LLM
    "openai_api_key":    ("llm", str),
    "openai_base_url":   ("llm", str),
    "model_name":        ("llm", str),
    "model_temperature": ("llm", float),
    # 联网搜索
    "bocha_api_key":     ("search", str),
    # 视频素材
    "video_pexels_api_keys":  ("media", str),
    "video_pixabay_api_keys": ("media", str),
    # Skills
    "github_token":          ("skills", str),
    "skills_env_allowlist":  ("skills", list),
}

# API key 字段(GET 返回明文 value,前端按密码框显示;与普通字段区分仅为 UI 语义)
_KEY_FIELDS = {
    "openai_api_key", "bocha_api_key",
    "video_pexels_api_keys", "video_pixabay_api_keys",
    "github_token",
}


def _load_toml() -> dict[str, Any]:
    """读 config.toml,返回顶层 key 的 dict。文件缺 / 损坏返回 {}。"""
    if not _CONFIG_TOML.exists():
        return {}
    try:
        # Python 3.11+ 内置 tomllib
        import tomllib
        with open(_CONFIG_TOML, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.warning("[settings] 读 config.toml 失败: %s", e)
        return {}


def _dump_toml(data: dict[str, Any]) -> str:
    """把 dict 序列化为 TOML 文本（只支持顶层标量 + list[str]）。

    不引入 tomli_w 这个额外依赖(避免 requirements.txt 膨胀)。我们的 config.toml 结构
    一直是扁平 key=value(没有嵌套 table),手写序列化够用且更可控。
    list[str] 分支用于 skills_env_allowlist 等数组配置项。
    """
    lines: list[str] = []
    for k, v in data.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        elif isinstance(v, str):
            # 转义 " 和 \,然后用双引号包(TOML basic string 规则)
            esc = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{esc}"')
        elif isinstance(v, list):
            # list 仅支持元素为 str（skills_env_allowlist 等场景）。其它类型严格拒绝。
            if not all(isinstance(x, str) for x in v):
                raise ValueError(f"settings._dump_toml list 仅支持 str 元素: {k}")
            items = ", ".join(
                '"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"' for x in v
            )
            lines.append(f"{k} = [{items}]")
        else:
            raise ValueError(f"settings._dump_toml 不支持的类型: {k}={type(v).__name__}")
    return "\n".join(lines) + "\n"


@router.get("")
async def get_settings() -> dict[str, Any]:
    """读当前 config.toml 的可见字段;key 字段也以明文返回,由前端按密码输入框显示。"""
    raw = _load_toml()
    out: dict[str, Any] = {}
    for field, (group, _typ) in _EXPOSED_FIELDS.items():
        val = raw.get(field, "")
        if field in _KEY_FIELDS:
            out[field] = {
                "group": group,
                "is_key": True,
                "value": val if isinstance(val, str) else "",
            }
        else:
            out[field] = {
                "group": group,
                "is_key": False,
                "value": val,
            }
    return {"fields": out}


@router.post("")
async def update_settings(request: Request) -> dict[str, Any]:
    """更新 config.toml + 即时刷新内存中的 settings 单例。

    body: {"fields": {field_name: new_value, ...}}
    - 仅白名单字段会被写;非白名单字段忽略(防注入)
    - 类型校验失败 → 400
    - key 字段空字符串视为"清空" → 写空串(允许用户主动 unset)
    - 其它顶层 toml 字段(白名单外的, 如 sqlite_db_path / video_tasks_dir 等)原样保留
    - 原子写: tmp + os.replace

    即时生效策略(无需重启后端):
        写完 toml 后,重新实例化 Settings()(按 env>toml>defaults 同样的优先级重读,
        和进程重启语义一致),把白名单字段拷回现存的模块级 settings 单例。
        - bocha/pexels/pixabay:消费点(web_search / video bridge)本就每次调用读 settings.*,
          单例刷新后下一次工具调用即用新值。
        - openai_api_key + model_*:ChatOpenAI 在节点每次调用时按需构造(见 build.py /
          subagents/schemas.make_chat_model 的调用点),单例刷新后下一次对话即用新值。
        不重建 LangGraph graph、不重建 checkpointer,进行中的请求用旧客户端跑完不受影响。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    incoming = (body or {}).get("fields") or {}
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="fields 必须是 object")

    raw = _load_toml()
    written: list[str] = []
    for field, new_val in incoming.items():
        if field not in _EXPOSED_FIELDS:
            continue  # 非白名单跳过
        _group, expected_type = _EXPOSED_FIELDS[field]

        # 类型校验 + 强制转换(允许 string→float 这种 UI 友好转换)
        try:
            if expected_type is float:
                coerced: Any = float(new_val) if new_val != "" else 0.0
            elif expected_type is int:
                coerced = int(new_val) if new_val != "" else 0
            elif expected_type is str:
                coerced = str(new_val) if new_val is not None else ""
            elif expected_type is list:
                # 仅接受 list[str]（如 skills_env_allowlist）。None/缺失 → 空 list；
                # 元素全部 str 化 + strip + 空过滤,保持 _dump_toml 的 list 分支要求。
                if new_val is None:
                    coerced = []
                elif isinstance(new_val, list):
                    coerced = [str(x).strip() for x in new_val if str(x).strip()]
                else:
                    raise TypeError(f"list 字段需要 array,收到 {type(new_val).__name__}")
            else:
                coerced = expected_type(new_val)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"字段 {field} 类型应为 {expected_type.__name__},收到 {type(new_val).__name__}: {new_val!r}",
            )
        raw[field] = coerced
        written.append(field)

    # 原子写 toml
    tmp = _CONFIG_TOML.with_suffix(".toml.tmp")
    text = _dump_toml(raw)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(_CONFIG_TOML)
    logger.info("[settings] 已更新 %d 个字段: %s", len(written), written)

    # 即时刷新内存单例(等价于重启后重读,但不中断服务)。
    # 用新 Settings() 实例的值覆盖现存单例的字段 —— 所有 `from agent.config import settings`
    # 的消费点持有的都是同一个对象引用,就地 setattr 后它们下次读即拿到新值。
    try:
        from agent.config import Settings, settings as _live

        fresh = Settings()
        for field in _EXPOSED_FIELDS:
            setattr(_live, field, getattr(fresh, field))
        logger.info("[settings] 内存单例已刷新,即时生效")
    except Exception as e:
        # 刷新失败不影响已落盘的 toml;提示用户重启兜底
        logger.warning("[settings] 内存单例刷新失败(%s),需手动重启后端生效", e)
        return {
            "status": "saved",
            "updated": written,
            "needs_restart": True,
            "message": f"配置已写入 config.toml,但内存刷新失败: {e}。请重启后端生效",
        }

    return {
        "status": "saved",
        "updated": written,
        "needs_restart": False,
        "message": "配置已保存并即时生效（下一次对话 / 工具调用即用新值）",
    }
