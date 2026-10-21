"""B站 Web / TV 接口的最小封装（自己实现，未使用 GPL 的 bilibili-api）。

- WBI 签名（照网页端 um()）：nav 拿 img_key/sub_key -> 固定表重排得 mixin_key -> 参数加 wts 排序、
  字符串值去掉 !'()*、按 encodeURIComponent 规则编码 -> md5(query + mixin_key) = w_rid。
- APP 签名（TV 扫码登录用）：参数按键排序 urlencode 后拼 appsec 取 md5。
- 图文动态 HTTP 模式（实验性）：nav -> upload_bfs（每张图）-> submit_check -> create/dyn。

只在 worker 子进程里导入（依赖 httpx）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import random
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx

from ..models import ErrorCode, LoginRequired, NeedsAction, PublishError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
API = "https://api.bilibili.com"
NAV_URL = f"{API}/x/web-interface/nav"
SPI_URL = f"{API}/x/frontend/finger/spi"
UPLOAD_BFS_URL = f"{API}/x/dynamic/feed/draw/upload_bfs"
SUBMIT_CHECK_URL = f"{API}/x/dynamic/feed/create/submit_check"
CREATE_DYN_URL = f"{API}/x/dynamic/feed/create/dyn"
TV_AUTH_CODE_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
TV_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
OAUTH_INFO_URL = "https://passport.bilibili.com/x/passport-login/oauth2/info"

# 云视听小电视（TV 端）appkey，与 biliup 的扫码登录一致；拿到的 token 能被 biliup 直接使用
TV_APPKEY = "4409e2ce8ffd12b8"
TV_APPSEC = "59b43e04ad6965f34319062b478f83dd"
# biliup 用账号密码 / 短信登录时得到的是 Android 端 token（登录文件 platform 为 Android 或缺省）
ANDROID_APPKEY = "783bbb7264451d82"
ANDROID_APPSEC = "2653583c8873dea268ab9386918b1d65"

MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)
_WBI_STRIP = str.maketrans("", "", "!'()*")

# 网页端 METADATA 中间件附加的 x-bili-device-req-json：UA 含 Mozilla -> device=pc，
# spmid 取 t.bilibili.com 页面的 <meta name="spm_prefix">，create/dyn 额外带 mobi_app=web_cn
WEB_SPMID = "333.1365"
# 网页端 WBI 中间件对 create/dyn 设置了 bodyPayload，把这两个 body 字段加前缀 w_ 拷进 URL 参数一起签名
CREATE_DYN_BODY_PAYLOAD = ("upload_id", "meta")

LOGIN_HINT = "运行 mpub login bilibili --account {account} 重新扫码登录"
HTTP_MODE_RISK_HINT = "稍后再试，或改用默认的浏览器模式（去掉 platforms.bilibili.mode: http）"
# 服务端内部错误 / 超时：请求可能已经处理，发布接口遇到这些码按"结果未知"处理
SERVER_ERROR_CODES = frozenset({-500, -502, -503, -504})


def opus_url(dyn_id: str) -> str:
    return f"https://www.bilibili.com/opus/{dyn_id}"


# ------------------------------------------------------------------ 签名

def _param_str(value: Any) -> str:
    """参数在 URL 里的取值：对象 / 数组按网页端请求库的做法 JSON.stringify。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _js_string(value: Any) -> str:
    """参数参与 WBI 签名时的取值，等同 JS 的 String(value)。

    网页端签名直接 encodeURIComponent(value)：对象会变成 "[object Object]"（URL 里发的却是 JSON），
    这里照做，保证与浏览器算出的 w_rid 一致。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "[object Object]"
    if isinstance(value, (list, tuple)):
        return ",".join("" if v is None else _js_string(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def encode_query(params: dict[str, Any]) -> str:
    """按 encodeURIComponent 规则编码（空格是 %20，不是 +），保持传入顺序；值为 None 的跳过。"""
    return "&".join(f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(_param_str(v), safe='')}"
                    for k, v in params.items() if v is not None)


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


def wbi_keys(nav_data: dict[str, Any]) -> tuple[str, str]:
    img = nav_data.get("wbi_img") or {}

    def key(url: str) -> str:
        return url.rsplit("/", 1)[-1].split(".", 1)[0]

    img_key, sub_key = key(str(img.get("img_url") or "")), key(str(img.get("sub_url") or ""))
    if len(img_key) < 32 or len(sub_key) < 32:
        raise PublishError("nav 接口没有返回 WBI 密钥", code=ErrorCode.PLATFORM_ERROR)
    return img_key, sub_key


def wbi_sign(params: dict[str, Any], img_key: str, sub_key: str, *, wts: int | None = None) -> dict[str, Any]:
    """返回追加了 wts / w_rid 的新参数字典。

    与网页端 um() 一致：加 wts、按键排序、跳过 None、只对字符串值去掉 !'()*、
    encodeURIComponent 后拼接，w_rid = md5(query + mixin_key)。发送的仍是原始值。
    """
    signed = {k: v for k, v in params.items() if v is not None}
    signed["wts"] = int(wts if wts is not None else time.time())
    parts = []
    for key in sorted(signed):
        value = signed[key]
        text = value.translate(_WBI_STRIP) if isinstance(value, str) else _js_string(value)
        parts.append(f"{urllib.parse.quote(str(key), safe='')}={urllib.parse.quote(text, safe='')}")
    signed["w_rid"] = hashlib.md5(("&".join(parts) + mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
    return signed


def device_req_json(mobi_app: str | None = None) -> str:
    """网页端 METADATA 中间件的 x-bili-device-req-json（JSON.stringify 的紧凑格式）。"""
    data = {"platform": "web", "device": "pc", "spmid": WEB_SPMID}
    if mobi_app:
        data["mobi_app"] = mobi_app
    return json.dumps(data, separators=(",", ":"))


def app_sign(params: dict[str, Any], appsec: str = TV_APPSEC) -> dict[str, Any]:
    signed = {k: params[k] for k in sorted(params)}
    query = urllib.parse.urlencode({k: _param_str(v) for k, v in signed.items()})
    signed["sign"] = hashlib.md5((query + appsec).encode("utf-8")).hexdigest()
    return signed


# ------------------------------------------------------------------ 客户端与通用请求

def make_client(cookies: dict[str, str] | None = None, *, referer: str = "https://t.bilibili.com/",
                transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30) -> httpx.AsyncClient:
    jar = httpx.Cookies()
    for name, value in (cookies or {}).items():
        jar.set(name, value, domain=".bilibili.com", path="/")
    origin = "://".join(urllib.parse.urlsplit(referer)[:2])
    return httpx.AsyncClient(
        cookies=jar, transport=transport, timeout=timeout, follow_redirects=True,
        headers={"User-Agent": UA, "Referer": referer, "Origin": origin,
                 "Accept": "application/json, text/plain, */*"},
    )


def cookie_value(client: httpx.AsyncClient, name: str) -> str | None:
    for c in client.cookies.jar:
        if c.name == name:
            return c.value
    return None


def _risk(what: str, hint: str = "稍后再试") -> NeedsAction:
    return NeedsAction(f"{what}被B站风控拦截（412/-352）：{hint}", code=ErrorCode.RISK_CONTROL)


async def request_json(client: httpx.AsyncClient, method: str, url: str, *, what: str,
                       risk_hint: str = "稍后再试", **kwargs: Any) -> dict[str, Any]:
    """发请求并解析 JSON。412 / -352 -> 风控；网络错误 / 非 JSON -> PublishError（可重试，调用方决定）。"""
    try:
        resp = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise PublishError(f"{what}：网络请求失败（{type(exc).__name__}: {exc}）", retryable=True) from exc
    if resp.status_code == 412:
        raise _risk(what, risk_hint)
    try:
        data = resp.json()
    except ValueError as exc:
        raise PublishError(f"{what}返回了非 JSON（HTTP {resp.status_code}）", retryable=True) from exc
    if not isinstance(data, dict):
        raise PublishError(f"{what}返回格式异常", retryable=True)
    if data.get("code") == -352:
        raise _risk(what, risk_hint)
    return data


def raise_for_code(data: dict[str, Any], *, what: str, account: str) -> None:
    code = data.get("code")
    if code in (0, None):
        return
    message = str(data.get("message") or data.get("msg") or "")
    if code == -101:
        raise LoginRequired(f"B站登录已失效（{what}）：" + LOGIN_HINT.format(account=account))
    if code == -111:
        raise LoginRequired(f"B站 csrf 校验失败（{what}）：" + LOGIN_HINT.format(account=account))
    if code == 4126021:
        raise PublishError("账号没有绑定手机，B站不允许发布动态：先在 B站 App 里绑定手机号")
    raise PublishError(f"{what}失败（B站返回 {code}: {message}）")


# ------------------------------------------------------------------ 只读接口

async def fetch_nav(client: httpx.AsyncClient) -> dict[str, Any]:
    """GET nav。返回 data（未登录时 data.isLogin=false，code=-101）。"""
    data = await request_json(client, "GET", NAV_URL, what="登录检查")
    nav = data.get("data")
    if not isinstance(nav, dict):
        raise PublishError(f"登录检查返回异常（code {data.get('code')}: {data.get('message')}）", retryable=True)
    return nav


async def ensure_buvid(client: httpx.AsyncClient) -> dict[str, str]:
    """biliup 的 cookie 里没有 buvid3/buvid4，缺了风控分会更高：从 finger/spi（只读）补上。"""
    if cookie_value(client, "buvid3"):
        return {}
    data = await request_json(client, "GET", SPI_URL, what="获取 buvid")
    d = data.get("data") or {}
    added = {k: str(d[src]) for k, src in (("buvid3", "b_3"), ("buvid4", "b_4")) if d.get(src)}
    for name, value in added.items():
        client.cookies.set(name, value, domain=".bilibili.com", path="/")
    return added


# ------------------------------------------------------------------ TV 扫码登录

async def tv_qr_start(client: httpx.AsyncClient) -> tuple[str, str]:
    form = app_sign({"appkey": TV_APPKEY, "local_id": "0", "ts": int(time.time())})
    data = await request_json(client, "POST", TV_AUTH_CODE_URL, data=form, what="申请登录二维码")
    d = data.get("data") or {}
    if data.get("code") != 0 or not d.get("url") or not d.get("auth_code"):
        raise PublishError(f"申请登录二维码失败（{data.get('code')}: {data.get('message')}）", retryable=True)
    return str(d["url"]), str(d["auth_code"])


async def tv_qr_poll(client: httpx.AsyncClient, auth_code: str) -> tuple[int, dict[str, Any] | None]:
    """返回 (code, data)。0 成功；86039 未扫码；86090 已扫未确认；86038 二维码过期。"""
    form = app_sign({"appkey": TV_APPKEY, "auth_code": auth_code, "local_id": "0", "ts": int(time.time())})
    data = await request_json(client, "POST", TV_POLL_URL, data=form, what="查询扫码状态")
    code = int(data.get("code", -1))
    payload = data.get("data") if isinstance(data.get("data"), dict) else None
    return code, payload


async def oauth_token_info(client: httpx.AsyncClient, access_token: str, platform: str | None,
                           *, account: str) -> dict[str, Any]:
    """与 biliup 上传前的 validate_tokens 相同的只读校验。返回 data（含 refresh：是否该续期）。"""
    if platform == "BiliTV":
        appkey, appsec = TV_APPKEY, TV_APPSEC
    elif platform in (None, "", "Android"):
        appkey, appsec = ANDROID_APPKEY, ANDROID_APPSEC
    else:
        raise LoginRequired(f"登录文件的 platform={platform!r} 不被 biliup 支持：" + LOGIN_HINT.format(account=account))
    params = app_sign({"access_key": access_token, "actionKey": "appkey", "appkey": appkey,
                       "ts": int(time.time())}, appsec)
    data = await request_json(client, "GET", OAUTH_INFO_URL, params=params, what="校验 biliup 登录令牌")
    info = data.get("data")
    if data.get("code") != 0 or not isinstance(info, dict) or not info.get("mid"):
        raise LoginRequired(f"biliup 登录令牌已失效（B站返回 {data.get('code')}: {data.get('message')}），"
                            "视频无法上传：" + LOGIN_HINT.format(account=account))
    return info


# ------------------------------------------------------------------ 图文动态（HTTP 模式）

def gen_upload_id(mid: int | str) -> str:
    return f"{mid}_{int(time.time())}_{random.randint(0, 9999)}"


def build_dyn_req(*, text: str, title: str, pics: list[dict[str, Any]], mid: int | str,
                  aigc: bool = False, pic_mode: int = 0, timer_pub_time: int | None = None,
                  upload_id: str | None = None) -> dict[str, Any]:
    """与网页端发布器（t.bilibili.com）构造的 dyn_req 结构一致。"""
    contents = [{"raw_text": text, "type": 1, "biz_id": ""}] if text else []
    content: dict[str, Any] = {"contents": contents}
    if title:
        content["title"] = title
    option: dict[str, Any] = {"aigc": 1 if aigc else 2}
    if pics:
        option["pic_mode"] = int(pic_mode)
    if timer_pub_time:
        option["timer_pub_time"] = int(timer_pub_time)
    return {
        "content": content,
        "pics": pics,
        "scene": 2,
        "upload_id": upload_id or gen_upload_id(mid),
        "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
        "option": option,
    }


async def upload_image(client: httpx.AsyncClient, path: Path, csrf: str, *, account: str) -> dict[str, Any]:
    """传一张图，返回 dyn_req.pics 里的一项（字段与网页端相同：img_size 是本地文件 KB 数）。"""
    content = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    files = {"file_up": (path.name, content, mime)}
    form = {"biz": "dyn", "category": "daily", "csrf": csrf}
    data = await request_json(client, "POST", UPLOAD_BFS_URL, data=form, files=files,
                              what=f"上传图片 {path.name}", risk_hint=HTTP_MODE_RISK_HINT, timeout=120)
    raise_for_code(data, what=f"上传图片 {path.name}", account=account)
    d = data.get("data") or {}
    if not d.get("image_url"):
        raise PublishError(f"上传图片 {path.name} 没有返回地址", retryable=True)
    pic = {"img_src": d["image_url"], "img_width": d.get("image_width", 0),
           "img_height": d.get("image_height", 0), "img_size": len(content) / 1024}
    if "ai_gen_pic" in d:
        pic["ai_gen_pic"] = d["ai_gen_pic"]
    return pic


async def submit_check(client: httpx.AsyncClient, dyn_req: dict[str, Any], csrf: str, *, account: str) -> None:
    body = {"content": dyn_req["content"], "pics": dyn_req["pics"], "scene": dyn_req["scene"],
            "create_option": dyn_req["option"]}
    url = f"{SUBMIT_CHECK_URL}?{encode_query({'csrf': csrf, 'x-bili-device-req-json': device_req_json()})}"
    data = await request_json(client, "POST", url, what="发布前检查", content=_json_bytes(body),
                              headers={"Content-Type": "application/json"}, risk_hint=HTTP_MODE_RISK_HINT)
    raise_for_code(data, what="发布前检查", account=account)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def create_dyn_query(csrf: str, keys: tuple[str, str], dyn_req: dict[str, Any], *, wts: int | None = None) -> str:
    """create/dyn 的 URL 参数，带 WBI 签名（参数组成照网页端 2026-10 的中间件链）。

    - x-bili-device-req-json：METADATA 中间件。
    - dm_img_*：RISK_USER_LOG 中间件；这里用"没有鼠标键盘轨迹"的取值，dm_img_str / dm_cover_img_str
      是 WebGL 版本与显卡名的 base64（固定值）。
    - w_dyn_req.upload_id / w_dyn_req.meta：WBI 中间件的 bodyPayload（meta 是对象：签名按 "[object Object]"，
      URL 里发 JSON，与浏览器一致）。
    网页端还会带 b_wet 环境令牌（WASM 生成，这里无法提供）和 x-bili-locale-json，
    所以本模式可能被风控（412 / -352），届时请改用浏览器模式。
    """
    params: dict[str, Any] = {
        "platform": "web",
        "csrf": csrf,
        "x-bili-device-req-json": device_req_json("web_cn"),
        "dm_img_list": "[]",
        "dm_img_str": _b64("WebGL 1.0 (OpenGL ES 2.0 Chromium)"),
        "dm_cover_img_str": _b64("ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)"),
        "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
    }
    for field_name in CREATE_DYN_BODY_PAYLOAD:
        if dyn_req.get(field_name) is not None:
            params[f"w_dyn_req.{field_name}"] = dyn_req[field_name]
    return encode_query(wbi_sign(params, *keys, wts=wts))


async def create_dyn(client: httpx.AsyncClient, dyn_req: dict[str, Any], csrf: str, keys: tuple[str, str],
                     *, account: str) -> dict[str, Any]:
    """真正发布。调用前必须已经 mark_submit_started；结果不明一律 UNKNOWN_OUTCOME。"""
    url = f"{CREATE_DYN_URL}?{create_dyn_query(csrf, keys, dyn_req)}"
    try:
        resp = await client.post(url, content=_json_bytes({"dyn_req": dyn_req}),
                                 headers={"Content-Type": "application/json"}, timeout=60)
    except httpx.HTTPError as exc:
        raise PublishError(f"发布请求已发出但没有收到结果（{type(exc).__name__}）：先到 B站动态页确认，不要直接重发",
                           code=ErrorCode.UNKNOWN_OUTCOME) from exc
    if resp.status_code == 412:
        raise _risk("发布动态", HTTP_MODE_RISK_HINT)  # 网关直接拦截，请求没进业务层
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise PublishError(f"发布接口返回了非 JSON（HTTP {resp.status_code}）：先到 B站动态页确认，不要直接重发",
                           code=ErrorCode.UNKNOWN_OUTCOME)
    if data.get("code") == -352:
        raise _risk("发布动态", HTTP_MODE_RISK_HINT)
    if data.get("code") in SERVER_ERROR_CODES:
        raise PublishError(f"发布接口返回服务端错误（{data.get('code')}: {data.get('message')}），可能已经发出："
                           "先到 B站动态页确认，不要直接重发", code=ErrorCode.UNKNOWN_OUTCOME)
    raise_for_code(data, what="发布动态", account=account)
    d = data.get("data") or {}
    dyn_id = str(d.get("dyn_id_str") or d.get("dyn_id") or "")
    if not dyn_id:
        raise PublishError("发布接口返回成功但没有动态 id：先到 B站动态页确认，不要直接重发",
                           code=ErrorCode.UNKNOWN_OUTCOME)
    d["dyn_id_str"] = dyn_id
    return d


async def publish_note_http(client: httpx.AsyncClient, *, csrf: str, account: str, mid: int | str,
                            keys: tuple[str, str], title: str, text: str, images: list[Path],
                            aigc: bool, pic_mode: int, timer_pub_time: int | None,
                            mark_submit: Callable[[], None], log: Callable[[str], None]) -> dict[str, Any]:
    pics = []
    for i, img in enumerate(images, start=1):
        log(f"上传图片 {i}/{len(images)}: {img.name}")
        pics.append(await upload_image(client, img, csrf, account=account))
    dyn_req = build_dyn_req(text=text, title=title, pics=pics, mid=mid, aigc=aigc,
                            pic_mode=pic_mode, timer_pub_time=timer_pub_time)
    await submit_check(client, dyn_req, csrf, account=account)
    mark_submit()
    return await create_dyn(client, dyn_req, csrf, keys, account=account)
