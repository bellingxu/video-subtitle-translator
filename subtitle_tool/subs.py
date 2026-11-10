"""字幕数据处理：把识别结果整理成一条条字幕、自动换行、生成 ASS / SRT。

一条字幕（cue）就是一个字典：{"start": 秒, "end": 秒, "text": 原文}
译文单独放在一个等长的列表里。
"""
from __future__ import annotations

import math
import re
import unicodedata

# 字体：macOS 自带、ffmpeg(libass) 能直接读到的。苹方（PingFang）在新系统里放在受保护的位置，libass 读不了。
ZH_FONT = "Hiragino Sans GB"  # 冬青黑体简体中文
SRC_FONT = "Hiragino Sans"  # 原文（日文 / 英文）

CJK_LANGS = {"ja", "zh", "ko", "yue"}

# Whisper 在没人说话的片段偶尔会“幻听”出的套话，电视剧台词里不会真的出现
_HALLUCINATIONS = [
    re.compile(p, re.I)
    for p in (
        r"ご視聴ありがとうございま", r"チャンネル登録", r"最後までご視聴", r"高評価.*お願い",
        r"^(字幕|翻訳|提供)\s*[:：]", r"字幕\s*by", r"thanks?\s+for\s+watching", r"subtitles?\s+by",
        r"amara\.org", r"please\s+subscribe", r"字幕由.*提供", r"請不吝點贊", r"请不吝点赞",
    )
]


def is_cjk(lang: str) -> bool:
    return (lang or "").lower() in CJK_LANGS


# ───────────────────────── 语音识别结果 → 字幕 ─────────────────────────

def _split_words(words: list[dict], limit: int, max_dur: float) -> list[list[dict]]:
    """太长的一段话，按词的时间戳在最合适的位置拆开（优先在标点或停顿处，其次靠近中间）。"""
    total = sum(len(w["word"].strip()) for w in words)
    dur = words[-1]["end"] - words[0]["start"]
    if len(words) < 2 or (total <= limit and (dur <= max_dur or total <= limit * 0.5)):
        return [words]
    best_score, best_i, acc = None, 1, 0
    for i in range(1, len(words)):
        prev = words[i - 1]["word"].strip()
        acc += len(prev)
        score = abs(acc - total / 2) / max(total, 1) * 2.0  # 0（正中间）～ 1（最边上）
        if prev[-1:] in "。！？!?.…":
            score -= 0.6
        elif prev[-1:] in "、，,;；:：":
            score -= 0.4
        score -= min(max(words[i]["start"] - words[i - 1]["end"], 0.0), 1.0) * 0.5
        if best_score is None or score < best_score:
            best_score, best_i = score, i
    return _split_words(words[:best_i], limit, max_dur) + _split_words(words[best_i:], limit, max_dur)


def cues_from_whisper(segments: list[dict], lang: str) -> list[dict]:
    cjk = is_cjk(lang)
    limit = 45 if cjk else 110
    cues: list[dict] = []
    for seg in segments:
        text = _clean(seg.get("text"))
        start, end = float(seg.get("start") or 0), float(seg.get("end") or 0)
        if not text or end - start < 0.02:
            continue
        if any(p.search(text) for p in _HALLUCINATIONS):
            continue
        words = [w for w in (seg.get("words") or []) if str(w.get("word") or "").strip()]
        pieces = _split_words(words, limit, 9.0) if words else [[]]
        if len(pieces) <= 1:
            cues.append({"start": start, "end": end, "text": text})
            continue
        for piece in pieces:
            piece_text = _clean("".join(w["word"] for w in piece))
            if piece_text:
                cues.append({"start": float(piece[0]["start"]), "end": float(piece[-1]["end"]), "text": piece_text})
    cues.sort(key=lambda c: (c["start"], c["end"]))

    # 连续重复的同一句（比如一连串的“はい”）合并成一条
    merged: list[dict] = []
    for cue in cues:
        if merged and cue["text"] == merged[-1]["text"] and cue["start"] - merged[-1]["end"] < 1.5:
            merged[-1]["end"] = max(merged[-1]["end"], cue["end"])
        else:
            merged.append(cue)
    return polish_timing(merged, cjk)


def polish_timing(cues: list[dict], cjk: bool) -> list[dict]:
    """让每条字幕的显示时间合理：不要太短来不及看，也不要在没人说话时一直挂着，前后不重叠。"""
    out = []
    for i, cue in enumerate(cues):
        start, end = cue["start"], cue["end"]
        n = len(cue["text"])
        cap = min(10.0, 2.0 + (0.30 if cjk else 0.09) * n)  # 按字数给的最长显示时间
        end = min(end, start + cap)
        end = max(end + 0.25, start + 0.8)  # 多停留一小会儿，且至少显示 0.8 秒
        if i + 1 < len(cues):
            end = min(end, cues[i + 1]["start"] - 0.04)
        if end - start < 0.1:
            end = start + 0.1
        out.append({"start": round(start, 3), "end": round(end, 3), "text": cue["text"]})
    return out


