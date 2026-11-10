"""单个视频的三道工序：取得原文台词 → 翻译 → 压制。

每一步的结果都存到 data/cache/<视频>/ 里：
  asr_raw.json  语音识别的原始结果
  source.json   整理好的原文字幕
  zh.json       中文译文
所以换个字幕位置重新压制时，不用再识别、再翻译（也不再花钱）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import media, subs, translate
from .common import Cancelled, RunContext, UserError
from .config import CACHE, FFMPEG, ROOT, write_json

CUE_VERSION = 1  # 改了“识别结果 → 字幕”的整理规则就 +1
SUBTITLE_DIR_NAME = "字幕文件"

ASR_STATUS = {
    "extract_audio": "读取声音",
    "load_model": "载入识别模型",
    "download_model": "下载语音识别模型（约 1.6 GB，只需这一次）",
    "detect_language": "判断语言",
    "transcribe": "识别台词",
}


def cache_dir(video: Path) -> Path:
    st = video.stat()
    key = hashlib.sha1(f"{video.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]
    safe = re.sub(r"[^\w\-]+", "_", video.stem).strip("_")[:48] or "video"
    return CACHE / f"{safe}-{key}"


def output_path(video: Path, out_dir: Path) -> Path:
    return out_dir / (video.stem + ".mp4")


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def load_cached_subtitles(video: Path) -> tuple[list[dict], list[str]] | None:
    """已经翻译过的视频：返回 (原文字幕, 译文)，给预览用。"""
    try:
        cdir = cache_dir(video)
    except OSError:
        return None
    source, zh = _load(cdir / "source.json"), _load(cdir / "zh.json")
    if not source or not zh:
        return None
    cues, lines = source.get("cues") or [], zh.get("lines") or []
    if not cues or len(cues) != len(lines):
        return None
    return cues, lines


def is_translated(video: Path) -> bool:
    try:
        return (cache_dir(video) / "zh.json").exists()
    except OSError:
        return False


# ───────────────────────── 第 1 步：原文台词 ─────────────────────────

def _run_asr(video: Path, info: dict, settings: dict, raw_file: Path, ctx: RunContext,
             on_progress, on_status) -> None:
    audio = media.pick_audio(info, settings["source_lang"])
    if audio is None:
        raise UserError("这个视频没有声音，没法识别台词")
    cmd = [
        sys.executable, "-u", "-m", "subtitle_tool.asr_worker", str(video), str(raw_file),
        "--lang", settings["source_lang"], "--model", settings["asr_model"], "--ffmpeg", FFMPEG,
        "--audio-stream", str(audio["stream_index"]),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    error = ""
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=errlog,
                                text=True, encoding="utf-8", errors="replace")
        ctx.register(proc)
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("event")
                if kind == "progress":
                    on_progress(float(event.get("value") or 0))
                elif kind == "status":
                    on_status(ASR_STATUS.get(event.get("text"), ""))
                elif kind == "error":
                    error = str(event.get("message") or "")
            code = proc.wait()
        finally:
            ctx.unregister(proc)
            if proc.poll() is None:
                proc.kill()
        if ctx.cancel.is_set():
            raise Cancelled()
        if code != 0 or not raw_file.exists():
            if not error:
                errlog.seek(0)
                tail = errlog.read().decode("utf-8", "replace").strip().splitlines()[-1:]
                error = tail[0] if tail else f"退出码 {code}"
            raise UserError(f"语音识别失败：{error}")


def get_source(video: Path, info: dict, settings: dict, ctx: RunContext,
               on_progress=lambda p: None, on_status=lambda s: None) -> dict:
    """返回 {"origin": 来源说明, "language": 语言代码, "cues": [...]}。"""
    cdir = cache_dir(video)
    cdir.mkdir(parents=True, exist_ok=True)
    params = {"lang": settings["source_lang"], "model": settings["asr_model"],
              "embedded": settings["use_embedded_subs"], "v": CUE_VERSION}
    cached = _load(cdir / "source.json")
    if cached and cached.get("params") == params and isinstance(cached.get("cues"), list):
        on_progress(1.0)
        return cached

    result = None
    if settings["use_embedded_subs"]:
        on_status("查找自带字幕")
        found = media.find_existing_subtitles(video, info, settings["source_lang"])
        if found:
            cues = subs.cues_from_srt(found["srt"])
            lang = found["language"] or subs.guess_language(cues)
            if not lang and settings["source_lang"] != "auto":
                lang = settings["source_lang"]
            # 条数太少的多半是“只翻译招牌/歌词”的特效字幕轨，不能当台词用；已经是中文的也不用翻
            enough = len(cues) >= max(5, (info.get("duration") or 0) / 60 * 2)
            if enough and lang != "zh":
                result = {"origin": found["origin"], "language": lang, "cues": cues}
    ctx.check()

    if result is None:
        raw_file = cdir / "asr_raw.json"
        raw_params = {"lang": settings["source_lang"], "model": settings["asr_model"]}
        raw = _load(raw_file)
        if not raw or raw.get("params") != raw_params:
            _run_asr(video, info, settings, raw_file, ctx, on_progress, on_status)
            raw = _load(raw_file)
            if not raw:
                raise UserError("语音识别没有产生结果")
            raw["params"] = raw_params
            write_json(raw_file, raw)
        lang = raw.get("language") or ""
        result = {"origin": "语音识别", "language": lang,
                  "cues": subs.cues_from_whisper(raw.get("segments") or [], lang)}

    result["params"] = params
    write_json(cdir / "source.json", result)
    on_progress(1.0)
    return result


# ───────────────────────── 第 2 步：翻译 ─────────────────────────

def get_translation(video: Path, source: dict, settings: dict, client: translate.Client,
                    ctx: RunContext, on_progress=lambda p: None) -> list[str]:
    texts = [c["text"] for c in source["cues"]]
    key = hashlib.sha1(json.dumps(
        {"t": texts, "m": settings["translate_model"], "h": settings["hint"],
         "l": source.get("language") or "", "v": translate.PROMPT_VERSION},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cache_file = cache_dir(video) / "zh.json"
    cached = _load(cache_file)
    if cached and cached.get("key") == key and len(cached.get("lines") or []) == len(texts):
        on_progress(1.0)
        return cached["lines"]

    lang = source.get("language") or ""
    # 先通读全集：剧情概要 + 人名表 + 听错的词。这一步失败了也不要紧，照常翻译
    analysis = None
    try:
        analysis = translate.analyze(texts, lang, client, ctx, title=video.stem, hint=settings["hint"],
                                     known_names=_series_names(video)[:40])
        _remember_names(video, analysis["names"])
    except (translate.AuthError, translate.BalanceError, Cancelled):
        raise
    except UserError:
        analysis = None
    on_progress(0.1)

    lines = translate.translate_lines(texts, lang, client, ctx, title=video.stem, hint=settings["hint"],
                                      analysis=analysis, on_progress=lambda p: on_progress(0.1 + 0.9 * p))
    write_json(cache_file, {"key": key, "model": settings["translate_model"], "analysis": analysis, "lines": lines})
    return lines


# 同一个文件夹里的视频当作同一部剧：人名表跨集共用，第 30 集里的名字和第 1 集译得一样

def _series_file(video: Path) -> Path:
    key = hashlib.sha1(str(video.parent.resolve()).encode()).hexdigest()[:12]
    return CACHE / "_series" / f"{key}.json"


def _series_names(video: Path) -> list[dict]:
    data = _load(_series_file(video)) or {}
    names = [n for n in data.get("names") or [] if isinstance(n, dict) and n.get("src") and n.get("zh")]
    return sorted(names, key=lambda n: -int(n.get("seen") or 1))


def _remember_names(video: Path, new_names: list[dict]) -> None:
    names = _series_names(video)
    for new in new_names:
        variants = translate._variants(new["src"])
        hit = next((old for old in names if set(variants) & set(translate._variants(old["src"]))), None)
        if hit is None:
            names.append({"src": new["src"], "zh": new["zh"], "note": new.get("note", ""), "seen": 1})
            continue
        # 已经有的人：译名保持最早定下来的那个，只补充新的写法
        known = translate._variants(hit["src"])
        hit["src"] = "/".join(known + [v for v in variants if v not in known])[:80]
        hit["seen"] = int(hit.get("seen") or 1) + 1
        if not hit.get("note") and new.get("note"):
            hit["note"] = new["note"]
    names.sort(key=lambda n: -int(n.get("seen") or 1))
    try:
        write_json(_series_file(video), {"folder": str(video.parent), "names": names[:80]})
    except OSError:
        pass


# ───────────────────────── 第 3 步：压制 ─────────────────────────

def render(video: Path, info: dict, source: dict, zh: list[str], settings: dict, out_dir: Path,
           ctx: RunContext, on_progress=lambda p: None) -> Path:
    work = cache_dir(video) / "work"
    work.mkdir(parents=True, exist_ok=True)
    ass_file = work / "sub.ass"
    ass_file.write_text(
        subs.build_ass(source["cues"], zh, info["play_w"], info["play_h"], settings, settings["bilingual"]),
        "utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    final = output_path(video, out_dir)
    # 先写成临时文件，成功后再改成正式名字——中途取消或出错不会留下一个看起来“完整”的坏视频
    tmp = out_dir / f".partial-{hashlib.sha1(video.name.encode()).hexdigest()[:10]}.mp4"
    try:
        media.burn_subtitles(info, ass_file, tmp, ctx, on_progress,
                             audio=media.pick_audio(info, settings["source_lang"]),
                             encoder=settings.get("encoder", "hardware"))
        os.replace(tmp, final)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    _write_subtitle_files(video, source, zh, out_dir)
    return final


def _write_subtitle_files(video: Path, source: dict, zh: list[str], out_dir: Path) -> None:
    """顺手把字幕也存成 .srt 文件（放在子文件夹里，免得播放器自动加载后和压好的字幕重影）。"""
    try:
        sub_dir = out_dir / SUBTITLE_DIR_NAME
        sub_dir.mkdir(parents=True, exist_ok=True)
        cues = source["cues"]
        zh_clean = [subs.tidy_zh(z) for z in zh]
        (sub_dir / f"{video.stem}.中文.srt").write_text(subs.build_srt(cues, zh_clean), "utf-8")
        (sub_dir / f"{video.stem}.原文.srt").write_text(subs.build_srt(cues, [c["text"] for c in cues]), "utf-8")
        both = [f"{z}\n{c['text']}" if z else "" for z, c in zip(zh_clean, cues)]
        (sub_dir / f"{video.stem}.双语.srt").write_text(subs.build_srt(cues, both), "utf-8")
    except OSError:
        pass  # 字幕文件只是附赠品，写不了不影响视频


def clean_partials(out_dir: Path) -> None:
    try:
        for f in out_dir.glob(".partial-*.mp4"):
            f.unlink(missing_ok=True)
    except OSError:
        pass
