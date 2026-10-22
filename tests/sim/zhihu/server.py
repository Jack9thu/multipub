"""知乎本地仿真（只在模拟测试的 worker 子进程里使用）。

- httpx：www / zhuanlan / api.zhihu.com 的接口、OSS 图床、正文里的远程图片，经 httpx.MockTransport 提供；
- 浏览器：登录页 www.zhihu.com/signin（页面自己请求二维码接口并轮询 scan_info，扫码后服务端 Set-Cookie z_c0
  再跳首页）、首页（下发 _xsrf / d_c0）、风控验证页 /account/unhuman，经 patchright 的 context.route 提供。

场景参数：环境变量 MULTIPUB_SIM_ZHIHU（JSON），见 DEFAULTS。每个请求（方法、地址、关键请求头、cookie、请求体摘要）
和关键事件追加写到 MULTIPUB_SIM_ZHIHU_LOG（JSON Lines），测试据此断言。跨进程的状态（已发布内容、已上传图片、
风控是否已验证、自增 id）存在 MULTIPUB_SIM_ZHIHU_STATE（JSON）。

线上行为依据（docs/platforms/zhihu.md 与 zhihu_obsidian / KrisTHL181 / BAIGUANGMEI / Wechatsync 的代码）：
- 登录态 = cookie z_c0；失效时 401 + ERR_TICKET_NOT_EXIST；写接口要求 x-xsrftoken 与 _xsrf cookie 一致；
- 风控：403 + {"error": {"code": 40352, "redirect": "/account/unhuman?..."}}；
- 文章：POST zhuanlan/api/articles/drafts → {"id": <数字>} → PATCH …/draft（返回空响应）→ 话题 → content/publish；
- 想法：POST content/drafts {"action": "pin"} → {"code": 0, "data": {"content_id": "…"}} → content/publish；
- content/publish：草稿 id 不对 → 200 {"code": 2000, "message": "发布失败"}；成功 → {"code": 0, "message": "success",
  "data": {"result": "<JSON 字符串>"}}；
- 图片：POST api.zhihu.com/images → state 1（已有）/ 2（要传）+ STS 凭证 → OSS PUT（v1 签名，bucket zhihu-pics）→
  GET /images/<id>：init（还没传）/ processing / success；
- 草稿和文章同一个 id：GET www/api/v4/articles/<草稿 id> 对作者本人也可能返回 200（draft_visible）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html as htmlmod
import json
import os
import re
import time
import urllib.parse
from typing import Any

SIM_UID = "5d3a7b9e0c1f2a4b6c8d0e1f2a3b4c5d"
SIM_NAME = "模拟知友"
XSRF = "sim-xsrf-7f3c2a"
D_C0 = "AGBQsim-dc0|1760000000"
# 测试写进登录文件的 z_c0；扫码登录后服务端下发 QR_Z_C0_PREFIX + token
SIM_Z_C0 = "2|1:0|10:1760000000|4:z_c0|92:Mi4xc2ltLXVzZXItdGlja2V0|4f1c0ffee"
QR_Z_C0_PREFIX = "2|1:0|10:1761000000|4:z_c0|92:qr-"
RISK_SESSION = "9f1e2d3c4b5a69788776655443322110"
RISK_REDIRECT = f"/account/unhuman?type=S6E3V1&need_login=true&session={RISK_SESSION}"
OSS_BUCKET = "zhihu-pics"
ZHIMG_RE = re.compile(r"^(?:https?:)?//[a-z0-9-]+\.zhimg\.com/", re.I)
IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc=\"([^\"]*)\"", re.I)

DEFAULTS: dict[str, Any] = {
    "api_delay_ms": 20,             # 每个接口的延迟
    # ---- 登录态
    "auth": "ok",                   # ok / expired（所有接口 401）/ expired_sim（只有测试写入的 SIM_Z_C0 失效）
    "expire_at": "",                # 走到这一步时登录恰好过期（401），如 patch / publish
    "bind_phone": True,
    "xsrf_check": True,             # 写接口校验 x-xsrftoken == _xsrf cookie
    # ---- 风控：这些步骤返回 40352，直到在验证页通过验证（state.verified）
    "risk": [],                     # me / draft / patch / topics / images / publish / content_draft / verify_get
    "verify_page": "pass",          # pass（打开就自动放行，跳首页）/ stuck（一直停在验证页）
    # ---- 图片
    "image_processing_polls": 1,    # 每张图先返回几次 processing 再 success
    "image_status": "success",      # success / fail / stuck（一直 processing）
    "oss": "ok",                    # ok / 403（签名错）/ 500
    "remote_status": 200,           # 正文远程图片的 HTTP 状态
    # ---- 文章
    "draft_create": "ok",           # ok / 500 / no_id
    "patch_response": "empty",      # empty（200 空响应）/ 204 / json
    "patch": "ok",                  # ok / 500
    "cover_patch": "ok",            # ok / 400
    "topics_format": "list",        # list（顶层数组）/ data（{"data": [...]}）
    "topic_add": "ok",              # ok / 400
    "draft_visible": True,          # GET articles/<草稿 id> 对作者本人返回 200（草稿和文章同一个 id）
    # ---- 想法
    "content_draft": "ok",          # ok / code（200 + code≠0）
    # ---- 发布
    "publish": "ok",                # ok / reject / reject_http / risk / 502 / html / no_code / no_code_id /
                                    # read_timeout（服务端处理完、客户端读超时）/ hang（一直不回）/ conn_reset
    "publish_delay_ms": 300,
    "publish_reject_code": 4033,
    "publish_reject_message": "内容涉嫌违反社区规范，请修改后重试",
    "visible": True,                # 发布后 GET articles/<id>、pins/<id> 能查到（否则 404，审核中）
    # ---- 登录页（浏览器）
    "scan_after_polls": 3,          # 登录页轮询 scan_info 第几次时视为扫码完成
    "qr_api": True,                 # 登录页通过接口拿二维码（False：二维码接口 404，产品只能截图兜底）
    "qr_expire_after_polls": 0,     # >0：轮询到第几次时二维码过期，页面重新请求二维码
    "home_sets_xsrf": True,         # 首页下发 _xsrf / d_c0
    "home_redirect": False,         # 首页一加载就用脚本跳到 /follow（_xsrf 在 /follow 的响应里下发）
    "signin_redirect_home": True,   # 扫码完成后登录页自己跳首页
    "signin_close_after_ms": 0,     # >0：打开登录页这么久后用户关掉了窗口（还没扫码）
    "close_after_scan_ms": 0,       # >0：扫码成功这么久后用户就关掉了窗口
    "phone_login_after_ms": 0,      # >0：用户没扫码，过这么久在页面上用手机号登录成功
}

_MEM: dict[str, Any] = {"drafts": {}, "content_drafts": {}, "images": {}, "tokens": {}, "polls": {},
                        "scan_polls": {}, "qr_tokens": 0}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = os.environ.get("MULTIPUB_SIM_ZHIHU", "")
    if raw:
        cfg.update(json.loads(raw))
    return cfg


def log_event(kind: str, **data: Any) -> None:
    path = os.environ.get("MULTIPUB_SIM_ZHIHU_LOG")
    if not path:
        return
    row = {"type": kind, "t": round(time.time(), 3), "pid": os.getpid(), **data}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 跨进程状态

def _state_path() -> str | None:
    return os.environ.get("MULTIPUB_SIM_ZHIHU_STATE")


def load_state() -> dict[str, Any]:
    st: dict[str, Any] = {"next_id": 1965000000000000001, "articles": {}, "pins": {}, "hashes": [],
                          "verified": False, "valid_z_c0": [], "publish_calls": 0}
    path = _state_path()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            st.update(json.load(fh))
    return st


def save_state(st: dict[str, Any]) -> None:
    path = _state_path()
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def next_id() -> int:
    st = load_state()
    value = int(st["next_id"])
    st["next_id"] = value + 7
    save_state(st)
    return value


# ------------------------------------------------------------------ 独立实现的校验（不引用产品代码）

def parse_cookie_header(value: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        out.setdefault(k.strip(), []).append(v.strip())
    return out


def oss_expected_signature(access_key: str, method: str, headers: dict[str, str], resource: str) -> str:
    """阿里云 OSS v1：VERB\\nContent-MD5\\nContent-Type\\nDate\\n + x-oss-* 头（小写、排序）+ /bucket/key。"""
    oss = sorted((k.lower(), v.strip()) for k, v in headers.items() if k.lower().startswith("x-oss-"))
    canon = "".join(f"{k}:{v}\n" for k, v in oss)
    date = headers.get("date", "")
    to_sign = (f"{method}\n{headers.get('content-md5', '')}\n{headers.get('content-type', '')}\n"
               f"{date}\n{canon}{resource}")
    return base64.b64encode(hmac.new(access_key.encode(), to_sign.encode(), hashlib.sha1).digest()).decode()


def visible_text(fragment: str) -> str:
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", fragment))


# ------------------------------------------------------------------ 响应构造

def _risk_body() -> dict[str, Any]:
    return {"error": {"code": 40352, "message": "系统监测到您的网络环境存在异常，为保证您的正常访问，请点击下方验证按钮进行验证。",
                      "redirect": RISK_REDIRECT, "need_login": True}}


def _unauth_body() -> dict[str, Any]:
    return {"error": {"code": 100, "message": "请求未授权", "name": "ERR_TICKET_NOT_EXIST"}}


def _not_found(msg: str = "资源不存在") -> tuple[int, dict[str, Any]]:
    return 404, {"error": {"code": 4041, "message": msg}}


def image_info(md5: str) -> dict[str, Any]:
    return {"status": "success", "src": f"https://pic1.zhimg.com/v2-{md5}_b.jpg",
            "original_src": f"https://picx.zhimg.com/v2-{md5}", "original_hash": f"v2-{md5}",
            "watermark": "watermark", "watermark_src": f"https://pic2.zhimg.com/v2-{md5}_r.jpg",
            "watermark_hash": f"v2-{md5}_r", "width": 0, "height": 0}


class Api:
    """知乎接口仿真：handle(method, url, headers, body) -> (status, headers, body)。"""

    def __init__(self) -> None:
        self.cfg = config()

    # ---- 公共检查
    def cookies(self, headers: dict[str, str]) -> dict[str, list[str]]:
        return parse_cookie_header(headers.get("cookie", ""))

    def authed(self, cookies: dict[str, list[str]]) -> bool:
        z = cookies.get("z_c0") or []
        if len(z) != 1:  # 两个 z_c0 同时发过来：服务端取哪个不确定，按未登录处理
            return False
        value = z[0]
        if self.cfg["auth"] == "expired":
            return False
        if value == SIM_Z_C0:
            return self.cfg["auth"] != "expired_sim"
        return value in load_state()["valid_z_c0"]

    def risky(self, step: str) -> bool:
        return step in self.cfg["risk"] and not load_state()["verified"]

    # ---- 分发
    async def handle(self, method: str, url: str, headers: dict[str, str], body: bytes,
                     timeout: dict[str, Any] | None = None) -> tuple[int, dict[str, str], bytes]:
        await asyncio.sleep(self.cfg["api_delay_ms"] / 1000)
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname or "", parts.path
        query = dict(urllib.parse.parse_qsl(parts.query))
        cookies = self.cookies(headers)
        entry = {"method": method, "host": host, "path": path, "query": query,
                 "ua": headers.get("user-agent", ""), "xsrf": headers.get("x-xsrftoken"),
                 "origin": headers.get("origin"), "referer": headers.get("referer"),
                 "xrw": headers.get("x-requested-with"), "ctype": headers.get("content-type"),
                 "cookies": sorted(cookies), "z_c0": (cookies.get("z_c0") or [None])[0]}
        data: Any = None
        if body and "json" in (headers.get("content-type") or ""):
            try:
                data = json.loads(body.decode("utf-8"))
            except ValueError:
                data = "<bad json>"
        entry["json"] = data
        entry["size"] = len(body)
        log_event("req", **entry)

        if host == "zhihu-pics-upload.zhimg.com":
            return self.oss(method, path, headers, body, cookies)
        if host == "img.example.com":
            return self.remote(path, cookies)
        if host not in ("www.zhihu.com", "zhuanlan.zhihu.com", "api.zhihu.com"):
            return 599, {}, b""  # 由 transport 转成连接错误

        if not self.authed(cookies):
            log_event("unauthorized", path=path)
            return self.json(401, _unauth_body())
        if method != "GET" and self.cfg["xsrf_check"]:
            xs = cookies.get("_xsrf") or []
            if not xs or headers.get("x-xsrftoken") != xs[0]:
                log_event("xsrf_rejected", path=path, header=headers.get("x-xsrftoken"), cookie=xs)
                return self.json(403, {"error": {"code": 10002, "message": "请求参数异常（xsrf）"}})

        route = self.route(method, host, path)
        if route is None:
            log_event("unknown_route", method=method, host=host, path=path)
            return self.json(*_not_found(f"sim: no route {method} {host}{path}"))
        name, args = route
        if self.cfg["expire_at"] == name:
            log_event("unauthorized", path=path, step=name)
            return self.json(401, _unauth_body())
        if name in ("me", "draft", "patch", "topics", "images", "publish", "content_draft") and self.risky(name):
            log_event("risk", step=name)
            return self.json(403, _risk_body())
        handler = getattr(self, "h_" + name)
        return await handler(*args, query=query, data=data, headers=headers, timeout=timeout)

    @staticmethod
    def json(status: int, payload: Any, extra: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        hdrs = {"content-type": "application/json; charset=utf-8", **(extra or {})}
        return status, hdrs, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def route(method: str, host: str, path: str) -> tuple[str, tuple[Any, ...]] | None:
        if host == "www.zhihu.com":
            if path == "/api/v4/me" and method == "GET":
                return "me", ()
            if path == "/api/v4/content/drafts" and method == "POST":
                return "content_draft", ()
            if path == "/api/v4/content/publish" and method == "POST":
                return "publish", ()
            m = re.fullmatch(r"/api/v4/articles/(\d+)", path)
            if m and method == "GET":
                return "get_article", (m.group(1),)
            m = re.fullmatch(r"/api/v4/pins/(\d+)", path)
            if m and method == "GET":
                return "get_pin", (m.group(1),)
        if host == "zhuanlan.zhihu.com":
            if path == "/api/articles/drafts" and method == "POST":
                return "draft", ()
            m = re.fullmatch(r"/api/articles/(\d+)/draft", path)
            if m and method == "PATCH":
                return "patch", (m.group(1),)
            if path == "/api/autocomplete/topics" and method == "GET":
                return "topics", ()
            m = re.fullmatch(r"/api/articles/(\d+)/topics", path)
            if m and method == "POST":
                return "topic_add", (m.group(1),)
        if host == "api.zhihu.com":
            if path == "/images" and method == "POST":
                return "images", ()
            m = re.fullmatch(r"/images/(\d+)", path)
            if m and method == "GET":
                return "image_status", (m.group(1),)
        return None

    # ---- 账号
    async def h_me(self, **_: Any) -> tuple[int, dict[str, str], bytes]:
        return self.json(200, {"id": SIM_UID, "url_token": "sim-user", "name": SIM_NAME, "headline": "",
                               "is_bind_phone": self.cfg["bind_phone"], "type": "people",
                               "avatar_url": "https://picx.zhimg.com/v2-avatar_l.jpg"})

    # ---- 文章
    async def h_draft(self, data: Any, **_: Any) -> tuple[int, dict[str, str], bytes]:
        mode = self.cfg["draft_create"]
        if mode == "500":
            return 502, {"content-type": "text/html"}, b"<html><body>502 Bad Gateway</body></html>"
        draft_id = next_id()
        title = (data or {}).get("title", "") if isinstance(data, dict) else ""
        _MEM["drafts"][str(draft_id)] = {"title": title, "content": "", "topics": [], "titleImage": "",
                                          "toc": False}
        log_event("draft_created", id=str(draft_id), title=title)
        if mode == "no_id":
            return self.json(200, {"title": title})
        return self.json(200, {"id": draft_id, "title": title, "url": f"https://zhuanlan.zhihu.com/p/{draft_id}",
                               "state": "draft", "type": "article", "content": ""})

    async def h_patch(self, draft_id: str, data: Any, **_: Any) -> tuple[int, dict[str, str], bytes]:
        draft = _MEM["drafts"].get(draft_id)
        if draft is None:
            return self.json(*_not_found("草稿不存在"))
        if not isinstance(data, dict):
            return self.json(400, {"error": {"code": 400, "message": "参数错误"}})
        if "titleImage" in data:
            if self.cfg["cover_patch"] == "400":
                return self.json(400, {"error": {"code": 4001, "message": "题图不合法"}})
            if not ZHIMG_RE.match(str(data["titleImage"])):
                log_event("bad_cover", value=data["titleImage"])
            draft["titleImage"] = data["titleImage"]
        elif self.cfg["patch"] == "500":
            return 500, {"content-type": "text/html"}, b"<html>500</html>"
        for key in ("title", "content"):
            if key in data:
                draft[key] = data[key]
        if "table_of_contents" in data:
            draft["toc"] = bool(data["table_of_contents"])
        if "content" in data:
            bad = [s for s in IMG_SRC_RE.findall(str(data["content"])) if not ZHIMG_RE.match(s)
                   and "www.zhihu.com/equation" not in s]
            if bad:
                log_event("bad_content_images", srcs=bad)
        log_event("draft_patched", id=draft_id, keys=sorted(data))
        mode = self.cfg["patch_response"]
        if mode == "204":
            return 204, {}, b""
        if mode == "json":
            return self.json(200, {"id": int(draft_id), "updated": int(time.time())})
        return 200, {"content-type": "application/json"}, b""

    TOPIC_DB = {
        "徒步": ["徒步", "徒步旅行", "户外运动"],
        "python": ["Python", "Python 入门", "编程"],
        "人工智能": ["人工智能", "人工智能（AI）", "机器学习"],
        "git": ["Git", "GitHub", "版本控制"],
        "读书": ["读书", "书籍推荐"],
    }

    async def h_topics(self, query: dict[str, str], **_: Any) -> tuple[int, dict[str, str], bytes]:
        token = query.get("token", "")
        names = self.TOPIC_DB.get(token.strip().lower(), ["生活", "随笔"])  # 不认识的词：给些不相干的联想
        def tid(n: str) -> str:
            return str(19550000 + int(hashlib.md5(n.encode()).hexdigest()[:6], 16) % 9999)

        topics = [{"id": tid(n), "name": n, "type": "topic", "url": f"https://www.zhihu.com/topic/{tid(n)}",
                   "avatar_url": "https://picx.zhimg.com/v2-topic_l.jpg", "excerpt": f"{n}相关"}
                  for n in names[: int(query.get("max_matches", 5))]]
        log_event("topic_search", token=token, results=[t["name"] for t in topics])
        payload: Any = topics if self.cfg["topics_format"] == "list" else {"data": topics}
        return self.json(200, payload)

    async def h_topic_add(self, draft_id: str, data: Any, **_: Any) -> tuple[int, dict[str, str], bytes]:
        draft = _MEM["drafts"].get(draft_id)
        if draft is None:
            return self.json(*_not_found("草稿不存在"))
        if self.cfg["topic_add"] == "400" or not isinstance(data, dict) or not data.get("id"):
            return self.json(400, {"error": {"code": 4002, "message": "话题不存在"}})
        if len(draft["topics"]) >= 3:
            return self.json(400, {"error": {"code": 4003, "message": "最多添加 3 个话题"}})
        draft["topics"].append(data.get("name"))
        log_event("topic_added", id=draft_id, name=data.get("name"))
        return self.json(200, [{"id": data["id"], "name": data.get("name"), "type": "topic"}])

    async def h_get_article(self, article_id: str, **_: Any) -> tuple[int, dict[str, str], bytes]:
        st = load_state()
        art = st["articles"].get(article_id)
        if art is not None and self.cfg["visible"]:
            return self.json(200, {"id": int(article_id), "title": art["title"], "type": "article",
                                   "url": f"https://zhuanlan.zhihu.com/p/{article_id}", "state": "published"})
        if art is None and article_id in _MEM["drafts"] and self.cfg["draft_visible"]:
            # 作者本人查自己的草稿：同一个 id 也能查到（不能据此判断已发布）
            return self.json(200, {"id": int(article_id), "title": _MEM["drafts"][article_id]["title"],
                                   "type": "article", "state": "draft"})
        return self.json(*_not_found("你似乎来到了没有知识存在的荒原"))

    # ---- 想法
    async def h_content_draft(self, data: Any, **_: Any) -> tuple[int, dict[str, str], bytes]:
        action = data.get("action") if isinstance(data, dict) else None
        if self.cfg["content_draft"] == "code":
            return self.json(200, {"code": 1001, "message": "创建草稿失败"})
        content_id = str(next_id())
        _MEM["content_drafts"][content_id] = action
        log_event("content_draft_created", id=content_id, action=action)
        return self.json(200, {"code": 0, "message": "success", "data": {"content_id": content_id}})

    async def h_get_pin(self, pin_id: str, **_: Any) -> tuple[int, dict[str, str], bytes]:
        st = load_state()
        if pin_id in st["pins"] and self.cfg["visible"]:
            return self.json(200, {"id": pin_id, "type": "pin", "url": f"https://www.zhihu.com/pin/{pin_id}"})
        return self.json(*_not_found("想法不存在"))

    # ---- 发布
    def check_publish(self, data: Any) -> list[str]:
        """按线上能确定的约束检查 content/publish 请求体，返回问题列表（会写进日志，也会让发布失败）。"""
        problems: list[str] = []
        if not isinstance(data, dict) or data.get("action") not in ("article", "pin"):
            return ["action 不对"]
        body = data.get("data") or {}
        draft = (body.get("draft") or {}).get("id")
        action = data["action"]
        if action == "article" and str(draft) not in _MEM["drafts"]:
            problems.append(f"文章草稿 id 不存在: {draft!r}")
        if action == "pin" and _MEM["content_drafts"].get(str(draft)) != "pin":
            problems.append(f"想法草稿 id 不存在: {draft!r}")
        trace = (body.get("publish") or {}).get("traceId", "")
        if not re.fullmatch(r"\d{13},[0-9a-f-]{36}", str(trace)):
            problems.append(f"traceId 格式不对: {trace!r}")
        hybrid = body.get("hybrid") or {}
        html = str(hybrid.get("html") or "")
        if not html.strip():
            problems.append("hybrid.html 为空")
        if not isinstance(hybrid.get("textLength"), int):
            problems.append("hybrid.textLength 不是整数")
        bad = [s for s in IMG_SRC_RE.findall(html) if not ZHIMG_RE.match(s) and "www.zhihu.com/equation" not in s]
        if bad:
            problems.append(f"正文图片不在知乎图床: {bad[:3]}")
        title = str((body.get("title") or {}).get("title") or "")
        if action == "article":
            if not title.strip() or len(title) > 100:
                problems.append(f"标题长度不对: {len(title)}")
            raw = (body.get("extra_info") or {}).get("pc_business_params")
            try:
                biz = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                biz = None
            if not isinstance(biz, dict):
                problems.append("pc_business_params 不是 JSON 字符串")
        else:
            medias = ((body.get("media") or {}).get("medias")) or []
            if len(medias) > 9:
                problems.append(f"想法最多 9 张图（{len(medias)}）")
            for m in medias:
                img = m.get("image") or {}
                if not ZHIMG_RE.match(str(img.get("url") or "")):
                    problems.append(f"想法图片地址不对: {img.get('url')!r}")
        return problems

    async def h_publish(self, data: Any, timeout: dict[str, Any] | None = None,
                        **_: Any) -> tuple[int, dict[str, str], bytes]:
        st = load_state()
        st["publish_calls"] = int(st.get("publish_calls", 0)) + 1
        save_state(st)
        mode = self.cfg["publish"]
        problems = self.check_publish(data)
        body = (data or {}).get("data") or {} if isinstance(data, dict) else {}
        action = data.get("action") if isinstance(data, dict) else None
        draft_id = str((body.get("draft") or {}).get("id"))
        log_event("publish", action=action, draft=draft_id, mode=mode, problems=problems,
                  title=(body.get("title") or {}).get("title"),
                  html=(body.get("hybrid") or {}).get("html"),
                  text_length=(body.get("hybrid") or {}).get("textLength"),
                  statement=body.get("creationStatement"), column=body.get("column"),
                  biz=(body.get("extra_info") or {}).get("pc_business_params"),
                  medias=len(((body.get("media") or {}).get("medias")) or []))
        await asyncio.sleep(self.cfg["publish_delay_ms"] / 1000)
        if mode == "hang":
            await asyncio.sleep(3600)
        if mode == "conn_reset":
            return 598, {}, b""
        if problems:
            return self.json(200, {"code": 2000, "message": "发布失败", "data": {"problems": problems}})
        if mode == "reject":
            return self.json(200, {"code": self.cfg["publish_reject_code"],
                                   "message": self.cfg["publish_reject_message"]})
        if mode == "reject_http":
            return self.json(400, {"error": {"code": 4000, "message": self.cfg["publish_reject_message"]}})
        if mode == "502":
            return 502, {"content-type": "text/html"}, b"<html><body>502 Bad Gateway</body></html>"
        # 下面这些情况服务端都已经发出去了
        pub_id = draft_id if action == "article" else str(next_id())
        st = load_state()
        bucket = "articles" if action == "article" else "pins"
        record = {"title": (body.get("title") or {}).get("title"),
                  "topics": _MEM["drafts"].get(draft_id, {}).get("topics", []),
                  "cover": _MEM["drafts"].get(draft_id, {}).get("titleImage", ""),
                  "html": (body.get("hybrid") or {}).get("html")}
        if pub_id in st[bucket]:
            log_event("double_publish", id=pub_id)
        st[bucket][pub_id] = record
        save_state(st)
        log_event("published", action=action, id=pub_id, **{k: v for k, v in record.items() if k != "html"})
        if mode == "html":
            return 200, {"content-type": "text/html"}, b"<html><body>ok</body></html>"
        if mode == "no_code":
            return self.json(200, {"message": "success", "data": {}})
        if mode == "no_code_id":
            return self.json(200, {"data": {"result": json.dumps({"publish": {"id": pub_id}})}})
        if mode == "read_timeout":
            return 597, {}, b""
        url = (f"https://zhuanlan.zhihu.com/p/{pub_id}" if action == "article"
               else f"https://www.zhihu.com/pin/{pub_id}")
        result = {"publish": {"id": pub_id, "type": action, "url": url}, "id": pub_id}
        return self.json(200, {"code": 0, "message": "success",
                               "data": {"result": json.dumps(result, ensure_ascii=False)}})

    # ---- 图片
    async def h_images(self, data: Any, headers: dict[str, str], **_: Any) -> tuple[int, dict[str, str], bytes]:
        if not isinstance(data, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("image_hash", ""))):
            return self.json(400, {"error": {"code": 400, "message": "image_hash 不合法"}})
        if data.get("source") not in ("article", "pin", "answer", "question"):
            return self.json(400, {"error": {"code": 400, "message": "source 不合法"}})
        md5 = data["image_hash"]
        image_id = str(1700000000000000000 + int(md5[:12], 16) % 10**15)
        known = md5 in load_state()["hashes"]
        key = f"v2-{md5}"
        token = {"access_id": "STS.sim" + md5[:6], "access_key": "sk-" + os.urandom(6).hex(),
                 "access_token": "CAIS" + os.urandom(12).hex(), "access_timestamp": int(time.time()) + 3600}
        _MEM["images"][image_id] = {"md5": md5, "key": key, "uploaded": known, "source": data["source"]}
        _MEM["tokens"][key] = token
        log_event("image_registered", id=image_id, md5=md5, state=1 if known else 2, source=data["source"])
        return self.json(200, {"upload_file": {"state": 1 if known else 2, "image_id": image_id, "object_key": key},
                               "upload_token": token})

    async def h_image_status(self, image_id: str, **_: Any) -> tuple[int, dict[str, str], bytes]:
        img = _MEM["images"].get(image_id)
        if img is None:
            return self.json(*_not_found("图片不存在"))
        if not img["uploaded"]:
            return self.json(200, {"status": "init"})
        n = _MEM["polls"][image_id] = _MEM["polls"].get(image_id, 0) + 1
        mode = self.cfg["image_status"]
        if mode == "stuck" or n <= int(self.cfg["image_processing_polls"]):
            return self.json(200, {"status": "processing"})
        if mode == "fail":
            return self.json(200, {"status": "fail"})
        return self.json(200, image_info(img["md5"]))

    def oss(self, method: str, path: str, headers: dict[str, str], body: bytes,
            cookies: dict[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
        key = path.lstrip("/")
        if cookies:
            log_event("cookie_leak", host="zhihu-pics-upload.zhimg.com", names=sorted(cookies))
        token = _MEM["tokens"].get(key)
        problems = []
        if method != "PUT":
            problems.append(f"method {method}")
        if token is None:
            problems.append("没有登记过的 object_key")
        else:
            auth = headers.get("authorization", "")
            expected = f"OSS {token['access_id']}:" + oss_expected_signature(
                token["access_key"], method, headers, f"/{OSS_BUCKET}/{key}")
            if auth != expected:
                problems.append("签名不对")
            if headers.get("x-oss-security-token") != token["access_token"]:
                problems.append("security token 不对")
        if hashlib.md5(body).hexdigest() != key.removeprefix("v2-"):
            problems.append("内容 md5 与 object_key 不一致")
        if not (headers.get("content-type") or "").startswith("image/"):
            problems.append(f"Content-Type 不对: {headers.get('content-type')}")
        log_event("oss_put", key=key, size=len(body), ctype=headers.get("content-type"), problems=problems)
        if self.cfg["oss"] == "500":
            return 500, {"content-type": "application/xml"}, b"<Error><Code>InternalError</Code></Error>"
        if problems or self.cfg["oss"] == "403":
            return 403, {"content-type": "application/xml"}, (
                b"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Error><Code>SignatureDoesNotMatch</Code>"
                b"<Message>The request signature we calculated does not match the signature you provided.</Message>"
                b"</Error>")
        for img in _MEM["images"].values():
            if img["key"] == key:
                img["uploaded"] = True
        st = load_state()
        if key.removeprefix("v2-") not in st["hashes"]:
            st["hashes"].append(key.removeprefix("v2-"))
            save_state(st)
        return 200, {"etag": f'"{key[3:].upper()}"', "x-oss-request-id": "5f" + os.urandom(8).hex()}, b""

    def remote(self, path: str, cookies: dict[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
        if cookies:
            log_event("cookie_leak", host="img.example.com", names=sorted(cookies))
        status = int(self.cfg["remote_status"])
        if status != 200:
            return status, {"content-type": "text/html"}, b"not found"
        from .images import webp

        return 200, {"content-type": "image/webp"}, webp(80, 60)


# ------------------------------------------------------------------ httpx

def _httpx_handler_factory():
    import httpx

    api = Api()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        headers = {k.lower(): v for k, v in request.headers.items()}
        timeout = request.extensions.get("timeout") or {}
        status, hdrs, content = await api.handle(request.method, str(request.url), headers, body, timeout)
        if status == 599:
            raise httpx.ConnectError(f"sim: blocked {request.url}", request=request)
        if status == 598:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        if status == 597:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(status, headers=hdrs, content=content)

    return handler


# ------------------------------------------------------------------ 浏览器（登录页 / 首页 / 验证页）

SIGNIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>知乎 - 有问题，就会有答案</title>
<style>body{font-family:sans-serif;background:#f6f6f6}.SignContainer{width:700px;margin:80px auto;background:#fff;
padding:24px;display:flex;gap:40px}.Qrcode-img{width:150px;height:150px;border:1px solid #ddd}</style></head>
<body><div class="SignContainer"><div class="Qrcode-container"><div class="Qrcode-title">打开知乎 App</div>
<div class="Qrcode-content"><img class="Qrcode-img" alt="二维码"></div>
<div class="Qrcode-guide">在「我的页」右上角打开扫一扫</div></div>
<form class="SignFlow"><input name="username" placeholder="手机号"><input name="digits" placeholder="输入 6 位短信验证码">
<button type="submit" class="SignFlow-submitButton">登录/注册</button></form></div>
<script>
async function getQr(){
  const r = await fetch('/api/v3/account/api/login/qrcode', {method: 'POST', headers: {'x-requested-with': 'fetch'}});
  if (!r.ok) { setTimeout(getQr, 3000); return; }
  const d = await r.json();
  document.querySelector('.Qrcode-img').src = '/api/v3/account/api/login/qrcode/' + d.token + '/image';
  poll(d.token);
}
async function poll(token){
  for (;;) {
    await new Promise(r => setTimeout(r, 500));
    let d;
    try { d = await (await fetch('/api/v3/account/api/login/qrcode/' + token + '/scan_info')).json(); }
    catch (e) { continue; }
    if (d.status === 'expired') { getQr(); return; }
    if (d.user_id) {
      if (d.redirect) { setTimeout(() => { location.href = d.redirect; }, 300); }
      return;
    }
  }
}
getQr();
if (window.SIM_PHONE_LOGIN > 0) setTimeout(async () => {
  const d = await (await fetch('/api/v3/oauth/sign_in', {method: 'POST'})).json();
  if (d.user_id) location.href = '/';
}, window.SIM_PHONE_LOGIN);
</script></body></html>"""

