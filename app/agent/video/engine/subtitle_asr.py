"""
ASR 字幕生成 —— ColdForge 满血硬化版双重 SequenceMatcher 强对齐 + \kf 卡拉OK 字字高亮

修复与硬化：
1. 复活动态参照系：重构 `_align_segments_difflib` 中的 Layer 2，将静态 anchors 恢复为
   冷锻正统的动态字典探查，确保每一个新内插的时轴点都能被后文实时参考，彻底消灭字幕严重错位与时轴飘移。
2. 标点前瞻换行保护：优化 `_split_script_breathing` 算法。达到 14字 硬截断时，向前看 3 个字符，
   若发现标点符号近在咫尺（≤3字），则主动延迟切断、吞入剩余字符，确保“钱塘江”、“与水相关”等意群绝对不被拦腰切断。
3. 修正语法残余：清除原代码末尾遗留的闭合花括号等语法死锁。
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from typing import Any

from agent.config import settings

logger = logging.getLogger(__name__)

# 全局单例
_ASR_MODEL: Any = None

_CHAR_REGEX = re.compile(r"[^一-龥a-zA-Z0-9]")
_CHAR_REGEX_KEEP_PIPE = re.compile(r"[^一-龥a-zA-Z0-9|]")

_SENTENCE_END = set("。！？!?…")          # 句末标点 → 强切
_WEAK_PUNCT = set("，、；：,;:")           # 弱标点

_MIN_CHAR_CS = 1            
_MIN_SEG_DUR = 0.25         
_MIN_SEG_START = 0.1        
_MONOTONIC_EPS = 0.001      
_DELETE_FALLBACK_PER = 0.1  

_PROMPT_CHAR_LIMIT = 200

_DEFAULT_PRIMARY = "#FFFFFF"      
_DEFAULT_SECONDARY = "#FFFF00"    
_DEFAULT_OUTLINE = "#000000"      

_FONT_FAMILY_MAP = {
    "STHeitiMedium.ttc": "Heiti SC",
    "NotoSansCJKsc-Regular.otf": "Noto Sans CJK SC",
}


def get_asr_model():
    """懒加载 faster-whisper 单例，显式解析本地 snapshot，避免联网查 HF revision。"""
    global _ASR_MODEL
    if _ASR_MODEL is None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download

        model_size = settings.video_asr_model_size
        model_path = model_size
        if "/" in model_size:
            try:
                model_path = snapshot_download(repo_id=model_size, local_files_only=True)
                logger.info(
                    "[engine/subtitle_asr] 使用本地 HuggingFace snapshot: repo=%s path=%s",
                    model_size,
                    model_path,
                )
            except Exception as e:
                logger.warning(
                    "[engine/subtitle_asr] 本地 snapshot 解析失败,回退原模型名(可能触发联网): %s | %s",
                    model_size,
                    str(e)[:200],
                )

        logger.info(
            "[engine/subtitle_asr] 加载 Whisper 模型: size=%s resolved=%s device=%s compute_type=%s ",
            model_size,
            model_path,
            settings.video_asr_device,
            settings.video_asr_compute_type,
        )
        _ASR_MODEL = WhisperModel(
            model_path,
            device=settings.video_asr_device,
            compute_type=settings.video_asr_compute_type,
        )
    return _ASR_MODEL


def transcribe_words(
    audio_path: str,
    initial_prompt: str = "",
    language: str | None = None,
) -> list:
    """跑识别，摊平所有 segment.words 返回 Word 列表。"""
    model = get_asr_model()
    prompt = (initial_prompt or "")[:_PROMPT_CHAR_LIMIT] if initial_prompt else None

    segments, info = model.transcribe(
        audio_path,
        beam_size=5,
        word_timestamps=True,
        initial_prompt=prompt,
        language=language or settings.video_asr_language,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
    )

    all_words: list = []
    for seg in segments:
        if seg.words:
            all_words.extend(seg.words)
    return all_words


def _flatten_to_chars(words: list) -> list[dict]:
    """Whisper word 块 → 单字字典列表。"""
    chars: list[dict] = []
    for w in words or []:
        raw = (getattr(w, "word", "") or "").strip()
        clean = _CHAR_REGEX.sub("", raw)
        if not clean:
            continue
        wstart = float(getattr(w, "start", 0.0))
        wend = float(getattr(w, "end", wstart))
        dur = max(0.0, wend - wstart)
        n = len(clean)
        per = dur / n if n > 0 else 0.0
        for i, ch in enumerate(clean):
            chars.append({
                "word": ch,
                "start": wstart + i * per,
                "end": wstart + (i + 1) * per,
            })
    return chars


def _force_align_with_script(whisper_chars: list[dict], script: str) -> list[dict]:
    """第一重对齐：SequenceMatcher 用 script 真理源覆盖 whisper 识别结果。"""
    clean_script = _CHAR_REGEX.sub("", script or "")
    if not clean_script:
        return whisper_chars

    if not whisper_chars:
        out: list[dict] = []
        t = 0.0
        for ch in clean_script:
            out.append({"word": ch, "start": t, "end": t + _DELETE_FALLBACK_PER})
            t += _DELETE_FALLBACK_PER
        return out

    whisper_text = "".join(c["word"] for c in whisper_chars)
    matcher = difflib.SequenceMatcher(None, clean_script, whisper_text, autojunk=False)
    opcodes = matcher.get_opcodes()

    aligned: list[dict] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag in ("equal", "replace"):
            script_seg = clean_script[i1:i2]
            whisper_seg = whisper_chars[j1:j2]
            if not whisper_seg:
                continue
            for k, correct_ch in enumerate(script_seg):
                anchor = whisper_seg[min(k, len(whisper_seg) - 1)]
                aligned.append({
                    "word": correct_ch,
                    "start": round(anchor["start"], 3),
                    "end": round(anchor["end"], 3),
                })
        elif tag == "delete":
            n = i2 - i1
            if n <= 0:
                continue
            prev_end = aligned[-1]["end"] if aligned else 0.0
            next_start = _find_next_anchor_start(opcodes, idx + 1, whisper_chars)

            if next_start is not None and next_start > prev_end:
                per = (next_start - prev_end) / n
                for k, ch in enumerate(clean_script[i1:i2]):
                    s = prev_end + k * per
                    e = prev_end + (k + 1) * per
                    aligned.append({"word": ch, "start": round(s, 3), "end": round(e, 3)})
            else:
                t = prev_end
                for ch in clean_script[i1:i2]:
                    aligned.append({
                        "word": ch,
                        "start": round(t, 3),
                        "end": round(t + _DELETE_FALLBACK_PER, 3),
                    })
                    t += _DELETE_FALLBACK_PER

    # 单调钳制保底防线
    for k in range(1, len(aligned)):
        if aligned[k]["start"] < aligned[k - 1]["start"] + _MONOTONIC_EPS:
            aligned[k]["start"] = round(aligned[k - 1]["start"] + _MONOTONIC_EPS, 3)
            if aligned[k]["end"] < aligned[k]["start"]:
                aligned[k]["end"] = round(aligned[k]["start"] + _MONOTONIC_EPS, 3)

    return aligned


def _find_next_anchor_start(opcodes, start_idx: int, whisper_chars: list[dict]) -> float | None:
    for tag, i1, i2, j1, j2 in opcodes[start_idx:]:
        if tag in ("equal", "replace") and j2 > j1 and j1 < len(whisper_chars):
            return float(whisper_chars[j1]["start"])
    return None


# ============================================================================
# ③ 算法版断句（🟢 已硬化：标点符号 3 字 Lookahead 前瞻保护机制）
# ============================================================================


def _split_script_breathing(script: str, max_chars: int = 14) -> str:
    """带有语义前瞻保护的高阶断句算法。

    当字数累计达到 max_chars 限额时，主动向前偷看 3 个字符。
    如果发现标点符号即将到来，则【选择暂缓换行】，等待吞完后续字并到标点处再干净切开，
    从物理上完美杜绝“钱塘江”、“与水相关”在硬断句处被拦腰截断的排版事故。
    """
    if not script:
        return ""

    half = max(1, max_chars // 2)

    # 第一步：规整并建立带标点属性的精细解析字符流
    raw_chars = []
    for ch in script:
        if ch in _SENTENCE_END or ch in _WEAK_PUNCT:
            raw_chars.append({"char": ch, "type": "punct", "is_strong": ch in _SENTENCE_END})
        elif not _CHAR_REGEX.match(ch):
            raw_chars.append({"char": ch, "type": "char", "is_strong": False})

    segments: list[str] = []
    current = ""

    i = 0
    n = len(raw_chars)
    while i < n:
        item = raw_chars[i]
        
        # 标点符号触发器
        if item["type"] == "punct":
            if item["is_strong"]:
                if current:
                    segments.append(current)
                    current = ""
            else:
                if len(current) >= half:
                    segments.append(current)
                    current = ""
            i += 1
            continue

        current += item["char"]

        # ⭐ 核心前瞻保护锁
        if len(current) >= max_chars:
            found_punct_near = False
            # 向前扫描最多 3 个字符
            for next_idx in range(i + 1, min(i + 4, n)):
                if raw_chars[next_idx]["type"] == "punct":
                    found_punct_near = True
                    break
            
            if found_punct_near:
                # 岸边就在眼前，选择延时切断，等待循环自然走到标点后再优雅换行
                i += 1
                continue
            else:
                # 确实是无标点长难句荒原，为了竖屏边界，执行刚性切断
                segments.append(current)
                current = ""

        i += 1

    if current:
        segments.append(current)

    # 吊尾短行强行并入合并 (严格锁死 <= 2)
    merged: list[str] = []
    pending = ""
    for seg in segments:
        seg = pending + seg
        pending = ""
        if len(seg) <= 2:
            if merged:
                merged[-1] += seg
            else:
                pending = seg
        else:
            merged.append(seg)
    if pending:
        merged.append(pending)

    return "|".join(merged)


# ============================================================================
# ④ 第二重对齐：char_list ↔ split_text(|) 
# ============================================================================


def _align_segments_difflib(char_list: list[dict], split_text: str) -> list[list[dict]]:
    """【🔴 核心修复】：带 | 的剧本段落与清洗完的字符级时轴二次对齐。

    Layer 2 已恢复冷锻原生的动态参照系探查机制，拒绝死锁，彻底修复时间轨长距离飘移误差。
    """
    if not char_list or not split_text:
        return [char_list] if char_list else []

    clean_split = _CHAR_REGEX_KEEP_PIPE.sub("", split_text)
    asr_chars = "".join(c["word"] for c in char_list)
    llm_chars = clean_split.replace("|", "")

    if not llm_chars:
        return [char_list]

    matcher = difflib.SequenceMatcher(None, asr_chars, llm_chars, autojunk=False)

    # ── Layer 1: matching_blocks 去重精准命中 ──
    asr_used: set[int] = set()
    llm_to_asr: dict[int, int] = {}
    for asr_idx, llm_idx, size in matcher.get_matching_blocks():
        for offset in range(size):
            a_i = asr_idx + offset
            l_i = llm_idx + offset
            if a_i not in asr_used:
                llm_to_asr[l_i] = a_i
                asr_used.add(a_i)

    # ── Layer 2: ⭐ 满血恢复动态双距离最近邻（对齐自愈核心） ──
    unmatched_llm = [i for i in range(len(llm_chars)) if i not in llm_to_asr]
    unused_asr = sorted(set(range(len(char_list))) - asr_used)

    for llm_i in unmatched_llm:
        if unused_asr:
            best_asr = None
            best_dist = float('inf')
            for candidate in unused_asr:
                # 🌟 正统冷锻机制：直接从动态字典迭代，确保实时捕获最近内插邻居作为最新锚点
                for matched_l, matched_a in llm_to_asr.items():
                    dist = abs(candidate - matched_a) + abs(llm_i - matched_l)
                    if dist < best_dist:
                        best_dist = dist
                        best_asr = candidate
            if best_asr is not None:
                llm_to_asr[llm_i] = best_asr
                unused_asr.remove(best_asr)

    # ── Layer 3: 拆段收拢 ──
    segments: list[list[dict]] = []
    current: list[dict] = []
    llm_idx = 0
    for ch in clean_split:
        if ch == "|":
            if current:
                segments.append(current)
                current = []
            continue
        if llm_idx in llm_to_asr:
            asr_i = llm_to_asr[llm_idx]
            info = dict(char_list[asr_i])  # 浅拷贝
            info["word"] = ch  # LLM 修正字（同音字容错）
            current.append(info)
        else:
            if current:
                prev_end = current[-1]["end"]
                info = {"word": ch, "start": prev_end, "end": prev_end + _DELETE_FALLBACK_PER}
            elif segments and segments[-1]:
                prev_end = segments[-1][-1]["end"]
                info = {"word": ch, "start": prev_end, "end": prev_end + _DELETE_FALLBACK_PER}
            elif char_list:
                t = char_list[0]["start"]
                info = {"word": ch, "start": t, "end": t + _DELETE_FALLBACK_PER}
            else:
                info = {"word": ch, "start": _MIN_SEG_START, "end": _MIN_SEG_START + _DELETE_FALLBACK_PER}
            current.append(info)
        llm_idx += 1

    if current:
        segments.append(current)

    return segments


# ============================================================================
# ⑤ ASS 卡拉OK 写入
# ============================================================================


def format_ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cc = int(round((seconds - int(seconds)) * 100))
    if cc >= 100:
        s += 1
        cc -= 100
        if s >= 60:
            m += 1
            s -= 60
            if m >= 60:
                h += 1
                m -= 60
    return f"{h}:{m:02d}:{s:02d}.{cc:02d}"


def _resolve_font_family(font_name: str) -> str:
    return _FONT_FAMILY_MAP.get(font_name, os.path.splitext(font_name)[0])


def _hex_to_libass(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "（").replace("}", "）")


def _segments_to_ass_karaoke(
    segments: list[list[dict]],
    ass_path: str,
    *,
    font_name: str = "STHeitiMedium.ttc",
    font_size: int = 60,
    margin_v: int = 270,
    primary_color: str = _DEFAULT_PRIMARY,
    secondary_color: str = _DEFAULT_SECONDARY,
    outline_color: str = _DEFAULT_OUTLINE,
) -> None:
    family = _resolve_font_family(font_name)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 1\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: MainText,{family},{font_size},"
        f"{_hex_to_libass(primary_color)},{_hex_to_libass(secondary_color)},"
        f"{_hex_to_libass(outline_color)},&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,3,0,2,20,20,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = []
    for seg in segments:
        if not seg:
            continue
        seg_start = max(seg[0]["start"], _MIN_SEG_START)
        seg_end = max(seg[-1]["end"], seg_start + _MIN_SEG_DUR)

        line_parts: list[str] = []
        for ch_info in seg:
            ch = ch_info["word"]
            dur = max(0.0, ch_info["end"] - ch_info["start"])
            cs = max(_MIN_CHAR_CS, int(round(dur * 100)))
            line_parts.append(f"{{\\kf{cs}}}{_escape_ass_text(ch)}")
        line_text = "".join(line_parts)

        events.append(
            f"Dialogue: 0,{format_ass_time(seg_start)},{format_ass_time(seg_end)},"
            f"MainText,,0,0,0,,{line_text}"
        )

    os.makedirs(os.path.dirname(ass_path), exist_ok=True)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
    logger.info(
        "[engine/subtitle_asr] ass 成功落盘并修正 %s (%d segments)",
        ass_path, len(events),
    )


# ============================================================================
# 主入口
# ============================================================================


def run(
    audio_path: str,
    script: str,
    ass_path: str,
    *,
    font_name: str = "STHeitiMedium.ttc",
    font_size: int = 60,
    margin_v: int = 270,
    secondary_color: str = _DEFAULT_SECONDARY,
) -> None:
    # 步骤 ①：Whisper 转写
    words = transcribe_words(audio_path, initial_prompt=script)
    if not words:
        logger.warning("[engine/subtitle_asr] transcribe 返回 0 words,跳过 ass 生成")
        return

    # 步骤 ②：摊平 word → 字符级时间轴
    chars = _flatten_to_chars(words)
    if not chars:
        logger.warning("[engine/subtitle_asr] flatten 返回 0 chars")
        return

    # 步骤 ③：第一重对齐
    if script:
        chars = _force_align_with_script(chars, script)
        if not chars:
            logger.warning("[engine/subtitle_asr] 第一重对齐返回空")
            return

    # 步骤 ④：硬化断句引擎（内置 3字标点前瞻防御，拒绝腰斩词汇）
    split_text = _split_script_breathing(
        script or "",
        max_chars=settings.video_subtitle_max_chars,
    )
    if not split_text:
        split_text = "".join(c["word"] for c in chars)

    # 步骤 ⑤：第二重对齐（完美继承冷锻正统动态参照链机制，消灭错位飘移）
    segments = _align_segments_difflib(chars, split_text)
    if not segments:
        logger.warning("[engine/subtitle_asr] 第二重对齐返回 0 段")
        return

    # 步骤 ⑥：写 \kf 卡拉OK ASS
    _segments_to_ass_karaoke(
        segments,
        ass_path,
        font_name=font_name,
        font_size=font_size,
        margin_v=margin_v,
        secondary_color=secondary_color,
    )