# ───────────────────────── 现成字幕（SRT）→ 字幕 ─────────────────────────

_SRT_TIME = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
)
_TAGS = re.compile(r"<[^>]{0,80}>|\{\\[^}]*\}")


def cues_from_srt(srt: str, lang: str = "") -> list[dict]:
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", srt.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for idx, line in enumerate(lines[:2]):
            m = _SRT_TIME.search(line)
            if not m:
                continue
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")) / 1000
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000
            parts = [_clean(_TAGS.sub("", ln)) for ln in lines[idx + 1:]]
            parts = [p for p in parts if p]
            if not parts or end <= start:
                break
            joined = "".join(parts) if _mostly_cjk("".join(parts)) else " ".join(parts)
            cues.append({"start": round(start, 3), "end": round(end, 3), "text": joined})
            break
    cues.sort(key=lambda c: (c["start"], c["end"]))
    # 完全相同的相邻重复（有些 ASS 转出来的字幕会带特效重复行）去掉
    dedup: list[dict] = []
    for cue in cues:
        if dedup and cue["text"] == dedup[-1]["text"] and cue["start"] < dedup[-1]["end"] + 0.05:
            dedup[-1]["end"] = max(dedup[-1]["end"], cue["end"])
        else:
            dedup.append(cue)
    return dedup


def _mostly_cjk(text: str) -> bool:
    wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    return wide * 2 >= max(1, len(text.replace(" ", "")))


def guess_language(cues: list[dict]) -> str:
    """现成字幕没有语言标签时，粗略判断是日语、中文还是别的。"""
    sample = "".join(c["text"] for c in cues[:200])
    if not sample:
        return ""
    kana = sum(1 for ch in sample if "぀" <= ch <= "ヿ")
    han = sum(1 for ch in sample if "一" <= ch <= "鿿")
    hangul = sum(1 for ch in sample if "가" <= ch <= "힣")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    if kana > len(sample) * 0.08:
        return "ja"
    if hangul > len(sample) * 0.2:
        return "ko"
    if han > len(sample) * 0.3:
        return "zh"
    if latin > len(sample) * 0.5:
        return "en"
    return ""


# ───────────────────────── 文字宽度与换行 ─────────────────────────
# 下面的宽度是在这台 Mac 上用 ffmpeg 实际渲染量出来的（单位：ASS 字号的倍数）

def _char_width(ch: str) -> float:
    if ch == " ":
        return 0.28
    if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
        return 0.81
    if ch.isdigit():
        return 0.58
    if ch.isupper():
        return 0.72
    if ch in "iljtf.,:;!'|()[]":
        return 0.32
    return 0.55


def text_width(text: str) -> float:
    return sum(_char_width(ch) for ch in text)


_NO_LINE_START = set("，。！？、；：）」』】》〉”’…—～~·％%,.!?;:)]}ー々ゝゞっゃゅょぁぃぅぇぉッャュョァィゥェォ")
_NO_LINE_END = set("（「『【《〈“‘([{")
_GOOD_BREAK_AFTER = set("，。！？、；：,.!?;:…")


def _ascii_word_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "'-_")


