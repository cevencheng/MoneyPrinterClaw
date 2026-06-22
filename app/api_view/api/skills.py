"""
Skills 管理 API

提供 skill 列表/预览/安装/卸载/刷新/env 授权 6 个端点：
- GET    /api/skills                列已安装 skill（含 project/user 来源标识）
- POST   /api/skills/preview        装前审查（URL 或 zip_b64）
- POST   /api/skills/install        实际安装（URL 或 zip_b64）
- DELETE /api/skills/{name}         卸载 user 级 skill
- POST   /api/skills/refresh        手动重扫 catalog
- POST   /api/skills/env-allowlist  增量授权环境变量
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from agent.skills.discovery import get_catalog, refresh_skills
from agent.skills.install import (
    grant_env,
    install_from_url,
    install_from_zip_b64,
    preview_from_url,
    preview_from_zip_b64,
    uninstall,
)
from agent.skills.install import installer as _installer_mod

logger = logging.getLogger(__name__)
router = APIRouter()


def _classify_source(location: str) -> str:
    """根据 skill 目录位置判断来源：user（INSTALL_BASE 下）/ project（其余,如 .agents/skills/）。

    动态从 installer 模块读 INSTALL_BASE,而非 from import 绑定到 router 模块——
    支持测试期 monkey-patch installer.INSTALL_BASE 重定向到 tmp。
    """
    try:
        Path(location).resolve().relative_to(_installer_mod.INSTALL_BASE)
        return "user"
    except ValueError:
        return "project"


def _has_install_meta(location: str) -> bool:
    """目录有 .install.json → 可卸载（user 级有元数据）。"""
    return (Path(location) / ".install.json").is_file()


@router.get("")
async def list_skills() -> list[dict]:
    """列出当前 catalog 内所有 skill。"""
    out: list[dict] = []
    for m in get_catalog():
        out.append({
            "name": m.name,
            "description": m.description,
            "version": m.version,
            "requires_env": list(m.requires_env),
            "location": m.location,
            "source": _classify_source(m.location),
            "removable": _has_install_meta(m.location),
        })
    return out


@router.post("/preview")
async def preview_skill(request: Request) -> dict:
    """装前审查：返回 SKILL.md 摘要 + 脚本清单（含前 50 行预览）+ requires-env。

    Body 支持 `{url}` 或 `{zip_b64}`。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    url = (body or {}).get("url", "").strip()
    zip_b64 = (body or {}).get("zip_b64", "").strip()
    if not url and not zip_b64:
        raise HTTPException(status_code=400, detail="必须提供 url 或 zip_b64")
    if url and zip_b64:
        raise HTTPException(status_code=400, detail="url 与 zip_b64 互斥,二选一")

    try:
        if url:
            preview = await preview_from_url(url)
        else:
            preview = preview_from_zip_b64(zip_b64)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception("[skills/preview] 未知错误")
        raise HTTPException(status_code=500, detail=f"预览失败: {type(e).__name__}: {e}")

    return preview


@router.post("/install")
async def install_skill(request: Request) -> dict:
    """实际安装。优先从 preview 缓存复用,缓存失效会重新拉。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    url = (body or {}).get("url", "").strip()
    zip_b64 = (body or {}).get("zip_b64", "").strip()
    if not url and not zip_b64:
        raise HTTPException(status_code=400, detail="必须提供 url 或 zip_b64")
    if url and zip_b64:
        raise HTTPException(status_code=400, detail="url 与 zip_b64 互斥,二选一")

    try:
        if url:
            result = await install_from_url(url)
        else:
            result = await install_from_zip_b64(zip_b64)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception("[skills/install] 未知错误")
        raise HTTPException(status_code=500, detail=f"安装失败: {type(e).__name__}: {e}")

    return result


@router.delete("/{name}")
async def uninstall_skill(name: str) -> dict:
    """卸载 user 级 skill（仅删带 .install.json 的目录,绝不删项目内置）。"""
    try:
        return await uninstall(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/refresh")
async def refresh_catalog() -> dict:
    """手动重扫 .agents/skills/ + ~/.agents/skills/。"""
    import asyncio
    count = await asyncio.to_thread(refresh_skills)
    return {"count": count}


@router.post("/env-allowlist")
async def grant_env_allowlist(request: Request) -> dict:
    """增量把 keys 加入 config.toml.skills_env_allowlist（去重保序）。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    keys = (body or {}).get("keys") or []
    if not isinstance(keys, list):
        raise HTTPException(status_code=400, detail="keys 必须是 array")

    try:
        return await grant_env(keys)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
