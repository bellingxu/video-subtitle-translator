"""本地小服务：给浏览器里的界面提供数据。只监听 127.0.0.1，外面的机器连不进来。"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, config, jobs, media, pipeline, subs, translate
from .common import UserError

WEB_DIR = Path(__file__).resolve().parent / "web"
SAMPLE_ZH = "这里是中文字幕的预览效果"
SAMPLE_SRC = "ここに元のセリフが表示されます"

_probe_cache: dict[tuple, dict] = {}
_probe_lock = threading.Lock()
_preview_slots = threading.Semaphore(2)
_key_status: dict = {"checked": 0.0, "result": None}


# ───────────────────────── 各个接口的实现 ─────────────────────────

def cached_probe(path: Path) -> dict:
    st = path.stat()
    key = (str(path), st.st_size, int(st.st_mtime))
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    info = media.probe(path)
    with _probe_lock:
        if len(_probe_cache) > 500:
            _probe_cache.clear()
        _probe_cache[key] = info
    return info


def _natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def api_state() -> dict:
    settings = config.load_settings()
    key = config.get_api_key()
    batch = jobs.current()
    return {
        "version": __version__,
        "settings": settings,
        "has_key": bool(key),
        "key_tail": key[-4:] if len(key) >= 8 else "",
        "job": batch.status() if batch else None,
    }


def api_key_status(force: bool = False) -> dict:
    """联网检查已保存的 Key 是否可用（结果缓存 60 秒，免得每次刷新页面都去请求）。"""
    key = config.get_api_key()
    if not key:
        return {"has_key": False}
    now = time.time()
    if not force and _key_status["result"] and now - _key_status["checked"] < 60:
        return _key_status["result"]
    settings = config.load_settings()
    try:
        info = translate.check_key(key, settings["api_base"])
        result = {"has_key": True, "ok": True, **info}
    except translate.AuthError as e:
        result = {"has_key": True, "ok": False, "error": str(e), "auth": True}
    except UserError as e:
        result = {"has_key": True, "ok": False, "error": str(e)}
    _key_status.update(checked=now, result=result)
    return result


def api_set_key(body: dict) -> dict:
    key = str(body.get("key") or "").strip()
    if not key:
        config.set_api_key("")
        _key_status.update(checked=0.0, result=None)
        return {"has_key": False}
    if len(key) < 8 or any(ch.isspace() for ch in key) or not key.isascii():
        raise UserError("这看起来不像一个 API Key（通常以 sk- 开头的一长串字母数字）")
    settings = config.load_settings()
    try:
        info = translate.check_key(key, settings["api_base"])
    except translate.AuthError:
        raise UserError("DeepSeek 说这个 Key 无效，请确认复制完整、没有多余的空格") from None
    except UserError as e:
        # 网络不通时没法验证：先存下来，提示用户
        config.set_api_key(key)
        _key_status.update(checked=0.0, result=None)
        return {"has_key": True, "ok": False, "key_tail": key[-4:], "error": f"已保存，但暂时无法验证：{e}"}
    config.set_api_key(key)
    # 默认的翻译模型如果已经不在可用列表里（DeepSeek 偶尔会改名），自动换成一个可用的
    models = info.get("models") or []
    if models and settings["translate_model"] not in models:
        pick = next((m for m in models if "flash" in m), None) or next(
            (m for m in models if "chat" in m), models[0])
        config.save_settings({"translate_model": pick})
    result = {"has_key": True, "ok": True, "key_tail": key[-4:], **info}
    _key_status.update(checked=time.time(), result=result)
    return result


def api_browse(path: str) -> dict:
    home = Path.home()
    p = Path(path).expanduser() if path else home
    try:
        p = p.resolve()
    except OSError:
        p = home
    while not p.is_dir() and p != p.parent:
        p = p.parent
    dirs, videos, error = [], 0, ""
    try:
        with os.scandir(p) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        if not entry.name.lower().endswith((".app", ".photoslibrary", ".bundle", ".framework")):
                            dirs.append({"name": entry.name, "path": entry.path})
                    elif media.is_video(Path(entry.name)):
                        videos += 1
                except OSError:
                    continue
    except PermissionError:
        error = "没有权限查看这个文件夹。请到 系统设置 → 隐私与安全性 → 文件和文件夹 里允许“终端”访问它。"
    except OSError as e:
        error = f"打不开这个文件夹：{e.strerror or e}"
    dirs.sort(key=lambda d: _natural_key(d["name"]))
    shortcuts = [
        {"name": "个人文件夹", "path": str(home)},
        {"name": "文稿", "path": str(home / "Documents")},
        {"name": "下载", "path": str(home / "Downloads")},
        {"name": "影片", "path": str(home / "Movies")},
        {"name": "桌面", "path": str(home / "Desktop")},
        {"name": "外接硬盘", "path": "/Volumes"},
    ]
    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": dirs,
        "videos": videos,
        "error": error,
        "shortcuts": [s for s in shortcuts if os.path.isdir(s["path"])],
    }


def api_mkdir(body: dict) -> dict:
    parent = Path(str(body.get("parent") or "")).expanduser()
    name = str(body.get("name") or "").strip()
    if not name or "/" in name or name in (".", "..") or name.startswith("."):
        raise UserError("文件夹名字不合适，换一个试试")
    if not parent.is_dir():
        raise UserError("上级文件夹不存在")
    target = parent / name
    try:
        target.mkdir(exist_ok=True)
    except OSError as e:
        raise UserError(f"无法新建文件夹：{e.strerror or e}") from None
    return {"path": str(target)}


def api_videos() -> dict:
    settings = config.load_settings()
    folder = settings["input_dir"]
    if not folder:
        return {"videos": [], "error": "", "input_dir": ""}
    out_dir = Path(settings["output_dir"]) if settings["output_dir"] else None
    items, error = [], ""
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file() or not media.is_video(Path(entry.name)):
                        continue
                    p = Path(entry.path)
                    items.append({
                        "path": entry.path,
                        "name": entry.name,
                        "size": entry.stat().st_size,
                        "done": bool(out_dir and pipeline.output_path(p, out_dir).exists()),
                        "translated": pipeline.is_translated(p),
                    })
                except OSError:
                    continue
    except FileNotFoundError:
        error = "这个文件夹不存在了，请重新选择"
    except PermissionError:
        error = "没有权限读取这个文件夹。请到 系统设置 → 隐私与安全性 → 文件和文件夹 里允许“终端”访问它。"
    except OSError as e:
        error = f"读取文件夹出错：{e.strerror or e}"
    items.sort(key=lambda v: _natural_key(v["name"]))
    return {"videos": items, "error": error, "input_dir": folder}


def api_video_info(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise UserError("找不到这个视频文件")
    info = cached_probe(p)
    return {
        "duration": info["duration"],
        "width": info["play_w"],
        "height": info["play_h"],
        "has_audio": bool(info["audio"]),
        "text_subs": sum(1 for s in info["subs"] if s["text"]),
        "translated": pipeline.is_translated(p),
    }


def api_preview(q: dict) -> bytes:
    p = Path(q.get("path", ""))
    if not p.is_file():
        raise UserError("找不到这个视频文件")
    info = cached_probe(p)
    try:
        t = float(q.get("t", 0))
    except ValueError:
        t = 0.0
    style = config.sanitize({
        "position": q.get("position"), "margin": q.get("margin"),
        "font_scale": q.get("font_scale"), "bilingual": q.get("bilingual") in ("1", "true"),
    })
    zh, src = SAMPLE_ZH, SAMPLE_SRC
    cached = pipeline.load_cached_subtitles(p)
    if cached:  # 已经翻译过：预览直接显示这个时间点真正的字幕
        for cue, line in zip(*cached):
            if cue["start"] - 0.3 <= t <= cue["end"] + 0.3 and subs.tidy_zh(line):
                zh, src = line, cue["text"]
                break
    with _preview_slots, tempfile.TemporaryDirectory(prefix="subtitle-preview-") as tmp:
        ass_file = Path(tmp) / "preview.ass"
        ass_file.write_text(
            subs.preview_ass(zh, src, info["play_w"], info["play_h"], style, style["bilingual"]), "utf-8")
        return media.grab_frame(info, t, ass_file)


def api_start(body: dict) -> dict:
    paths = [str(p) for p in body.get("paths") or [] if isinstance(p, str)]
    batch = jobs.start_batch(paths, config.load_settings(), config.get_api_key())
    return batch.status()


def api_open(body: dict) -> dict:
    settings = config.load_settings()
    target = body.get("target")
    if target == "output":
        folder = Path(settings["output_dir"])
        if not folder.is_dir():
            raise UserError("输出文件夹还不存在")
        subprocess.Popen(["/usr/bin/open", str(folder)])
    elif target == "video":
        p = Path(str(body.get("path") or "")).resolve()
        roots = [Path(d).resolve() for d in (settings["output_dir"], settings["input_dir"]) if d]
        if not p.is_file() or not any(root in p.parents for root in roots):
            raise UserError("找不到这个文件")
        subprocess.Popen(["/usr/bin/open", str(p)])
    else:
        raise UserError("未知操作")
    return {"ok": True}


# ───────────────────────── HTTP 外壳 ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "SubtitleTool"

    def log_message(self, fmt, *args):  # 不往终端刷访问日志
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _trusted(self) -> bool:
        """只接受从本工具自己的页面发来的请求，防止别的网页偷偷调用本机接口。"""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in hosts}:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return not site or site in ("same-origin", "none")

    def _dispatch(self, fn) -> None:
        try:
            result = fn()
        except UserError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._json({"error": f"程序内部出错：{type(e).__name__}: {e}"}, 500)
            return
        if isinstance(result, bytes):
            self._send(200, result, "image/jpeg")
        else:
            self._json(result)

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted():
            self._json({"error": "forbidden"}, 403)
            return
        url = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        route = url.path
        if route in ("/", "/index.html"):
            try:
                self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._json({"error": "界面文件丢失"}, 500)
        elif route == "/api/ping":
            self._json({"app": "subtitle_tool", "version": __version__})
        elif route == "/api/state":
            self._dispatch(api_state)
        elif route == "/api/key/status":
            self._dispatch(lambda: api_key_status(q.get("force") == "1"))
        elif route == "/api/browse":
            self._dispatch(lambda: api_browse(q.get("path", "")))
        elif route == "/api/videos":
            self._dispatch(api_videos)
        elif route == "/api/video-info":
            self._dispatch(lambda: api_video_info(q.get("path", "")))
        elif route == "/api/preview":
            self._dispatch(lambda: api_preview(q))
        elif route == "/api/job":
            batch = jobs.current()
            self._json({"job": batch.status() if batch else None})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted() or "application/json" not in self.headers.get("Content-Type", ""):
            self._json({"error": "forbidden"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if 0 < length < 2_000_000 else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._json({"error": "请求格式不对"}, 400)
            return
        route = urlsplit(self.path).path
        if route == "/api/settings":
            self._dispatch(lambda: {"settings": config.save_settings(body)})
        elif route == "/api/key":
            self._dispatch(lambda: api_set_key(body))
        elif route == "/api/mkdir":
            self._dispatch(lambda: api_mkdir(body))
        elif route == "/api/start":
            self._dispatch(lambda: api_start(body))
        elif route == "/api/cancel":
            batch = jobs.current()
            if batch:
                batch.cancel()
            self._json({"ok": True})
        elif route == "/api/open":
            self._dispatch(lambda: api_open(body))
        elif route == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=_shutdown, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)


def _shutdown(*_args) -> None:
    batch = jobs.current()
    if batch:
        batch.cancel()
    time.sleep(0.3)
    os._exit(0)


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as resp:
            return json.loads(resp.read()).get("app") == "subtitle_tool"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def serve(port: int = config.DEFAULT_PORT, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}/"
    if already_running(port):
        print(f"{config.APP_NAME} 已经在运行了，直接打开界面：{url}")
        if open_browser:
            subprocess.Popen(["/usr/bin/open", url])
        return

    httpd = None
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("启动失败：找不到可用的端口")
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_a: threading.Thread(target=_shutdown, daemon=True).start())

    print(f"\n  {config.APP_NAME} 已启动 ✔")
    print(f"  界面地址：{url}")
    print("  用完后关掉这个窗口（或在界面右上角点“退出”）即可。\n")
    if open_browser:
        subprocess.Popen(["/usr/bin/open", url])
    httpd.serve_forever()