def _best_break(text: str, target: float, max_units: float) -> int:
    """在 text 里找一个最接近 target 宽度、又不违反排版禁则的断行位置。"""
    best, best_cost, fallback, width = None, None, None, 0.0
    for i in range(1, len(text)):
        width += _char_width(text[i - 1])
        if width <= max_units:
            fallback = i
        prev, nxt = text[i - 1], text[i]
        if nxt in _NO_LINE_START or prev in _NO_LINE_END:
            continue
        if _ascii_word_char(prev) and _ascii_word_char(nxt):
            continue  # 不把英文单词、数字从中间断开
        cost = abs(width - target)
        if prev in _GOOD_BREAK_AFTER or prev == " " or nxt == " ":
            cost -= 3.2  # 标点、空格后面断行更自然（宁可两行不太一样长，也不把词从中间断开）
        if width > max_units:
            cost += (width - max_units) * 4
        if best_cost is None or cost < best_cost:
            best, best_cost = i, cost
    return best or fallback or max(1, len(text) // 2)


def wrap_text(text: str, max_units: float) -> list[str]:
    """一行放不下时，拆成长度尽量均匀的几行。"""
    text = _clean(text)
    total = text_width(text)
    if total <= max_units or len(text) < 2:
        return [text] if text else []
    n = min(4, math.ceil(total / max_units))
    lines, rest = [], text
    for k in range(n, 1, -1):
        if len(rest) < 2:
            break
        cut = _best_break(rest, text_width(rest) / k, max_units)
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    lines.append(rest)
    return [ln for ln in lines if ln]


def tidy_zh(text: str) -> str:
    """字幕惯例：不保留换行，句末不要句号。"""
    text = _clean(text)
    while text and text[-1] in "。，、":
        text = text[:-1].rstrip()
    return text


# ───────────────────────── ASS（带样式的字幕，用来压进画面）─────────────────────────

def style_metrics(play_w: int, play_h: int, style: dict) -> dict:
    ref = play_h if play_w >= play_h else play_w * 0.75  # 竖屏视频按宽度算，免得字太大
    size = max(8.0, round(ref * float(style.get("font_scale", 6.5)) / 100, 1))
    src_size = max(7.0, round(size * 0.66, 1))
    margin_lr = round(play_w * 0.04)
    usable = play_w - 2 * margin_lr
    return {
        "size": size,
        "src_size": src_size,
        "outline": round(max(1.0, size * 0.065), 2),
        "shadow": round(size * 0.03, 2),
        "src_outline": round(max(0.9, src_size * 0.07), 2),
        "margin_lr": margin_lr,
        "margin_v": round(play_h * float(style.get("margin", 5.0)) / 100),
        "align": {"bottom": 2, "middle": 5, "top": 8}.get(style.get("position"), 2),
        "max_units": usable / size,
        "src_max_units": usable / src_size,
    }


def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF️‍]")  # 字幕渲染器画不出彩色表情符号，会变成方框


def _clean(text: str) -> str:
    return " ".join(_EMOJI.sub("", str(text or "")).replace("　", " ").split())


def _ass_escape(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")


def _ass_header(play_w: int, play_h: int, m: dict) -> str:
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
           "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding")
    common = f"{m['align']},{m['margin_lr']},{m['margin_lr']},{m['margin_v']},1"
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_w}",
        f"PlayResY: {play_h}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        fmt,
        f"Style: zh,{ZH_FONT},{m['size']},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,"
        f"-1,0,0,0,100,100,0,0,1,{m['outline']},{m['shadow']},{common}",
        f"Style: src,{SRC_FONT},{m['src_size']},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,"
        f"0,0,0,0,100,100,0,0,1,{m['src_outline']},{m['shadow']},{common}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "",
    ])


def _event_text(zh: str, src: str, m: dict, bilingual: bool) -> str:
    lines = [_ass_escape(ln) for ln in wrap_text(tidy_zh(zh), m["max_units"])]
    text = "\\N".join(lines)
    if bilingual and src.strip():
        src_lines = [_ass_escape(ln) for ln in wrap_text(src, m["src_max_units"])]
        if src_lines:
            text += ("\\N" if text else "") + "{\\rsrc}" + "\\N".join(src_lines)
    return text


def build_ass(cues: list[dict], zh_lines: list[str], play_w: int, play_h: int,
              style: dict, bilingual: bool) -> str:
    m = style_metrics(play_w, play_h, style)
    events = []
    for cue, zh in zip(cues, zh_lines):
        if not tidy_zh(zh):
            continue  # 译文为空 = 这条是噪声，不显示
        text = _event_text(zh, cue["text"], m, bilingual)
        events.append(f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(cue['end'])},zh,,0,0,0,,{text}")
    return _ass_header(play_w, play_h, m) + "\n".join(events) + "\n"


def preview_ass(zh: str, src: str, play_w: int, play_h: int, style: dict, bilingual: bool) -> str:
    """只有一条、一直显示的字幕，用来渲染预览画面。"""
    m = style_metrics(play_w, play_h, style)
    text = _event_text(zh, src, m, bilingual)
    return _ass_header(play_w, play_h, m) + f"Dialogue: 0,0:00:00.00,9:59:59.00,zh,,0,0,0,,{text}\n"


# ───────────────────────── SRT（通用字幕文件，留给以后用）─────────────────────────

def _srt_time(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def build_srt(cues: list[dict], texts: list[str]) -> str:
    blocks, n = [], 0
    for cue, text in zip(cues, texts):
        text = "\n".join(_clean(part) for part in str(text).split("\n") if _clean(part))
        if not text:
            continue
        n += 1
        blocks.append(f"{n}\n{_srt_time(cue['start'])} --> {_srt_time(cue['end'])}\n{text}\n")
    return "\n".join(blocks)
