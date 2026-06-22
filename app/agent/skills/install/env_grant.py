"""
增量管理 config.toml 的 skills_env_allowlist。

用户在 webui 预安装审查时勾选「授权 OPENAI_API_KEY」,这个模块就负责把 key 加进
allowlist + 原子写回 toml + 同步 settings 内存值,主 agent 下一次跑 skill 即可读到。

复用 api/settings.py 的 _load_toml / _dump_toml / 原子写入逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import re
import tomllib
from pathlib import Path

from agent.config import Settings, settings
from agent.skills.discovery import refresh_skills

logger = logging.getLogger(__name__)


# 项目根（与 api/settings.py:34 同款锚定）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_TOML = _PROJECT_ROOT / "config.toml"

# env key 命名规范：大写 ASCII 字母 + 数字 + 下划线（标准 POSIX env 变量）
_VALID_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_keys(keys: list[str]) -> list[str]:
    """校验 + 去重保序。非法 key 直接抛 ValueError。"""
    seen: set[str] = set()
    result: list[str] = []
    for k in keys:
        k = k.strip()
        if not k:
            continue
        if not _VALID_ENV_KEY.match(k):
            raise ValueError(
                f"非法环境变量名: {k!r}（必须以大写字母开头,仅含 A-Z 0-9 _）"
            )
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


async def grant_env(keys: list[str]) -> dict:
    """把 keys 增量并入 skills_env_allowlist,原子写回 config.toml + 刷新内存。

    Returns:
        {"allowlist": [...], "added": [...]}：完整白名单 + 本次新增项。

    Raises:
        ValueError: keys 含非法字符。
        FileNotFoundError: config.toml 不存在。
    """
    valid_keys = _validate_keys(keys)
    if not _CONFIG_TOML.exists():
        raise FileNotFoundError(f"config.toml 不存在: {_CONFIG_TOML}")

    # 读现有 toml
    def _read_write_sync(new_keys: list[str]) -> tuple[list[str], list[str]]:
        """同步实现读 + 改 + 原子写。在 to_thread 里跑。"""
        with open(_CONFIG_TOML, "rb") as f:
            raw = tomllib.load(f)

        # 并入 allowlist
        existing = list(raw.get("skills_env_allowlist", []) or [])
        existing_set = set(existing)
        added: list[str] = []
        for k in new_keys:
            if k not in existing_set:
                existing.append(k)
                existing_set.add(k)
                added.append(k)
        raw["skills_env_allowlist"] = existing

        # 原子写回（复用 api/settings.py:_dump_toml 的 list 分支扩展）
        from api_view.api.settings import _dump_toml
        text = _dump_toml(raw)
        tmp = _CONFIG_TOML.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(_CONFIG_TOML)
        return existing, added

    full_list, added = await asyncio.to_thread(_read_write_sync, valid_keys)

    # 同步内存（settings 全局单例,不重启即生效）
    fresh = Settings()
    settings.skills_env_allowlist = list(fresh.skills_env_allowlist)

    # 刷新 catalog（虽然 env 变化不影响 catalog 内容,但顺手保持热刷新一致性）
    await asyncio.to_thread(refresh_skills)

    logger.info(
        "[skills/install/env] 授权 %d 个 key,白名单总数 %d (新增 %s)",
        len(added), len(full_list), added,
    )
    return {"allowlist": full_list, "added": added}
