"""
Skills CLI 入口

用法：
    python -m agent.skills install <github-url> [--token TOKEN]
    python -m agent.skills install <local.zip>
    python -m agent.skills list
    python -m agent.skills uninstall <name>
    python -m agent.skills refresh

CLI 安装的 skill 与 webui 安装等价：同一 INSTALL_BASE（~/.agents/skills/）+
同一 .install.json 元数据 + 同一 refresh_skills 缓存。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agent.skills.discovery import get_catalog, refresh_skills
from agent.skills.install import (
    install_from_url,
    install_from_zip_bytes,
    uninstall,
)
from agent.skills.install.installer import INSTALL_BASE


def _print_err(msg: str) -> None:
    """简单红色输出（Windows cmd 通常不支持 ANSI,但不影响可读性）。"""
    sys.stderr.write(f"[ERROR] {msg}\n")


def _print_ok(msg: str) -> None:
    sys.stdout.write(f"[OK] {msg}\n")


async def _cmd_install(source: str, token: str | None) -> int:
    """安装 skill。source 是 GitHub URL 或本地 .zip 路径。"""
    if source.startswith(("http://", "https://")):
        try:
            result = await install_from_url(source, token=token)
        except Exception as e:
            _print_err(f"安装失败（{type(e).__name__}）: {e}")
            return 1
    else:
        # 本地 zip
        path = Path(source)
        if not path.is_file():
            _print_err(f"找不到文件: {source}")
            return 1
        if not path.suffix.lower() == ".zip":
            _print_err(f"仅支持 .zip 后缀的本地文件: {source}")
            return 1
        try:
            zip_bytes = path.read_bytes()
            result = await install_from_zip_bytes(zip_bytes)
        except Exception as e:
            _print_err(f"安装失败（{type(e).__name__}）: {e}")
            return 1

    _print_ok(f"已安装 skill: {result['name']}")
    _print_ok(f"  路径: {result['path']}")
    _print_ok(f"  来源: {result['source_type']} ({result['source_ref'][:50]}...)")
    if result["env_required"]:
        _print_ok(f"  声明的环境变量: {result['env_required']}")
        if result["env_missing"]:
            _print_err(
                f"  ⚠️  以下环境变量未在 skills_env_allowlist 中,脚本无法访问: {result['env_missing']}"
            )
            _print_err(
                f"     在 config.toml 中 skills_env_allowlist = [..., \"{result['env_missing'][0]}\"] 显式开洞"
            )
    return 0


def _cmd_list() -> int:
    """列出已安装 skill。"""
    refresh_skills()
    catalog = get_catalog()
    if not catalog:
        _print_ok("当前 catalog 为空（未安装任何 skill）")
        return 0
    sys.stdout.write(f"{'NAME':<24} {'SOURCE':<8} {'VERSION':<10} DESCRIPTION\n")
    sys.stdout.write("-" * 80 + "\n")
    for m in catalog:
        # 判 source：location 在 INSTALL_BASE 下 → user
        try:
            Path(m.location).resolve().relative_to(INSTALL_BASE)
            source = "user"
        except ValueError:
            source = "project"
        desc = m.description[:50]
        if len(m.description) > 50:
            desc += "..."
        sys.stdout.write(f"{m.name:<24} {source:<8} {m.version or '-':<10} {desc}\n")
    return 0


async def _cmd_uninstall(name: str) -> int:
    """卸载 user 级 skill。"""
    try:
        await uninstall(name)
        _print_ok(f"已卸载 skill: {name}")
        return 0
    except FileNotFoundError as e:
        _print_err(str(e))
        return 1
    except PermissionError as e:
        _print_err(str(e))
        return 2
    except Exception as e:
        _print_err(f"卸载失败（{type(e).__name__}）: {e}")
        return 1


def _cmd_refresh() -> int:
    count = refresh_skills()
    _print_ok(f"已刷新 catalog,共 {count} 个 skill")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.skills",
        description="Agent Skills 管理 CLI (与 webui /settings?tab=skills 等价)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="安装 skill（GitHub URL 或本地 .zip）")
    p_install.add_argument("source", help="GitHub 子目录 URL 或本地 .zip 路径")
    p_install.add_argument("--token", help="GitHub Personal Access Token（可选,提升 API 速率）")

    sub.add_parser("list", help="列出已安装 skill")

    p_uninstall = sub.add_parser("uninstall", help="卸载 user 级 skill")
    p_uninstall.add_argument("name", help="skill 名（GET /api/skills 看到的 name）")

    sub.add_parser("refresh", help="手动重扫 catalog")

    args = parser.parse_args(argv)

    if args.cmd == "install":
        return asyncio.run(_cmd_install(args.source, args.token))
    elif args.cmd == "list":
        return _cmd_list()
    elif args.cmd == "uninstall":
        return asyncio.run(_cmd_uninstall(args.name))
    elif args.cmd == "refresh":
        return _cmd_refresh()
    return 1


if __name__ == "__main__":
    sys.exit(main())
