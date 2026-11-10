"""调用 DeepSeek（OpenAI 兼容接口）把字幕翻译成简体中文。

做法：把台词按 40 条一组分批发送，每组附带前后几条台词作为上下文，
要求模型按编号返回 JSON，这样译文和时间轴能一一对上。几组同时请求以节省时间。
"""
from __future__ import annotations

import json
import random
import ssl
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import Cancelled, RunContext, UserError

PROMPT_VERSION = 3  # 改了提示词就 +1，让旧的翻译缓存失效
CHUNK_SIZE = 40
CONTEXT_BEFORE = 8
CONTEXT_AFTER = 4
MAX_WORKERS = 4

LANG_NAMES = {
    "ja": "日语", "en": "英语", "ko": "韩语", "zh": "中文", "yue": "粤语", "fr": "法语", "de": "德语",
    "es": "西班牙语", "it": "意大利语", "ru": "俄语", "pt": "葡萄牙语", "th": "泰语", "vi": "越南语",
}


class AuthError(UserError):
    """API Key 不对。"""


class BalanceError(UserError):
    """账户没钱了。"""


class _NetworkError(Exception):
    pass


class Client:
    """一次翻译任务用到的连接参数，顺便累计用了多少 token。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.key = api_key
        self.base = base_url.rstrip("/")
        self.model = model
        self.send_thinking = True  # DeepSeek 新模型默认开“深度思考”，翻译字幕用不着，关掉更快更省
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._lock = threading.Lock()

    def add_usage(self, usage) -> None:
        if isinstance(usage, dict):
            with self._lock:
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)


_ssl_context = None


def _context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context()
    return _ssl_context


def _http(method: str, url: str, api_key: str, payload: dict | None = None, timeout: float = 120):
    """返回 (状态码, 解析后的 JSON 或原始文本)。网络层面的问题统一抛 _NetworkError。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_context()) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
        raise _NetworkError(str(getattr(e, "reason", e)) or type(e).__name__) from None
    text = raw.decode("utf-8", "replace").strip()  # 排队时服务器会先发一些空行保持连接，strip 掉即可
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _error_message(body) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return str(err)
        return str(body.get("message") or body)[:300]
    return str(body)[:300]


def _raise_for_status(status: int, body) -> None:
    if status == 401:
        raise AuthError("DeepSeek API Key 无效，请检查后重新填写")
    if status == 402:
        raise BalanceError("DeepSeek 账户余额不足，请充值后再继续")
    raise UserError(f"DeepSeek 返回错误（{status}）：{_error_message(body)}")


def check_key(api_key: str, base_url: str) -> dict:
    """验证 Key 是否可用，顺便取回可用模型和余额。网络不通时抛 UserError。"""
    base = base_url.rstrip("/")
    try:
        status, body = _http("GET", base + "/models", api_key, timeout=20)
    except _NetworkError as e:
        raise UserError(f"连不上 DeepSeek（{e}），请检查网络") from None
    if status != 200:
        _raise_for_status(status, body)
    models = []
    if isinstance(body, dict):
        models = [str(m.get("id")) for m in body.get("data") or [] if isinstance(m, dict) and m.get("id")]
    result = {"models": models, "balance": None}
    try:
        status, body = _http("GET", base + "/user/balance", api_key, timeout=20)
        if status == 200 and isinstance(body, dict):
            infos = body.get("balance_infos") or []
            if infos and isinstance(infos[0], dict):
                result["balance"] = {
                    "currency": str(infos[0].get("currency") or ""),
                    "total": str(infos[0].get("total_balance") or ""),
                    "available": bool(body.get("is_available", True)),
                }
    except _NetworkError:
        pass
    return result


