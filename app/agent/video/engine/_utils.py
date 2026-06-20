"""
视频引擎工具函数:资源路径解析 + ffmpeg binary 查找。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# engine/_utils.py → <root>/resources（parents[4]: engine→video→agent→app→root）
_RESOURCES_DIR = Path(__file__).resolve().parents[4] / "resources"


def font_dir() -> str:
    """字体目录(resources/fonts/)。"""
    d = _RESOURCES_DIR / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def song_dir() -> str:
    """BGM 音乐目录(resources/songs/)。"""
    d = _RESOURCES_DIR / "songs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def get_ffmpeg_binary() -> str:
    """查找 ffmpeg 可执行文件:env > shutil.which > imageio-ffmpeg > fallback。"""
    # 1. 环境变量
    exe = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if exe and os.path.isfile(exe):
        return exe
    # 2. PATH 查找
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 3. imageio-ffmpeg 包内 bundled
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"
