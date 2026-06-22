"""
Skill 安装子模块（agentskills 标准 client 安装层）。

对外接口：
- preview_from_url / preview_from_zip_b64 / preview_from_zip_bytes：装前审查
- install_from_url / install_from_zip_b64 / install_from_zip_bytes：实际安装
- uninstall：卸载（仅删带 .install.json 的 user 级 skill,绝不删项目内置）
- grant_env：增量改 config.toml.skills_env_allowlist

INSTALL_BASE = ~/.agents/skills/ 与 settings.skills_user_dir 对齐
（discovery.py 已支持 user/project 双扫 + project 覆盖 user 语义）。
"""

from agent.skills.install.env_grant import grant_env
from agent.skills.install.installer import (
    INSTALL_BASE,
    install_from_url,
    install_from_zip_b64,
    install_from_zip_bytes,
    uninstall,
)
from agent.skills.install.preview import (
    PreviewResult,
    preview_from_url,
    preview_from_zip_b64,
    preview_from_zip_bytes,
)
from agent.skills.install.source import (
    extract_zip,
    fetch_github_subdir,
    parse_github_subdir_url,
)

__all__ = [
    "INSTALL_BASE",
    "PreviewResult",
    "extract_zip",
    "fetch_github_subdir",
    "grant_env",
    "install_from_url",
    "install_from_zip_b64",
    "install_from_zip_bytes",
    "parse_github_subdir_url",
    "preview_from_url",
    "preview_from_zip_b64",
    "preview_from_zip_bytes",
    "uninstall",
]