HOME_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>首页 - 知乎</title></head>
<body><header class="AppHeader"><a class="AppHeader-profileEntry">模拟知友</a></header>
<main class="Topstory">推荐</main></body></html>"""

VERIFY_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>安全验证 - 知乎</title></head>
<body><div class="Unhuman"><h1>系统监测到您的网络环境存在异常</h1><button class="Button--primary">开始验证</button></div>
<script>
fetch('/api/v4/anticrawl/sim_verify', {method: 'POST'}).then(r => r.json()).then(d => {
  if (d.passed) setTimeout(() => { location.href = '/'; }, 800);
});
</script></body></html>"""


def _cookie_header(name: str, value: str, *, days: int = 30, http_only: bool = False) -> str:
    exp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + days * 86400))
    return (f"{name}={value}; Domain=zhihu.com; Path=/; Expires={exp}; Secure; SameSite=Lax"
            + ("; HttpOnly" if http_only else ""))


async def route_handler(route: Any, request: Any) -> bool:
    cfg = config()
    url = request.url
    parts = urllib.parse.urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if host != "www.zhihu.com":
        return False  # 静态资源等：一律 abort
    log_event("page", evt="request", method=request.method, path=path, query=parts.query)

    if path == "/signin":
        phone = int(cfg["phone_login_after_ms"])
        body = SIGNIN_HTML.replace("<script>", f"<script>window.SIM_PHONE_LOGIN = {phone};", 1)
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        if int(cfg["signin_close_after_ms"]) > 0:  # 模拟用户关掉登录窗口（页面脚本的 window.close() 关不掉）
            page = request.frame.page

            async def close_later() -> None:
                await asyncio.sleep(int(cfg["signin_close_after_ms"]) / 1000)
                log_event("page", evt="user_closed_window")
                await page.close()

            _MEM["close_task"] = asyncio.get_running_loop().create_task(close_later())
        return True
    if path == "/api/v3/account/api/login/qrcode" and request.method == "POST":
        if not cfg["qr_api"]:
            await route.fulfill(status=404, content_type="application/json", body='{"error":{"code":404}}')
            return True
        _MEM["qr_tokens"] += 1
        token = f"qr{_MEM['qr_tokens']}{os.urandom(4).hex()}"
        link = f"https://www.zhihu.com/account/scan/login/{token}?/api/login/qrcode"
        log_event("page", evt="qrcode_issued", token=token, link=link)
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"token": token, "link": link, "expires_at": int(time.time()) + 120}))
        return True
    if path == "/api/v3/oauth/sign_in" and request.method == "POST":  # 手机号 + 短信验证码登录
        z_c0 = QR_Z_C0_PREFIX + "phone"
        st = load_state()
        st["valid_z_c0"].append(z_c0)
        save_state(st)
        log_event("page", evt="phone_login")
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"user_id": SIM_UID}),
                            headers={"set-cookie": _cookie_header("z_c0", z_c0, http_only=True)})
        return True
    m = re.fullmatch(r"/api/v3/account/api/login/qrcode/([^/]+)/(image|scan_info)", path)
    if m:
        token, what = m.groups()
        if what == "image":
            from .images import png

            await route.fulfill(status=200, content_type="image/png", body=png(150, 150, (30, 30, 30)))
            return True
        n = _MEM["scan_polls"][token] = _MEM["scan_polls"].get(token, 0) + 1
        expire_at = int(cfg["qr_expire_after_polls"])
        if expire_at and n == expire_at and _MEM["qr_tokens"] == 1:
            log_event("page", evt="qrcode_expired", token=token)
            await route.fulfill(status=200, content_type="application/json", body='{"status": "expired"}')
            return True
        total = sum(_MEM["scan_polls"].values())
        if total < int(cfg["scan_after_polls"]):
            await route.fulfill(status=200, content_type="application/json", body='{"status": 0}')
            return True
        z_c0 = QR_Z_C0_PREFIX + token
        st = load_state()
        st["valid_z_c0"].append(z_c0)
        save_state(st)
        log_event("page", evt="scanned", token=token)
        if int(cfg["close_after_scan_ms"]) > 0:
            page = request.frame.page

            async def close_after_scan() -> None:
                await asyncio.sleep(int(cfg["close_after_scan_ms"]) / 1000)
                log_event("page", evt="user_closed_window")
                await page.close()

            _MEM["close_task"] = asyncio.get_running_loop().create_task(close_after_scan())
        body = {"user_id": SIM_UID, "uid": SIM_UID, "login_status": "CONFIRMED"}
        if cfg["signin_redirect_home"]:
            body["redirect"] = "/"
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(body),
                            headers={"set-cookie": _cookie_header("z_c0", z_c0, http_only=True)})
        return True
    if path == "/follow" and request.method == "GET":
        await asyncio.sleep(0.5)
        log_event("page", evt="follow")
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HOME_HTML,
                            headers={"set-cookie": _cookie_header("_xsrf", XSRF, days=365) + "\n"
                                     + _cookie_header("d_c0", D_C0, days=365)})
        return True
    if path == "/" and request.method == "GET" and cfg["home_redirect"]:
        log_event("page", evt="home_redirect")
        await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                            body="<!doctype html><html><head><script>location.replace('/follow')</script></head>"
                                 "<body>跳转中</body></html>")
        return True
    if path == "/" and request.method == "GET":
        headers = await request.all_headers()
        cookies = parse_cookie_header(headers.get("cookie", ""))
        if "z_c0" not in cookies:
            await route.fulfill(status=302, headers={"location": "https://www.zhihu.com/signin?next=%2F"})
            return True
        extra: dict[str, str] = {}
        if cfg["home_sets_xsrf"] and "_xsrf" not in cookies:
            extra["set-cookie"] = (_cookie_header("_xsrf", XSRF, days=365) + "\n"
                                   + _cookie_header("d_c0", D_C0, days=365))
        log_event("page", evt="home", cookies=sorted(cookies), sets=bool(extra))
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HOME_HTML, headers=extra)
        return True
    if path.startswith("/account/unhuman"):
        log_event("page", evt="verify_open", query=parts.query)
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=VERIFY_HTML)
        return True
    if path == "/api/v4/anticrawl/sim_verify":
        passed = cfg["verify_page"] == "pass"
        if passed:
            st = load_state()
            st["verified"] = True
            save_state(st)
        log_event("page", evt="verify", passed=passed)
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"passed": passed}))
        return True
    return False


# ------------------------------------------------------------------ 安装

def install() -> None:
    """worker 子进程里：httpx 一律走 MockTransport，浏览器走 route_handler；其他请求一律拒绝。"""
    import httpx

    from tests.sim._routing import install as install_routing

    install_routing(route_handler)
    handler = _httpx_handler_factory()
    orig_async_init = httpx.AsyncClient.__init__

    def async_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        orig_async_init(self, *args, **kwargs)

    def sync_init(self: Any, *args: Any, **kwargs: Any) -> None:  # 知乎不用同步客户端：用了就是意外
        raise RuntimeError("sim: 同步 httpx.Client 不在知乎仿真范围内")

    httpx.AsyncClient.__init__ = async_init  # type: ignore[method-assign]
    if os.environ.get("MULTIPUB_SIM_ZHIHU_SYNC_OK") != "1":
        httpx.Client.__init__ = sync_init  # type: ignore[method-assign]