def _chat(client: Client, messages: list[dict], ctx: RunContext, abort: threading.Event,
          temperature: float = 1.3) -> dict:
    attempt = 0
    while True:
        ctx.check()
        if abort.is_set():
            raise Cancelled()
        payload = {
            "model": client.model,
            "messages": messages,
            "temperature": temperature,  # 默认 1.3 是 DeepSeek 文档对翻译任务的推荐值
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        sent_thinking = client.send_thinking
        if sent_thinking:
            payload["thinking"] = {"type": "disabled"}
        try:
            status, body = _http("POST", client.base + "/chat/completions", client.key, payload)
        except _NetworkError as e:
            attempt += 1
            if attempt > 5:
                raise UserError(f"连不上 DeepSeek（{e}），请检查网络后重试") from None
            _sleep(ctx, abort, min(30.0, 2.0 ** attempt) + random.random())
            continue
        if status == 200 and isinstance(body, dict):
            return body
        if status in (400, 422) and sent_thinking:
            client.send_thinking = False  # 有的兼容服务不认识 thinking 参数：去掉再试一次
            continue
        if status in (429, 500, 502, 503, 504):
            attempt += 1
            if attempt > 6:
                raise UserError("DeepSeek 服务器现在太忙，请过一会儿再试")
            _sleep(ctx, abort, min(40.0, 2.0 ** attempt) + random.random() * 2)
            continue
        _raise_for_status(status, body)


def _sleep(ctx: RunContext, abort: threading.Event, seconds: float) -> None:
    if ctx.cancel.wait(seconds) or abort.is_set():
        raise Cancelled()


SYSTEM_PROMPT = """你是资深的影视字幕翻译，负责把{lang}影视剧的台词翻译成自然、地道的简体中文字幕。

输入是按时间顺序排列的台词，格式是一个 JSON 对象：{{"编号": "原文"}}。
请逐条翻译，输出一个 json 对象：{{"编号": "译文"}}。例如输入 {{"1": "...", "2": "..."}}，就输出 {{"1": "第一条的译文", "2": "第二条的译文"}}。

要求：
1. 编号必须和输入完全一致，一条对一条：不能合并、拆分、遗漏或增加。
2. 台词大多由语音识别自动生成，可能有同音错字、漏字或断句不当。请结合剧情和上下文推断说话人的原意再翻译，不要照着错字硬译；如果给了【识别纠错】，按纠正后的意思翻译。实在听不出原意的短句，按最合理的日常说法意译，不要编造离奇的内容。
3. 如果给了【补充说明】，里面的信息（人名、性别、关系）最可靠，以它为准。如果给了【译名表】，人名、地名、称呼必须按表翻译，前后一致；原文只叫名（或只叫姓）时，译文也只用对应的名（或姓）。表里没有的日语人名优先写成对应的汉字；只有假名、无法确定汉字的，用常见译名或音译。
4. 不确定人物性别时不要用“他”“她”，改用人名或省略主语。
5. 一句话可能被拆成相邻的几条。每条译文只表达这一条原文对应的内容，同时保证相邻几条连起来读是通顺的。
6. 译文要口语化、简洁，符合中文字幕的习惯：每条尽量不超过 22 个汉字；句中停顿用空格隔开，不用逗号、顿号和句号；问号和感叹号保留；无实义的语气词可以简化。
7. 如果某条只是噪声或明显是识别错误产生的无关内容（例如与剧情无关的“感谢收看”“请订阅频道”），这条的译文输出空字符串 ""。正常的短句、应答声（“嗯”“好的”）要照常翻译。
8. 只输出 json，不要添加任何解释。"""

ANALYSIS_PROMPT = """你是影视字幕翻译团队里做“前期分析”的人。下面是一集{lang}影视剧的全部台词，按时间顺序排列。台词大多由语音识别自动生成，可能有同音错字和听错的词。请通读全部台词，然后输出一个 json 对象，供后面的翻译参考：

{{"summary": "用两三句中文概括这一集的剧情、主要人物及其关系",
 "names": [{{"src": "健太/ケンタ/けんた", "zh": "健太", "note": "主角的同学；全名 山田健太=山田健太"}}],
 "fixes": [{{"wrong": "识别错的原文片段", "right": "正确的原文", "zh": "中文意思"}}]}}

要求：
- names 只收录人名、地名和本剧特有的称呼，最多 30 条；听错的普通词语不要放进 names，放进 fixes。
- names 里每个人一条：src 列出台词里对这个人的各种写法（汉字、假名、昵称，用 / 隔开）；zh 是台词里最常用的那种叫法的译名（通常只是名字，不带姓）；台词里出现过全名的，把“全名=译名”写在 note 里。日语人名尽量用对应的汉字；地名用纠正后的正确写法。
- 人物的性别只有在台词里有明确依据时才写（例如被叫作“お姉ちゃん”“彼”“娘”），没有依据就不要写、不要猜。
- 如果给了【补充说明】或【已有译名表】，其中出现过的人和地方必须沿用里面的译名和信息。
- fixes 只列出你有把握的识别错误（比如根据剧情应该是另一个同音词），wrong 必须是台词里原样出现的片段，最多 20 条；没有把握就不要列，没有就给空数组。
- 只输出 json。"""


def _clip(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def analyze(texts: list[str], lang: str, client: Client, ctx: RunContext,
            title: str = "", hint: str = "", known_names: list[dict] | None = None) -> dict:
    """翻译前先让模型通读全集：写个剧情概要，整理人名表，挑出听错的词。

    这样分批翻译时每一批都知道全局：人名前后统一、知道主角是男是女、不会照着听错的词硬译。
    返回 {"summary": str, "names": [{"src","zh","note"}], "fixes": [{"wrong","right","zh"}]}。
    """
    parts = []
    if title:
        parts.append(f"【文件名（可能包含剧名，仅供参考）】{title}")
    if hint:
        parts.append(f"【补充说明】{hint}")
    if known_names:
        parts.append("【已有译名表】\n" + "\n".join(_name_line(n) for n in known_names))
    parts.append("【台词】\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts)))
    messages = [
        {"role": "system", "content": ANALYSIS_PROMPT.format(lang=LANG_NAMES.get(lang, "外语"))},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    abort = threading.Event()
    data = None
    for _attempt in range(2):
        body = _chat(client, messages, ctx, abort, temperature=0.3)  # 分析要稳，不要发挥
        client.add_usage(body.get("usage"))
        choice = (body.get("choices") or [{}])[0] or {}
        data = _parse_object(str((choice.get("message") or {}).get("content") or ""))
        if isinstance(data, dict):
            break
    if not isinstance(data, dict):
        return {"summary": "", "names": [], "fixes": []}
    joined = "\n".join(texts)
    names, fixes = [], []
    for n in data.get("names") or []:
        if isinstance(n, dict) and _clip(n.get("src"), 80) and _clip(n.get("zh"), 40):
            names.append({"src": _clip(n.get("src"), 80), "zh": _clip(n.get("zh"), 40), "note": _clip(n.get("note"), 60)})
    for f in data.get("fixes") or []:
        if not isinstance(f, dict):
            continue
        wrong, right = _clip(f.get("wrong"), 40), _clip(f.get("right"), 40)
        # 只保留确实在台词里出现过的；单个字的“纠错”太容易误伤别的句子，不要
        if len(wrong) >= 2 and right and wrong != right and wrong in joined:
            fixes.append({"wrong": wrong, "right": right, "zh": _clip(f.get("zh"), 40)})
    return {"summary": _clip(data.get("summary"), 400), "names": names[:40], "fixes": fixes[:30]}


def _name_line(n: dict) -> str:
    return f"- {n['src']} → {n['zh']}" + (f"（{n['note']}）" if n.get("note") else "")


def _variants(src: str) -> list[str]:
    return [v.strip() for v in src.replace("／", "/").split("/") if v.strip()]


def _user_message(numbered: dict, before: list[str], after: list[str], title: str, hint: str,
                  analysis: dict | None = None) -> str:
    parts = []
    if title:
        parts.append(f"【文件名（可能包含剧名，仅供参考）】{title}")
    if hint:
        parts.append(f"【补充说明】{hint}")
    if analysis:
        nearby = "\n".join([*before, *numbered.values(), *after])
        if analysis.get("summary"):
            parts.append(f"【剧情概要】{analysis['summary']}")
        # 只带上这一批台词里用得到的人名和纠错，提示词更短、更聚焦
        names = [n for n in analysis.get("names") or [] if any(v in nearby for v in _variants(n["src"]))]
        if names:
            parts.append("【译名表】\n" + "\n".join(_name_line(n) for n in names))
        fixes = [f for f in analysis.get("fixes") or [] if f["wrong"] in nearby]
        if fixes:
            parts.append("【识别纠错】（左边是听错的，右边是正确的）\n" + "\n".join(
                f"- {f['wrong']} → {f['right']}" + (f"（{f['zh']}）" if f.get("zh") else "") for f in fixes))
    if before:
        parts.append("【前文（仅供理解剧情，不要翻译）】\n" + "\n".join(f"- {t}" for t in before))
    if after:
        parts.append("【后文（仅供理解剧情，不要翻译）】\n" + "\n".join(f"- {t}" for t in after))
    parts.append(f"【待翻译】共 {len(numbered)} 条，请输出包含全部 {len(numbered)} 个编号的 json：\n"
                 + json.dumps(numbered, ensure_ascii=False, indent=0))
    return "\n\n".join(parts)


def _parse_object(content: str):
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[4:] if content.lower().startswith("json") else content
    try:
        return json.loads(content)
    except ValueError:
        pass
    a, b = content.find("{"), content.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(content[a:b + 1])
        except ValueError:
            return None
    return None


def _extract(data, n: int) -> dict[int, str]:
    """从模型的回答里取出 {行号(从0起): 译文}，对常见的“多包了一层”也能应付。"""
    if isinstance(data, dict) and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, (dict, list)):
            data = inner
    if isinstance(data, list):
        data = {str(i + 1): v for i, v in enumerate(data)} if len(data) == n else {}
    out: dict[int, str] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        try:
            i = int(str(key).strip()) - 1
        except ValueError:
            continue
        if 0 <= i < n and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[i] = " ".join(str(value).split())
    return out


def _translate_chunk(client: Client, lang: str, title: str, hint: str, analysis: dict | None,
                     lines: list[str], before: list[str], after: list[str], ctx: RunContext,
                     abort: threading.Event, depth: int = 0) -> list[str]:
    n = len(lines)
    numbered = {str(i + 1): text for i, text in enumerate(lines)}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(lang=LANG_NAMES.get(lang, "外语"))},
        {"role": "user", "content": _user_message(numbered, before, after, title, hint, analysis)},
    ]
    best: dict[int, str] = {}
    for _attempt in range(2):
        body = _chat(client, messages, ctx, abort)
        client.add_usage(body.get("usage"))
        choice = (body.get("choices") or [{}])[0] or {}
        content = str((choice.get("message") or {}).get("content") or "")
        got = _extract(_parse_object(content), n)
        if len(got) > len(best):
            best = got
        if len(best) == n:
            return [best[i] for i in range(n)]
        if choice.get("finish_reason") == "length":
            break  # 回答被截断了，重试也一样，直接拆小
    if n > 4 and depth < 3:
        mid = n // 2
        left = _translate_chunk(client, lang, title, hint, analysis, lines[:mid], before,
                                lines[mid:mid + CONTEXT_AFTER], ctx, abort, depth + 1)
        right = _translate_chunk(client, lang, title, hint, analysis, lines[mid:],
                                 (before + lines[:mid])[-CONTEXT_BEFORE:], after, ctx, abort, depth + 1)
        return left + right
    # 实在翻不出来的个别行：保留原文，总比空着强
    return [best.get(i, lines[i]) for i in range(n)]


def translate_lines(texts: list[str], lang: str, client: Client, ctx: RunContext,
                    title: str = "", hint: str = "", analysis: dict | None = None,
                    on_progress=None) -> list[str]:
    """把一个视频的全部台词翻译成中文，返回等长的译文列表。"""
    if not texts:
        return []
    results: list[str | None] = [None] * len(texts)
    abort = threading.Event()
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="translate")
    done = 0
    try:
        futures = {}
        for start in range(0, len(texts), CHUNK_SIZE):
            lines = texts[start:start + CHUNK_SIZE]
            before = texts[max(0, start - CONTEXT_BEFORE):start]
            after = texts[start + len(lines):start + len(lines) + CONTEXT_AFTER]
            fut = pool.submit(_translate_chunk, client, lang, title, hint, analysis, lines, before, after,
                              ctx, abort)
            futures[fut] = (start, len(lines))
        for fut in as_completed(futures):
            start, count = futures[fut]
            results[start:start + count] = fut.result()
            done += count
            if on_progress:
                on_progress(done / len(texts))
    except BaseException:
        abort.set()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    ctx.check()
    return [r if r is not None else texts[i] for i, r in enumerate(results)]
