"""
视频引擎 schema:aspect / concat mode / params。

替代 MPT app.models.schema 的 VideoAspect / VideoConcatMode / VideoParams,
精简为实际使用的字段(砍掉 MPT 25+ 字段中不用的)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class VideoAspect(str, Enum):
    """视频画幅 → 目标分辨率。"""

    landscape = "16:9"   # 1920×1080
    portrait = "9:16"    # 1080×1920
    square = "1:1"       # 1080×1080

    def to_resolution(self) -> tuple[int, int]:
        return {
            VideoAspect.landscape.value: (1920, 1080),
            VideoAspect.portrait.value: (1080, 1920),
            VideoAspect.square.value: (1080, 1080),
        }.get(self.value, (1080, 1920))


class VideoConcatMode(str, Enum):
    """素材拼接模式。"""

    random = "random"           # 打乱顺序
    sequential = "sequential"   # 按搜索顺序


@dataclass
class VideoParams:
    """generate_video 的参数(精简版,只保留实际使用字段)。"""

    video_subject: str = ""
    subtitle_position: str = "bottom"   # bottom / top / center
    font_name: str = "NotoSansCJKsc-Regular.otf"
    voice_volume: float = 1.0
    font_size: int = 20   # libass FontSize（1080×1920 竖屏实测：占宽~50%，不遮挡画面）
    stroke_color: str = "#000000"
    stroke_width: float = 1.5
    text_color: str = "#FFFFFF"
    bgm_type: str = "random"            # "random"=随机 BGM;""=无 BGM
    bgm_volume: float = 0.2
