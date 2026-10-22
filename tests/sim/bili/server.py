"""B站本地仿真（只在模拟测试的 worker 子进程和仿 biliup 里使用）。

- 浏览器：t.bilibili.com 动态发布页（pages/t.html，按线上 dyn-home 发布器的类名、文案、流程搭建）、
  api.bilibili.com / passport.bilibili.com 接口，经 patchright 的 context.route 提供（见 sitecustomize.py）；
- httpx：同一套接口经 httpx.MockTransport 提供（nav、finger/spi、oauth2/info、TV 扫码、图文 HTTP 模式）；
- biliup：fake_biliup.py（独立脚本，见那里）。

场景参数：环境变量 MULTIPUB_SIM_BILI（JSON），见 DEFAULTS。关键事件（接口调用、发布请求体、页面上报、
biliup 收到的参数）追加写到 MULTIPUB_SIM_BILI_LOG（JSON Lines），测试据此断言。

线上行为依据（2026-10-20 的 dyn-home index.ad97d12f.js、API-collect 文档、biliup v1.3.1 源码）：
- 发布页是 SPA：先渲染页头，再调 nav，登录后才渲染 .bili-dyn-publishing；没有 buvid3 的请求整页 412；
- 点「图片」工具会立刻弹出选文件框（PicsUploader 可见且没有图片时自动 trigger），选文件框的 input 不挂在 DOM 上，
  可多选；每张图先做本地校验（类型、宽高 >= 10px），再依次 POST upload_bfs，失败显示「上传失败」；
- 「发布」按钮只在没有正文也没有图片时禁用；有图片没传完时点发布会弹「图片上传中」确认框；
- 立即发布且 cookie dy_spec_agreed != 1 时先弹《动态发布规范》（确认并发送 / 取消），确认后写 cookie；
  定时发布不弹；发布 = submit_check + create/dyn，成功 toast「发布成功」并清空发布框，失败 toast 错误信息；
- 定时：发布器组件状态 tools.timingSetting（active / data.datetime），create/dyn 的 option.timer_pub_time =
  floor(datetime / 1000)；AI 声明：creationOption.value = 1 -> option.aigc = 1。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from typing import Any

PAGES = Path(__file__).resolve().parent / "pages"

MID = 10086
UNAME = "模拟UP主"
IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
TV_APPKEY, TV_APPSEC = "4409e2ce8ffd12b8", "59b43e04ad6965f34319062b478f83dd"
ANDROID_APPKEY, ANDROID_APPSEC = "783bbb7264451d82", "2653583c8873dea268ab9386918b1d65"
APPSECS = {TV_APPKEY: TV_APPSEC, ANDROID_APPKEY: ANDROID_APPSEC}
DYN_ID = "1062378417531961344"
BVID = "BV1Sm4y1c7Qa"
AID = 115404857754321

# 账号文件里的 SESSDATA / access_token：sim-*（测试写入）、renewed-*（biliup renew 之后）、tvqr-*（扫码登录之后）
VALID_SESSDATA = {"sim-sessdata", "renewed-sessdata", "tvqr-sessdata"}
VALID_TOKENS = {"sim-access", "renewed-access", "tvqr-access"}

DEFAULTS: dict[str, Any] = {
    "api_delay_ms": 30,             # 普通接口的延迟
    # ---- 登录态
    "nav": "ok",                    # ok / expired（网页登录态失效）/ expired_until_renew（biliup renew 后恢复）/ 412
    "browser_nav": "",              # 非空时页面里的 nav 用它（httpx 检查通过、浏览器里却掉线）
    "token": "ok",                  # oauth2/info：ok / refresh（快到期）/ expired / expired_sim（只有测试写入的
                                    # sim-access 失效，扫码后的新令牌有效）/ 412 / hang
    # ---- 发布页
    "page": "ok",                   # ok / 412（整页风控）/ blank（发布框永远不出来）
    "logged_out": "header",         # 未登录时：header（页头显示登录按钮）/ redirect（跳 passport 登录页）
    "render_delay_ms": 400,         # nav 返回后多久渲染出发布框
    "title_visible": True,
    "pic_multiple": True,           # 选文件框可多选（线上 PicsUploader 是 multiple）
    "pic_autotrigger": True,        # 点「图片」工具立刻弹选文件框（线上行为）
    "upload_ms": 400,               # 每张图 upload_bfs 的耗时
    "upload_fail": [],              # 这些序号（0 起）的图 upload_bfs 返回失败
    "upload_fail_code": -4,
    "timing": "ok",                 # ok / hidden（账号不能定时）/ swallow（设置不生效）/ drop（页面不把定时带进请求）
    "aigc_option": True,            # 发布框有「AI 创作声明」选项
    "spec_always": False,           # 即使有 dy_spec_agreed cookie 也弹规范确认框
    "human_spec": "",               # 有头模式下模拟用户：confirm / cancel（过 human_delay_ms 后点规范确认框的按钮）
    "human_captcha": False,         # 有头模式下模拟用户过了人机验证（create: captcha 时）
    "human_delay_ms": 1500,
    # ---- 发布接口
    "submit_check": 0,              # 发布前检查的返回码
    "create": "ok",                 # ok / reject / 412 / -352 / server_error / hang / no_id / html / captcha / login_popup
    "create_code": 4126021,         # reject 时的 code
    "create_message": "账号未绑定手机",
    "create_delay_ms": 300,
    # ---- TV 扫码登录
    "qr_poll": [86039, 86090, 0],   # 每次轮询依次返回的 code；用完后重复最后一个
    # ---- biliup（fake_biliup.py 读取）
    "biliup": "ok",                 # ok / reject / server_error / login_error / network_error / sso_missing
                                    # crash_after_upload / hang_after_upload / hang_upload / no_bvid
    "biliup_version": "ok",         # ok / fail / hang
    "biliup_upload_seconds": 1.0,   # 上传耗时（期间每 biliup_tick 秒没有输出，与线上管道输出一致）
    "biliup_time": "unknown",       # unknown（Linux 多线程取不到本地时区）/ local（Windows：2026-10-22 18:00:01）
    "biliup_renew": "ok",           # ok / fail
}

_STATE: dict[str, Any] = {"polls": 0, "uploads": 0, "creates": 0, "auth_codes": 0}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = os.environ.get("MULTIPUB_SIM_BILI", "")
    if raw:
        cfg.update(json.loads(raw))
    return cfg


def log_event(kind: str, **data: Any) -> None:
    path = os.environ.get("MULTIPUB_SIM_BILI_LOG")
    if not path:
        return
    row = {"type": kind, "t": round(time.time(), 3), "pid": os.getpid(), **data}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 账号文件（biliup 格式）

def login_info(sessdata: str = "sim-sessdata", token: str = "sim-access", *, csrf: str = "sim-csrf",
               platform: str | None = "BiliTV", expires: int | None = None) -> dict[str, Any]:
    exp = expires if expires is not None else int(time.time()) + 150 * 86400
    cookies = [
        {"name": "SESSDATA", "value": sessdata, "http_only": 1, "expires": exp, "secure": 0},
        {"name": "bili_jct", "value": csrf, "http_only": 0, "expires": exp, "secure": 0},
        {"name": "DedeUserID", "value": str(MID), "http_only": 0, "expires": exp, "secure": 0},
        {"name": "DedeUserID__ckMd5", "value": "a1b2c3d4e5f60718", "http_only": 0, "expires": exp, "secure": 0},
        {"name": "sid", "value": "simsid01", "http_only": 0, "expires": exp, "secure": 0},
    ]
    info: dict[str, Any] = {
        "cookie_info": {"cookies": cookies, "domains": [".bilibili.com", ".biligame.com", ".bigfun.cn",
                                                        ".bigfunapp.cn", ".dreamcast.hk"]},
        "sso": ["https://passport.bilibili.com/api/v2/sso", "https://passport.biligame.com/api/v2/sso",
                "https://passport.bigfunapp.cn/api/v2/sso"],
        "token_info": {"mid": MID, "access_token": token, "refresh_token": token.replace("access", "refresh"),
                       "expires_in": 15552000},
    }
    if platform is not None:
        info["platform"] = platform
    return info


# ------------------------------------------------------------------ 独立实现的签名校验（不引用产品代码）

_MIXIN_TAB = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28,
              14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21,
              56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52)


def _wbi_ok(query_pairs: list[tuple[str, str]]) -> bool:
    """按文档算法从收到的 URL 参数重算 w_rid。对象类参数（w_dyn_req.meta）网页端签名时取 "[object Object]"。"""
    params = {k: v for k, v in query_pairs}
    got = params.pop("w_rid", None)
    if not got or "wts" not in params:
        return False
    mixin = "".join((IMG_KEY + SUB_KEY)[i] for i in _MIXIN_TAB)[:32]
    parts = []
    for key in sorted(params):
        value = params[key]
        if value.startswith("{") and key.startswith("w_"):
            value = "[object Object]"
        value = "".join(ch for ch in value if ch not in "!'()*")
        parts.append(f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='')}")
    return hashlib.md5(("&".join(parts) + mixin).encode()).hexdigest() == got


def _app_sign_ok(params: dict[str, str]) -> bool:
    sign = params.get("sign")
    appsec = APPSECS.get(params.get("appkey", ""))
    if not sign or not appsec:
        return False
    rest = {k: v for k, v in params.items() if k != "sign"}
    query = urllib.parse.urlencode(sorted(rest.items()))
    return hashlib.md5((query + appsec).encode()).hexdigest() == sign


# ------------------------------------------------------------------ 接口

def _json(data: dict[str, Any], status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "application/json; charset=utf-8"}, json.dumps(data, ensure_ascii=False).encode()


def _html(text: str, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "text/html; charset=utf-8"}, text.encode()


PAGE_412 = ("<!DOCTYPE html><html><head><title>出错啦! - bilibili.com</title></head><body>"
            "<div class='error-container'><div class='txt-item err-code'>错误号: 412</div>"
            "<div class='txt-item err-default'>由于触发哔哩哔哩安全风控策略，该次访问请求被拒绝。</div></div></body></html>")


def parse_cookie(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


def _form(body: bytes) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def _multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    msg = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files[str(name)] = (filename, payload)
        else:
            fields[str(name)] = payload.decode("utf-8", "replace")
    return fields, files


def _png_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


async def api(method: str, url: str, headers: dict[str, str], body: bytes, *, via: str
              ) -> tuple[int | None, dict[str, str], bytes]:
    """返回 (HTTP 状态, 头, 内容)；状态为 None 表示请求挂起不回。"""
    cfg = config()
    parts = urllib.parse.urlsplit(url)
    path = parts.path
    query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = dict(query_pairs)
    cookies = parse_cookie(headers.get("cookie", ""))
    logged_in = cookies.get("SESSDATA") in VALID_SESSDATA
    await asyncio.sleep(cfg["api_delay_ms"] / 1000)
    log_event("api", via=via, method=method, path=path)

    if path == "/x/web-interface/nav":
        mode = (cfg["browser_nav"] or cfg["nav"]) if via == "browser" else cfg["nav"]
        if mode == "412":
            return _html(PAGE_412, 412)
        if mode == "expired" or (mode == "expired_until_renew" and cookies.get("SESSDATA") != "renewed-sessdata"):
            logged_in = False
        wbi = {"img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
               "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png"}
        if not logged_in:
            return _json({"code": -101, "message": "账号未登录", "ttl": 1, "data": {"isLogin": False, "wbi_img": wbi}})
        return _json({"code": 0, "message": "0", "ttl": 1,
                      "data": {"isLogin": True, "mid": MID, "uname": UNAME, "wbi_img": wbi}})

    if path == "/x/frontend/finger/spi":
        return _json({"code": 0, "message": "ok", "data": {"b_3": "SIM0B3C5-3DED-06AC-C117-91E6B116FF3E44872infoc",
                                                           "b_4": "SIM0B4C6-36B6-DF78-2A37-F8B52D26ACA644872"}})

    if path == "/x/passport-login/oauth2/info":
        if not _app_sign_ok(query):
            return _json({"code": -3, "message": "API校验密匙错误", "ttl": 1})
        mode = cfg["token"]
        if mode == "412":
            return _html(PAGE_412, 412)
        if mode == "hang":
            return None, {}, b""
        if (mode == "expired" or query.get("access_key") not in VALID_TOKENS
                or (mode == "expired_sim" and query.get("access_key") == "sim-access")):
            return _json({"code": -101, "message": "账号未登录", "ttl": 1})
        return _json({"code": 0, "message": "0", "ttl": 1,
                      "data": {"mid": MID, "access_token": query["access_key"],
                               "expires_in": 86400 if mode == "refresh" else 9000000, "refresh": mode == "refresh"}})

    if path == "/x/passport-tv-login/qrcode/auth_code":
        form = _form(body)
        if method != "POST" or not _app_sign_ok(form) or form.get("appkey") != TV_APPKEY:
            return _json({"code": -3, "message": "API校验密匙错误", "ttl": 1})
        _STATE["auth_codes"] += 1
        code = hashlib.md5(f"sim-{_STATE['auth_codes']}".encode()).hexdigest()
        log_event("qr", step="auth_code", auth_code=code)
        return _json({"code": 0, "message": "0", "ttl": 1, "data": {
            "url": f"https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/auth?auth_code={code}",
            "auth_code": code}})

    if path == "/x/passport-tv-login/qrcode/poll":
        form = _form(body)
        if method != "POST" or not _app_sign_ok(form) or not form.get("auth_code"):
            return _json({"code": -3, "message": "API校验密匙错误", "ttl": 1})
        seq = list(cfg["qr_poll"]) or [86039]
        code = seq[min(_STATE["polls"], len(seq) - 1)]
        _STATE["polls"] += 1
        log_event("qr", step="poll", code=code, auth_code=form["auth_code"])
        if code != 0:
            msg = {86039: "二维码尚未确认", 86090: "二维码已扫码未确认", 86038: "二维码已失效"}.get(code, "请求错误")
            return _json({"code": code, "message": msg, "ttl": 1, "data": None})
        info = login_info("tvqr-sessdata", "tvqr-access", csrf="tvqr-csrf")
        token = info["token_info"]
        return _json({"code": 0, "message": "0", "ttl": 1, "data": {
            "is_new": False, "mid": MID, "access_token": token["access_token"],
            "refresh_token": token["refresh_token"], "expires_in": token["expires_in"], "token_info": token,
            "cookie_info": info["cookie_info"], "sso": info["sso"]}})

    if path == "/x/dynamic/feed/draw/upload_bfs":
        fields, files = _multipart(headers.get("content-type", ""), body)
        index = _STATE["uploads"]
        _STATE["uploads"] += 1
        name, data = files.get("file_up", ("", b""))
        log_event("upload", via=via, index=index, filename=name, size=len(data), biz=fields.get("biz"),
                  csrf_ok=fields.get("csrf") == cookies.get("bili_jct"))
        await asyncio.sleep(cfg["upload_ms"] / 1000)
        if not logged_in:
            return _json({"code": -101, "message": "账号未登录", "ttl": 1})
        if fields.get("csrf") != cookies.get("bili_jct"):
            return _json({"code": -111, "message": "csrf 校验失败", "ttl": 1})
        if not name or (via == "http" and not data):  # 浏览器的 multipart 请求体拿不到文件内容（Chromium 限制）
            return _json({"code": -400, "message": "请求错误", "ttl": 1})
        if index in cfg["upload_fail"]:
            return _json({"code": cfg["upload_fail_code"], "message": "上传失败", "ttl": 1})
        w, h = _png_size(data)
        return _json({"code": 0, "message": "0", "ttl": 1, "data": {
            "image_url": f"http://i0.hdslb.com/bfs/new_dyn/sim{index:02d}{MID}.png", "image_width": w or 1080,
            "image_height": h or 1440, "img_size": round(len(data) / 1024, 3), "ai_gen_pic": 0}})

    if path == "/x/dynamic/feed/create/submit_check":
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        log_event("submit_check", via=via, body=data)
        if query.get("csrf") != cookies.get("bili_jct"):
            return _json({"code": -111, "message": "csrf 校验失败", "ttl": 1})
        code = int(cfg["submit_check"])
        return _json({"code": code, "message": "0" if code == 0 else "内容不符合规范", "ttl": 1})

    if path == "/x/dynamic/feed/create/dyn":
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        _STATE["creates"] += 1
        signed = "w_rid" in query
        wbi_ok = _wbi_ok(query_pairs) if signed else None
        log_event("create", via=via, query=query, body=data, wbi_ok=wbi_ok, n=_STATE["creates"],
                  csrf_ok=query.get("csrf") == cookies.get("bili_jct"))
        await asyncio.sleep(cfg["create_delay_ms"] / 1000)
        mode = cfg["create"]
        if mode == "hang":
            return None, {}, b""
        if mode == "412":
            return _html(PAGE_412, 412)
        if mode == "html":
            return _html("<html><body>502 Bad Gateway</body></html>", 502)
        if not logged_in:
            return _json({"code": -101, "message": "账号未登录", "ttl": 1})
        if query.get("csrf") != cookies.get("bili_jct"):
            return _json({"code": -111, "message": "csrf 校验失败", "ttl": 1})
        if signed and not wbi_ok:
            return _json({"code": -403, "message": "访问权限不足", "ttl": 1})
        option = ((data.get("dyn_req") or {}).get("option") or {})
        timer = option.get("timer_pub_time")
        if timer and int(timer) < time.time() + 300:
            return _json({"code": 4126103, "message": "定时发布时间需晚于当前时间5分钟", "ttl": 1})
        if mode == "reject":
            return _json({"code": cfg["create_code"], "message": cfg["create_message"], "ttl": 1})
        if mode == "-352":
            return _json({"code": -352, "message": "风控校验失败", "ttl": 1, "data": {"v_voucher": "voucher_sim"}})
        if mode == "server_error":
            return _json({"code": -500, "message": "服务器错误", "ttl": 1})
        if mode == "no_id":
            return _json({"code": 0, "message": "0", "ttl": 1, "data": {}})
        return _json({"code": 0, "message": "0", "ttl": 1, "data": {
            "dyn_id": int(DYN_ID), "dyn_id_str": DYN_ID, "dyn_rid": 4321, "dyn_rid_str": "4321", "dyn_type": 2}})

    log_event("api_unknown", via=via, method=method, url=url)
    return _json({"code": -404, "message": "啥都木有", "ttl": 1}, 404)


# ------------------------------------------------------------------ httpx（worker 里的 bili_http）

async def httpx_handler(request: Any) -> Any:
    import httpx

    host = request.url.host
    if host not in ("api.bilibili.com", "passport.bilibili.com"):
        log_event("blocked", via="http", url=str(request.url))
        raise httpx.ConnectError(f"sim: blocked {request.url}", request=request)
    body = await request.aread()
    headers = {k.lower(): v for k, v in request.headers.items()}
    status, hdrs, content = await api(request.method, str(request.url), headers, body, via="http")
    if status is None:
        await asyncio.sleep(3600)
    return httpx.Response(status, headers=hdrs, content=content)


def sync_httpx_handler(request: Any) -> Any:
    import httpx

    log_event("blocked", via="http-sync", url=str(request.url))
    raise httpx.ConnectError(f"sim: blocked {request.url}", request=request)


# ------------------------------------------------------------------ 浏览器路由

def _cors(headers: dict[str, str]) -> dict[str, str]:
    return {"access-control-allow-origin": headers.get("origin") or "https://t.bilibili.com",
            "access-control-allow-credentials": "true",
            "access-control-allow-headers": "content-type",
            "access-control-allow-methods": "GET, POST, OPTIONS"}


def publisher_html() -> str:
    cfg = config()
    page = (PAGES / "t.html").read_text(encoding="utf-8")
    return page.replace("/*__SIM__*/{}", json.dumps(cfg, ensure_ascii=False))


LOGIN_PAGE = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>哔哩哔哩 (゜-゜)つロ 干杯~-bilibili</title></head>"
              "<body><div class='login-scan-box'><div class='login-scan-title'>扫描二维码登录</div></div></body></html>")


async def route_handler(route: Any, request: Any) -> bool:
    url = request.url
    parts = urllib.parse.urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if host == "t.bilibili.com":
        if path.startswith("/__sim/"):
            try:
                data = json.loads(request.post_data or "{}")
            except ValueError:
                data = {}
            data.pop("type", None)
            log_event("page", **data)
            await route.fulfill(status=204, body="")
            return True
        if path in ("", "/"):
            cfg = config()
            log_event("page_load", url=url)
            if cfg["page"] == "412":
                await route.fulfill(status=412, content_type="text/html; charset=utf-8", body=PAGE_412)
            else:
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=publisher_html())
            return True
        if path.startswith("/h5/dynamic/specification"):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body="<html><body><h3>哔哩哔哩动态发布规范</h3></body></html>")
            return True
        return False
    if host == "passport.bilibili.com" and path.startswith("/login"):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=LOGIN_PAGE)
        return True
    if host in ("api.bilibili.com", "passport.bilibili.com"):
        headers = await request.all_headers()
        if request.method == "OPTIONS":
            await route.fulfill(status=204, headers=_cors(headers), body="")
            return True
        body = request.post_data_buffer or b""
        status, hdrs, content = await api(request.method, url, headers, body, via="browser")
        if status is None:
            await asyncio.sleep(3600)
            return True
        await route.fulfill(status=status, headers={**_cors(headers), **hdrs}, body=content)
        return True
    return False


def install() -> None:
    """worker 子进程里：浏览器走 route_handler，httpx 走 MockTransport；其他一律拒绝。"""
    import httpx

    from tests.sim._routing import install as install_routing

    install_routing(route_handler)

    orig_async_init = httpx.AsyncClient.__init__
    orig_sync_init = httpx.Client.__init__

    def async_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(httpx_handler)
        orig_async_init(self, *args, **kwargs)

    def sync_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(sync_httpx_handler)
        orig_sync_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = async_init  # type: ignore[method-assign]
    httpx.Client.__init__ = sync_init  # type: ignore[method-assign]
