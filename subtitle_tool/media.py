"""ffprobe / ffmpeg 的封装：读视频信息、截预览画面、把字幕压进视频、找视频自带的字幕。"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .common import Cancelled, RunContext, UserError
from .config import FFMPEG, FFPROBE

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".webm", ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".vob", ".ogv", ".asf", ".divx", ".f4v",
}
# 文字型字幕可以直接拿来翻译；图片型字幕（蓝光 PGS、DVD VobSub）不行，只能走语音识别
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt")
ZH_TAGS = {
    "zh", "zho", "chi", "chs", "cht", "sc", "tc", "cn", "cmn", "zh-cn", "zh-tw", "zh-hk", "zh-hans",
    "zh-hant", "chinese", "简体", "繁体", "简中", "繁中", "中文", "简", "繁", "中字",
}
_LANG_ALIASES = {"ja": {"ja", "jpn", "jp", "japanese", "日语", "日文"}, "en": {"en", "eng", "english", "英语", "英文"}}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS and not path.name.startswith(".")


def _ratio(text) -> float | None:
    try:
        a, b = str(text).replace("/", ":").split(":")
        a, b = float(a), float(b)
        return a / b if a > 0 and b > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path) -> dict:
    """读取视频的基本信息。play_w / play_h 是最终画面（方形像素、偶数）的尺寸。"""
    path = Path(path)
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise UserError("读取视频信息超时，文件可能已损坏") from None
    except OSError as e:
        raise UserError(f"无法运行 ffprobe：{e}") from None
    if r.returncode != 0:
        raise UserError("无法读取这个视频文件，可能已损坏或不是视频")
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        raise UserError("无法读取这个视频文件") from None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if not video:
        raise UserError("文件里没有视频画面")
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if w <= 0 or h <= 0:
        raise UserError("无法识别视频的画面尺寸")

    sar = _ratio(video.get("sample_aspect_ratio")) or 1.0
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int(float((video.get("tags") or {}).get("rotate") or 0))
        except (TypeError, ValueError):
            rotation = 0
    rotated = abs(rotation) % 180 == 90
    coded = (h, w) if rotated else (w, h)  # ffmpeg 解码时会自动按 rotation 旋转
    disp_w, disp_h = (h, w * sar) if rotated else (w * sar, h)
    play_w = max(2, int(round(disp_w / 2)) * 2)
    play_h = max(2, int(round(disp_h / 2)) * 2)

    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate")) or 25.0
    duration = 0.0
    for cand in (fmt.get("duration"), video.get("duration")):
        try:
            duration = float(cand)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    audio, subs = [], []
    for s in streams:
        tags = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
        if s.get("codec_type") == "audio":
            audio.append({
                "stream_index": s.get("index"),
                "codec": s.get("codec_name") or "",
                "channels": int(s.get("channels") or 2),
                "language": str(tags.get("language") or "").lower(),
                "default": bool((s.get("disposition") or {}).get("default")),
            })
        elif s.get("codec_type") == "subtitle":
            subs.append({
                "index": len(subs),  # 第几条字幕轨（给 -map 0:s:N 用）
                "codec": s.get("codec_name") or "",
                "language": str(tags.get("language") or "").lower(),
                "title": str(tags.get("title") or ""),
                "text": (s.get("codec_name") or "") in TEXT_SUB_CODECS,
            })

    return {
        "path": str(path),
        "duration": duration,
        "width": w,
        "height": h,
        "play_w": play_w,
        "play_h": play_h,
        "needs_scale": (play_w, play_h) != coded,
        "fps": fps,
        "video_index": video.get("index", 0),
        "video_codec": video.get("codec_name") or "",
        "audio": audio,
        "subs": subs,
    }


def pick_audio(info: dict, lang: str = "auto") -> dict | None:
    """多音轨时：优先选和所选语言一致的那条，其次选默认音轨，再其次第一条。"""
    tracks = info.get("audio") or []
    if not tracks:
        return None
    wanted = _LANG_ALIASES.get(lang)
    if wanted:
        for a in tracks:
            if a["language"] in wanted:
                return a
    return next((a for a in tracks if a["default"]), tracks[0])


def _base_filters(info: dict) -> list[str]:
    if info["needs_scale"]:
        return [f"scale={info['play_w']}:{info['play_h']}:flags=lanczos", "setsar=1"]
    return []


def grab_frame(info: dict, t: float, ass_file: Path | None = None, max_width: int = 1280) -> bytes:
    """截取 t 秒处的一帧（JPEG）。给了 ass_file 就把字幕也画上去——和最终成品用的是同一套渲染。"""
    duration = info.get("duration") or 0
    if duration > 1:
        t = min(max(0.0, t), duration - 0.5)
    filters = _base_filters(info)
    cwd = None
    if ass_file is not None:
        filters.append(f"ass={ass_file.name}")  # 用相对文件名，避开滤镜参数里的各种转义问题
        cwd = str(ass_file.parent)
    if info["play_w"] > max_width:
        filters.append(f"scale={max_width}:-2")
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, t):.3f}", "-i", info["path"],
        "-map", f"0:{info['video_index']}", "-frames:v", "1",
    ]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise UserError("截取预览画面超时") from None
    if r.returncode != 0 or not r.stdout:
        raise UserError("无法截取这个时间点的画面")
    return r.stdout


def _x264_options(info: dict) -> list[str]:
    """软件编码（x264）：文件最小，但所有 CPU 核心都会跑满，电脑会发烫、风扇会响。"""
    pixels = info["play_w"] * info["play_h"]
    if pixels <= 720 * 576:  # 标清：慢一点也很快，画质优先
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "19"]
    if pixels <= 1280 * 720:
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]  # 1080p 及以上：速度优先


def _hardware_options(info: dict) -> list[str]:
    """硬件编码（Mac 自带的 VideoToolbox 编码芯片）：几乎不占 CPU。

    在这台 M1 Pro 上用同一段视频实测：CPU 用量是 x264 的 1/14，速度略快；
    下面的画质档位和 x264 的档位画质相当（SSIM / PSNR 持平，放大对比看不出差别），文件大约大 50%。
    """
    pixels = info["play_w"] * info["play_h"]
    quality = 75 if pixels <= 720 * 576 else 70 if pixels <= 1280 * 720 else 65
    # 不指定的话 ffmpeg 会每 12 帧放一个关键帧，白白多占两成多的体积；改成大约 8 秒一个
    gop = int(max(24, min(300, round((info.get("fps") or 30) * 8))))
    return ["-c:v", "h264_videotoolbox", "-q:v", str(quality), "-profile:v", "high", "-g", str(gop)]


def _encode(info: dict, ass_file: Path, out_file: Path, ctx: RunContext, on_progress,
            audio: dict | None, video_options: list[str]) -> str | None:
    """跑一次 ffmpeg。成功返回 None，失败返回错误说明；用户取消则抛 Cancelled。"""
    filters = _base_filters(info) + [f"ass={ass_file.name}"]
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-i", info["path"], "-map", f"0:{info['video_index']}",
    ]
    if audio is not None:
        cmd += ["-map", f"0:{audio['stream_index']}"]
    cmd += ["-vf", ",".join(filters), *video_options, "-pix_fmt", "yuv420p"]
    if audio is not None:
        cmd += ["-c:a", "aac", "-b:a", "160k" if audio["channels"] <= 2 else "384k"]
    cmd += ["-sn", "-dn", "-map_chapters", "-1", "-movflags", "+faststart",
            "-max_muxing_queue_size", "4096", "-f", "mp4", str(out_file)]

    duration = info.get("duration") or 0
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(cmd, cwd=str(ass_file.parent), stdout=subprocess.PIPE, stderr=errlog, text=True)
        ctx.register(proc)
        try:
            for line in proc.stdout:
                if on_progress and duration > 0 and line.startswith("out_time_us="):
                    try:
                        on_progress(min(1.0, max(0.0, int(line.split("=", 1)[1]) / 1e6 / duration)))
                    except ValueError:
                        pass
            code = proc.wait()
        finally:
            ctx.unregister(proc)
            if proc.poll() is None:
                proc.kill()
        if ctx.cancel.is_set():
            _unlink(out_file)
            raise Cancelled()
        if code != 0:
            errlog.seek(0)
            detail = errlog.read().decode("utf-8", "replace").strip().splitlines()[-3:]
            _unlink(out_file)
            return " / ".join(detail) or f"ffmpeg 退出码 {code}"
    return None


def burn_subtitles(info: dict, ass_file: Path, out_file: Path, ctx: RunContext,
                   on_progress=None, audio: dict | None = None, encoder: str = "hardware") -> str:
    """把 ass_file 里的字幕压进画面，输出 H.264 + AAC 的 MP4（几乎所有播放器、手机、电视都能放）。

    encoder="hardware" 用硬件编码（省电安静）；用不了时（例如不是 Apple 芯片的 Mac）自动改用软件编码。
    返回实际用的方式："hardware" 或 "software"。
    """
    used = "software"
    error = "硬件编码不可用"
    if encoder == "hardware":
        used = "hardware"
        error = _encode(info, ass_file, out_file, ctx, on_progress, audio, _hardware_options(info))
    if error is not None:
        used = "software"
        error = _encode(info, ass_file, out_file, ctx, on_progress, audio, _x264_options(info))
    if error is not None:
        raise UserError("压制视频失败：" + error)
    if on_progress:
        on_progress(1.0)
    return used


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ───────────────────────── 视频自带的字幕 ─────────────────────────

def _lang_rank(tag: str, wanted: str) -> int | None:
    """给字幕的语言标签打分：越小越优先；None 表示不要（比如已经是中文字幕）。"""
    tag = tag.lower().strip()
    if tag in ZH_TAGS:
        return None
    if wanted in _LANG_ALIASES:
        if tag in _LANG_ALIASES[wanted]:
            return 0
        return 5 if not tag or tag == "und" else None  # 指定了语言：语言不符的字幕不用
    if tag in _LANG_ALIASES["ja"]:
        return 0
    if tag in _LANG_ALIASES["en"]:
        return 1
    return 2


def _tag_to_lang(tag: str) -> str:
    for code, names in _LANG_ALIASES.items():
        if tag.lower() in names:
            return code
    return ""


def _decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8", "cp932", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _to_srt(args: list[str], cwd: str | None = None) -> str | None:
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *args, "-f", "srt", "pipe:1"],
                           cwd=cwd, capture_output=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.decode("utf-8", "replace")


def find_existing_subtitles(video: Path, info: dict, wanted_lang: str = "auto") -> dict | None:
    """找可以直接翻译的现成字幕：先找同名的字幕文件，再找视频里内嵌的文字字幕轨。

    返回 {"origin": 说明, "language": 语言代码或空, "srt": SRT 文本}；没有就返回 None。
    """
    candidates: list[tuple[int, str, object]] = []

    stem = video.stem
    try:
        siblings = sorted(video.parent.iterdir())
    except OSError:
        siblings = []
    for f in siblings:
        if f.suffix.lower() not in SIDECAR_EXTS or not f.is_file():
            continue
        if f.stem == stem:
            tag = ""
        elif f.stem.startswith(stem + "."):
            # “视频名.ja.srt”这种带语言标记的。标记必须像个语言代码，
            # 否则 “第1集.Part2.srt” 会被误认成 “第1集” 的字幕
            tag = f.stem[len(stem) + 1:].lower()
            known = tag in ZH_TAGS or any(tag in names for names in _LANG_ALIASES.values())
            if not known and not (tag.isascii() and tag.isalpha() and 2 <= len(tag) <= 3):
                continue
        else:
            continue
        rank = _lang_rank(tag, wanted_lang) if tag else (5 if wanted_lang in _LANG_ALIASES else 2)
        if rank is not None:
            candidates.append((rank, tag, f))

    for s in info.get("subs") or []:
        if not s["text"]:
            continue
        title_words = set(s["title"].lower().replace("-", " ").split())
        if title_words & ZH_TAGS:
            continue
        rank = _lang_rank(s["language"], wanted_lang)
        if rank is not None:
            candidates.append((rank + 0.5, s["language"], s))  # 同等条件下，外挂字幕文件优先

    candidates.sort(key=lambda c: c[0])
    for _rank, tag, src in candidates:
        if isinstance(src, Path):
            try:
                text = _decode_text(src.read_bytes())
            except OSError:
                continue
            if src.suffix.lower() != ".srt":
                with tempfile.TemporaryDirectory() as tmp:
                    copy = Path(tmp) / ("sub" + src.suffix.lower())
                    copy.write_text(text, "utf-8")
                    text = _to_srt(["-i", copy.name], cwd=tmp) or ""
            if text.strip():
                return {"origin": f"字幕文件 {src.name}", "language": _tag_to_lang(tag), "srt": text}
        else:
            text = _to_srt(["-i", str(video), "-map", f"0:s:{src['index']}"])
            if text:
                return {"origin": "视频内嵌字幕", "language": _tag_to_lang(tag), "srt": text}
    return None
