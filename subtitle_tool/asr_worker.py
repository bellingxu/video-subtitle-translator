"""语音识别子进程（Whisper，跑在 Mac 的 GPU 上）。

用法：python -m subtitle_tool.asr_worker <视频> <输出.json> --lang auto --model <模型> --ffmpeg <ffmpeg路径>

进度以“一行一个 JSON”的形式打印到标准输出，主程序读到后显示在界面上。
单独放在子进程里有两个好处：识别完内存（几个 GB）立刻还给系统；用户点取消时能马上停掉。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def emit(**event) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def load_audio(ffmpeg: str, video: str, stream_index: int | None):
    """抽出声音，转成 Whisper 要的 16kHz 单声道。

    aresample=async=1:first_pts=0 保证“声音的第 0 秒 = 视频的第 0 秒”，
    声音轨起点有偏移或中间有断点的视频，字幕时间也不会跑偏。
    """
    import numpy as np

    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", video,
           "-map", f"0:{stream_index}" if stream_index is not None else "0:a:0",
           "-vn", "-sn", "-dn", "-af", "aresample=async=1:first_pts=0",
           "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 3200:
        detail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise RuntimeError("读取不到视频里的声音 " + detail[0])
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def resolve_model(name: str) -> str:
    """模型已经在本机就直接用（不联网）；没有才去下载。"""
    if os.path.isdir(name):
        return name
    from huggingface_hub import snapshot_download

    try:
        path = snapshot_download(name, local_files_only=True)
        if os.path.exists(os.path.join(path, "config.json")) and any(
            os.path.exists(os.path.join(path, w)) for w in ("weights.safetensors", "weights.npz")
        ):
            return path
    except Exception:
        pass
    emit(event="status", text="download_model")
    return snapshot_download(name)


def detect_language(audio, model_path: str) -> str | None:
    """在整段声音里均匀取几个 30 秒的片段一起投票。

    Whisper 默认只听开头 30 秒，而电视剧开头经常是片头曲或没人说话，容易判断错。
    """
    import mlx.core as mx
    import numpy as np
    from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim

    holder = sys.modules["mlx_whisper.transcribe"].ModelHolder
    model = holder.get_model(model_path, mx.float16)
    if not model.is_multilingual:
        return "en"
    total = len(audio)
    if total <= N_SAMPLES:
        starts = [0]
    else:
        n = min(6, max(1, total // N_SAMPLES))
        starts = [int((total - N_SAMPLES) * (i + 0.5) / n) for i in range(n)]
    scores: dict[str, float] = {}
    for start in starts:
        chunk = audio[start:start + N_SAMPLES]
        if float(np.sqrt(np.mean(np.square(chunk)))) < 0.003:
            continue  # 这一段基本是静音
        mel = log_mel_spectrogram(chunk, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        segment = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        _, probs = model.detect_language(segment)
        for lang, p in probs.items():
            scores[lang] = scores.get(lang, 0.0) + float(p)
    return max(scores, key=scores.get) if scores else None


class _ProgressBar:
    """顶替 mlx_whisper 里的 tqdm 进度条，把进度转成 JSON 事件。"""

    last_emit = 0.0

    def __init__(self, total=None, **_kwargs):
        self.total = max(1, int(total or 1))
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def update(self, n=1):
        self.n += n
        now = time.monotonic()
        if now - _ProgressBar.last_emit >= 0.4:
            _ProgressBar.last_emit = now
            emit(event="progress", value=round(min(1.0, self.n / self.total), 4))


class _TqdmShim:
    tqdm = _ProgressBar


def _plain(obj):
    if hasattr(obj, "item"):
        return obj.item()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("output")
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--model", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--audio-stream", type=int, default=None)
    args = ap.parse_args()

    emit(event="status", text="extract_audio")
    audio = load_audio(args.ffmpeg, args.video, args.audio_stream)
    duration = len(audio) / 16000

    emit(event="status", text="load_model")
    import mlx_whisper

    model_path = resolve_model(args.model)
    tmod = sys.modules["mlx_whisper.transcribe"]
    tmod.tqdm = _TqdmShim

    lang = None if args.lang in ("", "auto") else args.lang
    if lang is None:
        emit(event="status", text="detect_language")
        lang = detect_language(audio, model_path)

    emit(event="status", text="transcribe", language=lang or "")
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=model_path,
        language=lang,
        task="transcribe",
        word_timestamps=True,  # 逐词时间戳：字幕时间更准，也能用来拆长句
        condition_on_previous_text=False,  # 每 30 秒独立识别，避免一处出错后面跟着一路错下去
        hallucination_silence_threshold=2.0,  # 长时间没人说话的地方不硬编台词
        verbose=False,
    )

    segments = []
    for seg in result.get("segments") or []:
        segments.append({
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": seg.get("text", ""),
            "avg_logprob": seg.get("avg_logprob"),
            "no_speech_prob": seg.get("no_speech_prob"),
            "compression_ratio": seg.get("compression_ratio"),
            "words": [{"word": w.get("word", ""), "start": w.get("start"), "end": w.get("end")}
                      for w in seg.get("words") or []],
        })
    out = {"language": result.get("language") or lang or "", "duration": duration, "segments": segments}
    tmp = args.output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=_plain)
    os.replace(tmp, args.output)
    emit(event="done", language=out["language"], segments=len(segments))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — 把任何错误都转成一行 JSON 告诉主程序
        emit(event="error", message=f"{type(e).__name__}: {e}")
        sys.exit(1)
