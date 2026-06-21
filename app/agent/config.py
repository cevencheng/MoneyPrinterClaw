"""
全局配置

集中管理所有配置项:模型参数、沙箱设置、SQLite 连接、路径、Store 配置等。

加载来源(优先级从高到低):
  1. 环境变量 `AGENT_<UPPER>`(便于 CI / Docker / 临时 override)
  2. 项目根 `config.toml`
  3. 类内默认值

路径字段(`sqlite_db_path` / `vector_db_path` / `video_tasks_dir`)若是相对路径,
统一以"项目根"为锚 resolve 成绝对路径,消费点(database / bridge / agent_runtime)
直接使用,不再依赖 CWD。
"""

from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# 项目根目录:本文件位于 <root>/app/agent/config.py → parents[2] = <root>
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_TOML = _PROJECT_ROOT / "config.toml"


class Settings(BaseSettings):
    """全局配置类,支持 TOML 文件 + 环境变量"""

    # ---- 模型配置 ----
    model_name: str = "gpt-4o"
    model_temperature: float = 0.7
    model_max_tokens: int = 16384
    openai_api_key: str = ""
    openai_base_url: str = ""

    # ---- 沙箱配置 ----
    sandbox_enabled: bool = True
    sandbox_timeout: int = 300

    # ---- 持久化路径(相对项目根)----
    sqlite_db_path: str = "storage/app.db"
    vector_db_path: str = "storage/vectors"

    # ---- 文件路径配置 ----
    skills_dir: str = "./skills"  # 相对 app/ 工作目录
    subagent_configs_dir: str = "./subagents/configs"  # 相对 app/ 工作目录

    # ---- 搜索配置 ----
    search_engine: str = "bocha"
    search_max_results: int = 5
    bocha_api_key: str = ""
    search_proxy: str = ""
    tavily_api_key: str = ""
    # ReAct 子 Agent（如 researcher）单次调用内累计工具调用上限。达到后切「整理模式」
    # 追加一轮无工具调用产出 out_field，避免硬截断丢失检索成果。详见 factory.py。
    search_react_tool_call_limit: int = 4

    # ---- Store 配置 ----
    store_backend: str = "sqlite"

    # ---- 视频创作 ----
    video_pexels_api_keys: str = ""
    video_pixabay_api_keys: str = ""
    video_voice_name: str = "zh-CN-XiaoxiaoNeural"
    video_voice_rate: float = 1.0
    video_aspect: str = "9:16"
    video_concat_mode: str = "random"
    video_clip_duration: int = 5
    video_source: str = "pexels"
    video_subtitle_enabled: bool = True
    video_font_name: str = "STHeitiMedium.ttc"
    # ASS 字幕字号（ass [V4+ Styles].Fontsize；与 PlayResY=1920 同坐标系,~3% 视高一行）。
    # 60 ≈ 1080×1920 短视频常见单行高,大致占屏宽 6-7 字。早期默认 20 是 .srt+libass force_style
    # 路径调出来的,用在 ASS 上会塌成约 1% 视高超小字。
    video_font_size: int = 60
    # ASS 字幕距离底边的垂直边距（[V4+ Styles].MarginV；PlayResY=1920 坐标系,1 单位≈1 像素）。
    # 270 让字幕底边约位于视频高度 14% 处,避开抖音/TikTok UI 安全区。
    video_subtitle_margin_v: int = 270
    # 单行字幕最大字数（ColdForge 满血版断句算法用,字数累加到此即强切）。
    # 与 video_font_size 联动:60 → 14 字一行约占宽 80%;调大字号要相应调小。
    video_subtitle_max_chars: int = 14
    # \kf 卡拉OK 已唱字色（黄,与未唱白色形成滑过对比;PrimaryColour 仍硬编码 #FFFFFF）。
    # 抖音/TikTok 主流配色锁死,视觉一致性优先。要换风格再开 knob。
    video_subtitle_secondary_color: str = "#FFFF00"
    video_tasks_dir: str = "storage/video_tasks"  # 相对项目根

    # ---- ASR 字幕（faster-whisper）----
    # 字幕生成已与 TTS 解耦：TTS 退化为纯 text→audio,字幕由 ASR 从音频反向识别（词级时间戳 → ASS）。
    # 默认 large-v3-turbo（~1.5GB）：OpenAI 2024 蒸馏架构,~809M 参数,比 large-v3 快约 8x,
    #   全多语言保留（中文 OK）,词级时间戳支持。CPU/int8 跑 30s 音频约 2-4s,中文准确率接近 large-v3。
    # ⚠️ 千万不要换 distil-whisper-large-v3：distil 系列论文明确英文 only,跑中文会吐空或翻成英文。
    # 备选：medium（769MB,~3-5s,~97%）/ small（244MB,~1s,~94%）/ large-v3（3GB,~8-15s,~98%+）。
    video_asr_model_size: str = "large-v3-turbo"
    video_asr_device: str = "cpu"  # cpu / cuda（cuda 需手动确认 ctranslate2 支持本机 CUDA 版本）
    video_asr_compute_type: str = "int8"  # cpu→int8;cuda→float16
    video_asr_language: str = "zh"  # 锁死中文识别,提升响应率(避免 Whisper 自动检测把中文短音频判成 yue/en)

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        toml_file=str(_CONFIG_TOML),
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        """优先级:init > env(AGENT_*) > config.toml > defaults"""
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    def model_post_init(self, __context) -> None:
        """把存储相关的相对路径锚到项目根 → 绝对路径,消费点不再依赖 CWD"""
        for field in ("sqlite_db_path", "vector_db_path", "video_tasks_dir"):
            value = getattr(self, field)
            if value and not Path(value).is_absolute():
                setattr(self, field, str((_PROJECT_ROOT / value).resolve()))


# 全局单例
settings = Settings()
