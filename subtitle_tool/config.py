"""路径、设置和密钥的读写。

所有数据都放在工具文件夹里的 data/ 目录：
  data/settings.json  界面上的各种选项
  data/secrets.json   DeepSeek API Key（权限 600，只有本人可读）
  data/cache/         每个视频的识别结果和翻译结果，重新压制时不用重跑
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path

APP_NAME = "字幕翻译工具"
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SUBTITLE_TOOL_DATA") or (ROOT / "data"))
CACHE = DATA / "cache"
SETTINGS_FILE = DATA / "settings.json"
SECRETS_FILE = DATA / "secrets.json"

DEFAULT_PORT = 8791


def _tool(name: str) -> str:
    """优先用工具自带 runtime 里的 ffmpeg（带字幕渲染功能），找不到再用系统里的。"""
    for cand in (Path(sys.executable).parent / name, ROOT / "runtime" / "bin" / name):
        if cand.exists():
            return str(cand)
    return shutil.which(name) or name


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")

DEFAULT_SETTINGS = {
    "input_dir": "",
    "output_dir": "",
    "source_lang": "auto",  # auto / ja / en（也可以手动填 Whisper 支持的其它语言代码）
    "bilingual": False,  # True = 中文 + 原文 两行
    "position": "bottom",  # bottom / middle / top
    "margin": 5.0,  # 字幕离画面边缘的距离，占画面高度的百分比
    "font_scale": 6.5,  # 字号，占画面高度的百分比
    "use_embedded_subs": True,  # 视频自带文字字幕时直接翻译字幕，不做语音识别
    "encoder": "hardware",  # hardware = 硬件编码（省电安静，文件大约大一半）；software = 软件编码（文件最小，CPU 满载）
    "hint": "",  # 当前视频文件夹的剧情提示（剧名、人名等）。实际按文件夹存放在 hints 里
    "hints": {},  # {视频文件夹: 剧情提示} —— 换一部剧（换文件夹）时，上一部剧的提示不会串过来
    "translate_model": "deepseek-flash",
    "api_base": "https://api.deepseek.com",
    "asr_model": "mlx-community/whisper-large-v3-turbo",
}

_POSITIONS = ("bottom", "middle", "top")
_lock = threading.RLock()


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def sanitize(raw: dict) -> dict:
    """把任意来源的设置整理成合法值，缺的用默认值补上。"""
    s = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in raw and raw[key] is not None:
            s[key] = raw[key]
    for key in ("input_dir", "output_dir", "hint", "translate_model", "api_base", "asr_model", "source_lang"):
        s[key] = str(s[key]).strip()
    s["hint"] = s["hint"][:2000]
    hints = s["hints"] if isinstance(s["hints"], dict) else {}
    s["hints"] = {str(k): str(v).strip()[:2000] for k, v in list(hints.items())[-200:] if str(v).strip()}
    s["source_lang"] = s["source_lang"].lower() or "auto"
    if not s["source_lang"].isalpha() or len(s["source_lang"]) > 12:
        s["source_lang"] = "auto"
    if s["position"] not in _POSITIONS:
        s["position"] = "bottom"
    if s["encoder"] not in ("hardware", "software"):
        s["encoder"] = "hardware"
    s["margin"] = round(_clamp(s["margin"], 0.0, 40.0, DEFAULT_SETTINGS["margin"]), 1)
    s["font_scale"] = round(_clamp(s["font_scale"], 3.0, 14.0, DEFAULT_SETTINGS["font_scale"]), 1)
    s["bilingual"] = bool(s["bilingual"])
    s["use_embedded_subs"] = bool(s["use_embedded_subs"])
    s["api_base"] = s["api_base"].rstrip("/") or DEFAULT_SETTINGS["api_base"]
    for key in ("translate_model", "asr_model"):
        s[key] = s[key] or DEFAULT_SETTINGS[key]
    return s


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data, mode: int | None = None) -> None:
    """先写临时文件再改名，避免写到一半断电留下坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _with_hint(settings: dict) -> dict:
    settings["hint"] = settings["hints"].get(settings["input_dir"], "")
    return settings


def load_settings() -> dict:
    with _lock:
        return _with_hint(sanitize(_read_json(SETTINGS_FILE)))


def save_settings(patch: dict) -> dict:
    with _lock:
        merged = _read_json(SETTINGS_FILE)
        merged.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS and k not in ("hint", "hints")})
        clean = sanitize(merged)
        if "hint" in patch and clean["input_dir"]:  # 剧情提示记在当前的视频文件夹名下
            hint = str(patch["hint"] or "").strip()[:2000]
            if hint:
                clean["hints"][clean["input_dir"]] = hint
            else:
                clean["hints"].pop(clean["input_dir"], None)
        write_json(SETTINGS_FILE, {k: v for k, v in clean.items() if k != "hint"})
        return _with_hint(clean)


def get_api_key() -> str:
    with _lock:
        key = str(_read_json(SECRETS_FILE).get("deepseek_api_key") or "").strip()
    return key or os.environ.get("DEEPSEEK_API_KEY", "").strip()


def set_api_key(key: str) -> None:
    with _lock:
        key = key.strip()
        if key:
            write_json(SECRETS_FILE, {"deepseek_api_key": key}, mode=0o600)
        else:
            SECRETS_FILE.unlink(missing_ok=True)
