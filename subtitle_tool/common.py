"""各模块共用的小东西：错误类型、可取消的运行上下文。"""
from __future__ import annotations

import subprocess
import threading


class UserError(Exception):
    """可以直接显示给用户看的错误（中文、说人话）。"""


class Cancelled(Exception):
    """用户点了取消。"""


class RunContext:
    """一批任务共用：记录取消标志和正在运行的子进程，取消时一并结束。"""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
        if self.cancel.is_set():
            _kill(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def stop(self) -> None:
        self.cancel.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass
