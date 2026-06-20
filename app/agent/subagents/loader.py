"""
YAML 配置加载 + 工具名称匹配解析

负责：
- 从 configs/ 目录加载子 Agent 的 YAML 配置文件
- 解析工具名称与实际工具函数的映射关系
- 校验配置完整性
"""

import os
from pathlib import Path
from typing import Any

import yaml


# 子 Agent 配置目录（相对于本文件）
CONFIGS_DIR = Path(__file__).parent / "configs"


def load_subagent_config(config_name: str) -> dict[str, Any]:
    """
    加载单个子 Agent 的 YAML 配置

    Args:
        config_name: 配置文件名（如 "procurement_analyst.yaml"）

    Returns:
        解析后的配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置格式校验失败
    """
    config_path = CONFIGS_DIR / config_name

    if not config_path.exists():
        raise FileNotFoundError(f"子 Agent 配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 校验必要字段
    _validate_config(config, config_name)
    return config


def load_all_configs() -> dict[str, dict[str, Any]]:
    """
    加载所有子 Agent 配置

    Returns:
        配置名 → 配置字典 的映射
    """
    configs = {}
    for file in CONFIGS_DIR.glob("*.yaml"):
        configs[file.stem] = load_subagent_config(file.name)
    return configs


def _validate_config(config: dict[str, Any], name: str) -> None:
    """
    校验配置文件的必要字段

    Args:
        config: 配置字典
        name: 配置名（用于错误提示）

    Raises:
        ValueError: 缺少必要字段时抛出
    """
    required_fields = ["name", "description"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"子 Agent 配置 '{name}' 缺少必要字段: {field}")
    # tools 可选：缺省视为无工具（factory 用 cfg.get("tools") or [] 兜底）；
    # input/output 等扩展字段由 factory 按需校验。
