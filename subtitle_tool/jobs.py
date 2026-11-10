"""批量任务。

三道工序（原文台词 → 翻译 → 压制）各用一条线程，像流水线一样同时进行：
第 1 集在压制时，第 2 集在翻译，第 3 集已经在识别了。整体比一集一集做快一倍左右。
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import media, pipeline, translate
from .common import Cancelled, RunContext, UserError
from .config import APP_NAME

WEIGHTS = {"source": 0.45, "translate": 0.15, "encode": 0.40}
FINAL_STATES = ("done", "error", "cancelled")


class Task:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.state = "queued"  # queued → source → wait_translate → translate → wait_encode → encode → done
        self.progress = {"source": 0.0, "translate": 0.0, "encode": 0.0}
        self.message = ""
        self.error = ""
        self.output = ""
        self.origin = ""
        self.language = ""
        self.cue_count = 0
        self.info: dict | None = None
        self.source: dict | None = None
        self.zh: list[str] | None = None

    def overall(self) -> float:
        if self.state == "done":
            return 1.0
        return sum(WEIGHTS[k] * min(1.0, self.progress[k]) for k in WEIGHTS)

    def fail(self, exc: BaseException) -> None:
        self.state = "error"
        if isinstance(exc, UserError):
            self.error = str(exc)
        else:
            self.error = f"意外错误：{type(exc).__name__}: {exc}"
            traceback.print_exception(exc)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path), "name": self.path.name, "state": self.state,
            "progress": dict(self.progress), "overall": round(self.overall(), 4),
            "message": self.message, "error": self.error, "output": self.output,
            "origin": self.origin, "language": self.language, "cues": self.cue_count,
        }


class Batch:
    def __init__(self, paths: list[str], settings: dict, api_key: str) -> None:
        self.settings = settings
        self.out_dir = Path(settings["output_dir"])
        self.ctx = RunContext()
        self.client = translate.Client(api_key, settings["api_base"], settings["translate_model"])
        self.tasks = [Task(p) for p in paths]
        self.started = time.time()
        self.finished: float | None = None
        self.fatal = ""
        self.cancelled = False
        self._q_translate: queue.Queue = queue.Queue()
        self._q_encode: queue.Queue = queue.Queue()
        self._caffeinate: subprocess.Popen | None = None

    # ── 控制 ──
    def start(self) -> None:
        pipeline.clean_partials(self.out_dir)
        try:  # 处理期间不让电脑睡眠
            self._caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            self._caffeinate = None
        for loop in (self._source_loop, self._translate_loop, self._encode_loop):
            threading.Thread(target=loop, daemon=True).start()

    def cancel(self) -> None:
        if self.finished is None:
            self.cancelled = True
            self.ctx.stop()

    @property
    def running(self) -> bool:
        return self.finished is None

    # ── 三条流水线 ──
    def _source_loop(self) -> None:
        for task in self.tasks:
            if self.ctx.cancel.is_set():
                task.state = "cancelled"
                continue
            task.state = "source"
            try:
                task.info = media.probe(task.path)

                def on_status(text: str, task=task) -> None:
                    task.message = text

                def on_progress(p: float, task=task) -> None:
                    task.progress["source"] = p

                source = pipeline.get_source(task.path, task.info, self.settings, self.ctx, on_progress, on_status)
                task.origin, task.language = source.get("origin", ""), source.get("language", "")
                task.cue_count = len(source["cues"])
                if not source["cues"]:
                    raise UserError("没有识别到任何台词")
                task.source = source
                task.progress["source"] = 1.0
                task.message = ""
                task.state = "wait_translate"
                self._q_translate.put(task)
            except Cancelled:
                task.state = "cancelled"
            except BaseException as e:  # noqa: BLE001
                task.fail(e)
        self._q_translate.put(None)

    def _translate_loop(self) -> None:
        while (task := self._q_translate.get()) is not None:
            if self.ctx.cancel.is_set():
                task.state = "cancelled"
                continue
            task.state = "translate"
            try:
                def on_progress(p: float, task=task) -> None:
                    task.progress["translate"] = p

                task.zh = pipeline.get_translation(task.path, task.source, self.settings, self.client,
                                                   self.ctx, on_progress)
                task.progress["translate"] = 1.0
                task.state = "wait_encode"
                self._q_encode.put(task)
            except Cancelled:
                task.state = "cancelled"
            except (translate.AuthError, translate.BalanceError) as e:
                # Key 不对或没余额：后面的也一定会失败，整批停下来，让用户先处理
                task.fail(e)
                self.fatal = str(e)
                self.ctx.stop()
            except BaseException as e:  # noqa: BLE001
                task.fail(e)
        self._q_encode.put(None)

    def _encode_loop(self) -> None:
        while (task := self._q_encode.get()) is not None:
            if self.ctx.cancel.is_set():
                task.state = "cancelled"
                continue
            task.state = "encode"
            try:
                def on_progress(p: float, task=task) -> None:
                    task.progress["encode"] = p

                out = pipeline.render(task.path, task.info, task.source, task.zh, self.settings,
                                      self.out_dir, self.ctx, on_progress)
                task.output = str(out)
                task.progress["encode"] = 1.0
                task.state = "done"
            except Cancelled:
                task.state = "cancelled"
            except BaseException as e:  # noqa: BLE001
                task.fail(e)
            finally:
                task.source = task.zh = None  # 用完就放掉，几百集也不占内存
        self._finish()

    def _finish(self) -> None:
        self.finished = time.time()
        if self._caffeinate is not None:
            try:
                self._caffeinate.terminate()
            except OSError:
                pass
        done = sum(1 for t in self.tasks if t.state == "done")
        failed = sum(1 for t in self.tasks if t.state == "error")
        if self.cancelled:
            return
        text = f"{done} 个视频已加好中文字幕" + (f"，{failed} 个失败" if failed else "")
        if self.fatal:
            text = self.fatal
        text = text.replace("\\", " ").replace('"', "'")
        try:
            subprocess.Popen(["/usr/bin/osascript", "-e",
                              f'display notification "{text}" with title "{APP_NAME}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # ── 给界面看的状态 ──
    def status(self) -> dict:
        tasks = [t.to_dict() for t in self.tasks]
        n = max(1, len(tasks))
        finished = sum(1 for t in tasks if t["state"] in FINAL_STATES)
        overall = sum(1.0 if t["state"] in FINAL_STATES else t["overall"] for t in tasks) / n
        elapsed = (self.finished or time.time()) - self.started
        eta = None
        if self.running and overall > 0.03 and elapsed > 20:
            eta = elapsed * (1 - overall) / overall
        return {
            "running": self.running, "cancelled": self.cancelled, "fatal": self.fatal,
            "overall": round(overall, 4), "elapsed": round(elapsed, 1),
            "eta": round(eta) if eta is not None else None,
            "total": len(tasks), "finished": finished,
            "done": sum(1 for t in tasks if t["state"] == "done"),
            "failed": sum(1 for t in tasks if t["state"] == "error"),
            "out_dir": str(self.out_dir),
            "tokens": self.client.prompt_tokens + self.client.completion_tokens,
            "tasks": tasks,
        }


_current: Batch | None = None
_lock = threading.Lock()


def current() -> Batch | None:
    return _current


def start_batch(paths: list[str], settings: dict, api_key: str) -> Batch:
    global _current
    if not api_key:
        raise UserError("还没有填写 DeepSeek API Key")
    if not settings["output_dir"]:
        raise UserError("还没有选择输出文件夹")
    out_dir = Path(settings["output_dir"]).expanduser()
    videos = [Path(p) for p in paths]
    videos = [p for p in videos if p.is_file() and media.is_video(p)]
    if not videos:
        raise UserError("没有选中任何视频")
    for p in videos:
        if _same_dir(p.parent, out_dir):
            raise UserError("输出文件夹不能和视频所在的文件夹相同，请另选一个")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UserError(f"无法创建输出文件夹：{e.strerror or e}") from None
    if not os.access(out_dir, os.W_OK):
        raise UserError("输出文件夹没有写入权限，请另选一个")
    with _lock:
        if _current is not None and _current.running:
            raise UserError("已经有任务在进行中")
        batch = Batch([str(p) for p in videos], dict(settings, output_dir=str(out_dir)), api_key)
        _current = batch
        batch.start()
    return batch


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False
