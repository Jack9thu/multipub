"""知乎 Web 接口的最小封装：cookie + x-xsrftoken，不做 x-zse-96 签名。

接口与请求体来自 zhihu_obsidian（0BSD）、KrisTHL181/zhihu-cli（MIT）、BAIGUANGMEI/zhihu-cli（Apache-2.0）
的公开代码，本文件为独立实现。2026 年有两个独立客户端在写接口上不带 x-zse-96 也能成功；
全部未在真实账号上验证，见 docs/platforms/zhihu.md。

- 登录检查：GET www/api/v4/me
- 文章：POST zhuanlan/api/articles/drafts → PATCH …/<id>/draft → (话题) → POST www/api/v4/content/publish
- 想法：POST www/api/v4/content/drafts {"action":"pin"} → POST www/api/v4/content/publish
- 图片：POST api.zhihu.com/images → (state=2 时) 阿里云 OSS PUT（v1 签名，bucket zhihu-pics）
  → 轮询 GET api.zhihu.com/images/<id>
- 风控：HTTP 403 + error.code 40352 → 需要人在浏览器里过验证（RiskControl）

只在 worker 子进程里导入（依赖 httpx / Pillow）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import time
import uuid
from dataclasses import dataclass
from email.utils import formatdate
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any, Callable

import httpx

from ..models import ErrorCode, LoginRequired, NeedsAction, PublishError, ValidationFailed
from .zhihu_html import ZhihuImage

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
OSS_UA = "aliyun-sdk-js/6.8.0 Chrome 141.0.0.0 on Windows 10 64-bit"

WWW = "https://www.zhihu.com"
WWW_HOME = f"{WWW}/"
ZHUANLAN = "https://zhuanlan.zhihu.com"
API_HOST = "https://api.zhihu.com"
OSS_HOST = "https://zhihu-pics-upload.zhimg.com"
OSS_BUCKET = "zhihu-pics"  # 注意不是 zhihu-pics-upload

SIGNIN_URL = f"{WWW}/signin"
ME_URL = f"{WWW}/api/v4/me"
CONTENT_DRAFTS_URL = f"{WWW}/api/v4/content/drafts"
PUBLISH_URL = f"{WWW}/api/v4/content/publish"
ARTICLE_DRAFTS_URL = f"{ZHUANLAN}/api/articles/drafts"
TOPIC_AUTOCOMPLETE_URL = f"{ZHUANLAN}/api/autocomplete/topics"
IMAGES_URL = f"{API_HOST}/images"
RISK_FALLBACK_URL = f"{WWW}/account/risk_control/"

RISK_CODE = 40352
SIGNATURE_CODE = 10003  # 「请求参数异常，请升级客户端后重试」：通常是缺 x-zse-96 签名
AUTH_MARKERS = ("ERR_TICKET_NOT_EXIST", "ZERR_NO_AUTH_TOKEN")

DEFAULT_WRITE_INTERVAL = 2.0  # 两次写请求之间至少间隔（秒），config.toml [platforms.zhihu] write_interval
DEFAULT_IMAGE_TIMEOUT = 90.0  # 单张图片等知乎处理完成的最长时间（秒）
IMAGE_POLL_INTERVAL = 1.5
REMOTE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
PASS_THROUGH_FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png")}

LogFn = Callable[..., None]


def login_hint(account: str) -> str:
    return f"运行 mpub login zhihu --account {account}"


def article_url(article_id: str) -> str:
    return f"{ZHUANLAN}/p/{article_id}"


def article_edit_url(article_id: str) -> str:
    return f"{ZHUANLAN}/p/{article_id}/edit"


def pin_url(pin_id: str) -> str:
    return f"{WWW}/pin/{pin_id}"


def trace_id() -> str:
    """content/publish 的 traceId："<毫秒时间戳>,<uuid4>"。"""
    return f"{int(time.time() * 1000)},{uuid.uuid4()}"


class RiskControl(NeedsAction):
    """知乎风控（40352 等）：需要人打开 url 完成验证。"""

    code = ErrorCode.RISK_CONTROL

    def __init__(self, message: str, *, url: str = "", **kw: Any):
        super().__init__(message, **kw)
        self.url = url


# ------------------------------------------------------------------ cookie

def normalize_cookies(data: Any) -> list[dict[str, Any]]:
    """接受 patchright storage_state、浏览器插件导出的 cookie 列表，或 {name: value} 字典。"""
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        items: list[Any] = data["cookies"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [{"name": k, "value": v} for k, v in data.items() if isinstance(v, str)]
    else:
        items = []
    out: list[dict[str, Any]] = []
    for c in items:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        try:
            expires = float(c.get("expires", c.get("expirationDate", -1)) or -1)
        except (TypeError, ValueError):
            expires = -1.0
        out.append({
            "name": str(c["name"]),
            "value": str(c.get("value", "")),
            "domain": str(c.get("domain") or ".zhihu.com"),
            "path": str(c.get("path") or "/"),
            "expires": expires,
            "secure": bool(c.get("secure", False)),
        })
    return out


def load_cookies(path: Path, *, account: str) -> list[dict[str, Any]]:
    hint = login_hint(account)
    if not path.is_file():
        raise LoginRequired(f"知乎账号 {account} 还没有登录：{hint}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LoginRequired(f"知乎登录文件无法读取（{exc}）：{hint}") from exc
    cookies = normalize_cookies(data)
    z_c0 = next((c for c in cookies if c["name"] == "z_c0" and c["value"]), None)
    if z_c0 is None:
        raise LoginRequired(f"知乎登录文件里没有 z_c0（登录没有完成）：{hint}")
    if 0 < z_c0["expires"] < time.time():
        raise LoginRequired(f"知乎登录已过期：{hint}")
    return cookies


def cookie_days_left(cookies: list[dict[str, Any]], name: str = "z_c0") -> float | None:
    for c in cookies:
        if c["name"] == name and c["expires"] > 0:
            return (c["expires"] - time.time()) / 86400
    return None


def build_jar(cookies: list[dict[str, Any]]) -> httpx.Cookies:
    """按域名存 cookie：.zhihu.com 的会发给 www / zhuanlan / api，不会发给 zhimg.com 图床。"""
    jar = httpx.Cookies()
    for c in cookies:
        domain = c["domain"]
        expires = int(c["expires"]) if c["expires"] > 0 else None
        jar.jar.set_cookie(Cookie(
            version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=c["path"], path_specified=True, secure=c["secure"], expires=expires,
            discard=expires is None, comment=None, comment_url=None, rest={},
        ))
    return jar


def make_client(cookies: list[dict[str, Any]] | None = None, *,
                transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        cookies=build_jar(cookies or []), transport=transport, timeout=timeout, follow_redirects=False,
        headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )


def make_download_client(*, transport: httpx.AsyncBaseTransport | None = None,
                         timeout: float = 30) -> httpx.AsyncClient:
    """下载正文里的远程图片用：不带知乎 cookie。"""
    return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True,
                             headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"})


# ------------------------------------------------------------------ 阿里云 OSS v1 签名

def oss_signature(access_key: str, *, method: str, content_type: str, date: str,
                  oss_headers: dict[str, str], resource: str, content_md5: str = "") -> str:
    """StringToSign = VERB\\nContent-MD5\\nContent-Type\\nDate\\nCanonicalizedOSSHeaders + CanonicalizedResource。"""
    canonical = "".join(f"{k}:{v.strip()}\n" for k, v in sorted((k.lower(), v) for k, v in oss_headers.items()))
    to_sign = f"{method}\n{content_md5}\n{content_type}\n{date}\n{canonical}{resource}"
    digest = hmac.new(access_key.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def oss_put_headers(token: dict[str, Any], object_key: str, content_type: str, date: str) -> dict[str, str]:
    oss = {"x-oss-date": date, "x-oss-security-token": str(token["access_token"]), "x-oss-user-agent": OSS_UA}
    sig = oss_signature(str(token["access_key"]), method="PUT", content_type=content_type, date=date,
                        oss_headers=oss, resource=f"/{OSS_BUCKET}/{object_key}")
    return {
        "Content-Type": content_type,
        "Date": date,
        **oss,
        "Authorization": f"OSS {token['access_id']}:{sig}",
        "Origin": ZHUANLAN,
        "Referer": f"{ZHUANLAN}/",
    }


# ------------------------------------------------------------------ 图片预处理

@dataclass
class PreparedImage:
    name: str
    data: bytes
    mime: str
    ext: str
    width: int
    height: int
    converted_from: str = ""  # 非空表示从该格式转成了 JPEG


def prepare_image(raw: bytes, name: str) -> PreparedImage:
    """JPEG / PNG 原样上传；GIF（只取第一帧）、WebP、BMP 等转成 JPEG。"""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as im:
            fmt = (im.format or "").upper()
            width, height = im.size
            if fmt in PASS_THROUGH_FORMATS:
                mime, ext = PASS_THROUGH_FORMATS[fmt]
                return PreparedImage(name, raw, mime, ext, width, height)
            im.seek(0)
            frame = im.convert("RGBA")
            canvas = Image.new("RGB", frame.size, (255, 255, 255))
            canvas.paste(frame, mask=frame.getchannel("A"))
            buf = io.BytesIO()
            canvas.save(buf, "JPEG", quality=92)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationFailed(f"{name} 不是可识别的图片（{exc}）") from exc
    return PreparedImage(name, buf.getvalue(), "image/jpeg", "jpg", width, height, converted_from=fmt or "unknown")


async def download_image(url: str, *, max_bytes: int = REMOTE_IMAGE_MAX_BYTES) -> bytes:
    full = "https:" + url if url.startswith("//") else url
    try:
        async with make_download_client() as client:
            async with client.stream("GET", full) as resp:
                if resp.status_code != 200:
                    raise ValidationFailed(f"下载远程图片失败（HTTP {resp.status_code}）: {url}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValidationFailed(f"远程图片超过 {max_bytes // 1024 // 1024}MB: {url}")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise PublishError(f"下载远程图片失败（{type(exc).__name__}: {exc}）: {url}", retryable=True) from exc
    return b"".join(chunks)


# ------------------------------------------------------------------ 响应检查

def _json(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _snippet(resp: httpx.Response) -> str:
    try:
        return resp.text[:2000]
    except (UnicodeDecodeError, LookupError):
        return ""


def error_message(data: Any, fallback: str = "") -> str:
    """知乎错误信息：error.message / message / toast_message / msg。"""
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        for src in (err, data):
            for key in ("message", "toast_message", "msg"):
                if src.get(key):
                    return str(src[key])
    return fallback[:200]


def risk_error(info: dict[str, Any], *, what: str, account: str) -> RiskControl:
    redirect = str(info.get("redirect") or "")
    url = (WWW + redirect) if redirect.startswith("/") else (redirect or RISK_FALLBACK_URL)
    detail = str(info.get("message") or "").strip()
    return RiskControl(
        f"{what}被知乎风控拦截（{RISK_CODE}{'：' + detail if detail else ''}）。需要人工验证：{login_hint(account)}"
        f"（会用已保存的登录态打开验证页，验证完自动保存），然后再发布；验证页: {url}",
        url=url,
    )


def raise_for_response(resp: httpx.Response, data: Any, *, what: str, account: str,
                       after_submit: bool = False, check_code: bool = False) -> None:
    """把知乎的各种失败映射成 LoginRequired / RiskControl / PublishError。成功时什么也不做。"""
    status = resp.status_code
    body = _snippet(resp) if status >= 300 else ""
    err = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else {}
    if status == 401 or (status >= 400 and any(m in body for m in AUTH_MARKERS)):
        raise LoginRequired(f"知乎登录已失效（{what}）：{login_hint(account)}")
    if err.get("code") == RISK_CODE or (isinstance(data, dict) and data.get("code") == RISK_CODE):
        raise risk_error(err or data, what=what, account=account)
    if status == 403 and "zh-zse-ck" in body:
        raise RiskControl(f"{what}被知乎要求浏览器安全验证（zh-zse-ck）：{login_hint(account)} 后重试",
                          url=WWW_HOME)
    if status == 429:
        raise NeedsAction(f"{what}：请求太频繁被知乎限流，过一段时间再试", code=ErrorCode.RISK_CONTROL)
    if 300 <= status < 400:
        location = resp.headers.get("location", "")
        if "signin" in location or "login" in location:
            raise LoginRequired(f"知乎登录已失效（{what} 被跳转到登录页）：{login_hint(account)}")
    if after_submit and (status >= 500 or 300 <= status < 400 or (status < 300 and data is None)):
        # 4xx 是明确拒绝；5xx / 跳转 / 200 但不是 JSON 都可能已经发出去了
        raise PublishError(f"{what}请求已发出但结果不明（HTTP {status}）：先到知乎确认是否已发布，不要直接重发",
                           code=ErrorCode.UNKNOWN_OUTCOME)
    if status >= 500 or 300 <= status < 400:
        raise PublishError(f"{what}失败（HTTP {status}），可稍后重试", retryable=True)
    if status >= 400:
        message = error_message(data, body)
        hint = ""
        if err.get("code") == SIGNATURE_CODE or "升级客户端" in message:
            hint = "；知乎可能开始要求请求签名（x-zse-96），见 docs/platforms/zhihu.md「已知风险」"
        raise PublishError(f"{what}失败（HTTP {status}: {message}）{hint}")
    if check_code and isinstance(data, dict) and data.get("code") not in (None, 0, "0"):
        raise PublishError(f"{what}失败（知乎返回 {data.get('code')}: {error_message(data)}）")


# ------------------------------------------------------------------ 发布请求体

def article_publish_payload(*, draft_id: str, title: str, html: str, text_len: int,
                            disclaimer_type: str = "none", toc: bool = False,
                            column: str | None = None) -> dict[str, Any]:
    """文章 content/publish 请求体（合并 zhihu_obsidian / KrisTHL181 / 公开抓包的字段）。"""
    disclaimer_status = "close" if disclaimer_type == "none" else "open"
    business: dict[str, Any] = {
        "column": None,
        "commentPermission": "anyone",
        "disclaimer_type": disclaimer_type,
        "disclaimer_status": disclaimer_status,
        "table_of_contents_enabled": toc,
        "commercial_report_info": {"commercial_types": []},
        "commercial_zhitask_bind_info": None,
        "canReward": False,
        "title": title,
        "content": html,
    }
    if column:
        # 选专栏时不带 column: null（与已知能发到专栏的请求一致），专栏放在 data.column
        business.pop("column")
    data: dict[str, Any] = {
        "publish": {"traceId": trace_id()},
        "extra_info": {"publisher": "pc", "pc_business_params": json.dumps(business, ensure_ascii=False)},
        "draft": {"disabled": 1, "id": draft_id, "isPublished": False},
        "commentsPermission": {"comment_permission": "anyone"},
        "creationStatement": {"disclaimer_type": disclaimer_type, "disclaimer_status": disclaimer_status},
        "contentsTables": {"table_of_contents_enabled": toc},
        "commercialReportInfo": {"isReport": 0},
        "appreciate": {"can_reward": False, "tagline": ""},
        "hybridInfo": {},
        "hybrid": {"html": html, "textLength": text_len},
        "title": {"title": title},
    }
    if column:
        data["column"] = {"column": [column]}
    return {"action": "article", "data": data}


def pin_publish_payload(*, draft_id: str, title: str, html: str, text_len: int,
                        images: list[ZhihuImage]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "publish": {"traceId": trace_id()},
        "commentsPermission": {"comment_permission": "all"},
        "extra_info": {"view_permission": "all", "publisher": "pc"},
        "draft": {"disabled": 1, "id": draft_id},
        "title": {"title": title},  # 接口要求带上，可以为空
        "hybrid": {"html": html, "textLength": text_len},
    }
    if images:
        data["media"] = {"medias": [{"image": {
            "width": img.width, "height": img.height, "url": img.src,
            "originalUrl": img.original_src or img.src, "watermark": img.watermark,
            "watermarkUrl": img.watermark_src,
        }} for img in images]}
    return {"action": "pin", "data": data}


def publish_code(data: Any) -> Any:
    return data.get("code") if isinstance(data, dict) else None


def parse_publish_id(data: Any) -> str:
    """content/publish 成功时 data.result 是 JSON 字符串，id 在 result.publish.id 或 result.id。"""
    if not isinstance(data, dict):
        return ""
    payload = data.get("data")
    if not isinstance(payload, dict):
        return ""
    result: Any = payload.get("result")
    if isinstance(result, str) and result:
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if isinstance(result, dict):
        pub = result.get("publish")
        for candidate in ((pub or {}).get("id") if isinstance(pub, dict) else None, result.get("id")):
            if candidate:
                return str(candidate)
    return str(payload.get("id") or "")


# ------------------------------------------------------------------ 客户端

class ZhihuApi:
    def __init__(self, client: httpx.AsyncClient, *, account: str,
                 write_interval: float = DEFAULT_WRITE_INTERVAL,
                 image_timeout: float = DEFAULT_IMAGE_TIMEOUT, log: LogFn | None = None):
        self.client = client
        self.account = account
        self.write_interval = max(0.0, write_interval)
        self.image_timeout = image_timeout
        self.log = log or (lambda *a, **k: None)
        self._last_write = 0.0
        self._image_cache: dict[tuple[str, str], ZhihuImage] = {}

    @property
    def xsrf(self) -> str:
        for c in self.client.cookies.jar:
            if c.name == "_xsrf":
                return c.value or ""
        return ""

    def _headers(self, method: str, *, origin: str, referer: str, has_body: bool) -> dict[str, str]:
        h = {"Referer": referer, "x-requested-with": "fetch"}
        if method != "GET":
            h["Origin"] = origin
        if self.xsrf:
            h["x-xsrftoken"] = self.xsrf
        if has_body:
            h["Content-Type"] = "application/json"
        return h

    async def _throttle(self) -> None:
        """写请求之间至少间隔 write_interval 秒（知乎风控对频率敏感）。"""
        wait = self._last_write + self.write_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_write = time.monotonic()

    async def call(self, method: str, url: str, *, what: str, origin: str = WWW, referer: str = WWW_HOME,
                   body: Any = None, params: dict[str, Any] | None = None, check_code: bool = False,
                   allow_404: bool = False) -> Any:
        if method != "GET":
            await self._throttle()
        content = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = self._headers(method, origin=origin, referer=referer, has_body=content is not None)
        try:
            resp = await self.client.request(method, url, params=params, content=content, headers=headers)
        except httpx.HTTPError as exc:
            raise PublishError(f"{what}：网络请求失败（{type(exc).__name__}: {exc}）", retryable=True) from exc
        data = _json(resp)
        if allow_404 and resp.status_code == 404:
            return None
        raise_for_response(resp, data, what=what, account=self.account, check_code=check_code)
        if data is None and resp.content:
            raise PublishError(f"{what}返回了非 JSON（HTTP {resp.status_code}）", retryable=True)
        return data

    # ---- 账号
    async def me(self) -> dict[str, Any]:
        data = await self.call("GET", ME_URL, what="登录检查", params={"include": "is_bind_phone"})
        if not isinstance(data, dict) or not (data.get("id") or data.get("name")):
            raise PublishError("登录检查返回异常（没有用户信息）", retryable=True)
        return data

    # ---- 文章
    async def create_article_draft(self, title: str) -> str:
        data = await self.call("POST", ARTICLE_DRAFTS_URL, what="创建文章草稿", origin=ZHUANLAN,
                               referer=f"{ZHUANLAN}/write",
                               body={"title": title, "delta_time": 0, "can_reward": False})
        draft_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        if not draft_id:
            raise PublishError("创建文章草稿成功但没有返回 id", retryable=True)
        return draft_id

    async def patch_article_draft(self, article_id: str, body: dict[str, Any], *, what: str = "保存草稿") -> None:
        await self.call("PATCH", f"{ZHUANLAN}/api/articles/{article_id}/draft", what=what, origin=ZHUANLAN,
                        referer=article_edit_url(article_id), body=body)

    async def find_topic(self, article_id: str, keyword: str) -> dict[str, Any] | None:
        data = await self.call("GET", TOPIC_AUTOCOMPLETE_URL, what=f"搜索话题「{keyword}」",
                               referer=article_edit_url(article_id),
                               params={"token": keyword, "max_matches": 5, "use_similar": 0, "topic_filter": 1})
        if isinstance(data, dict):
            data = data.get("data")
        topics = [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []
        if not topics:
            return None
        exact = [t for t in topics if str(t.get("name", "")).strip().lower() == keyword.strip().lower()]
        return (exact or topics)[0]

    async def add_article_topic(self, article_id: str, topic: dict[str, Any]) -> None:
        await self.call("POST", f"{ZHUANLAN}/api/articles/{article_id}/topics",
                        what=f"添加话题「{topic.get('name', '')}」", origin=ZHUANLAN,
                        referer=article_edit_url(article_id), body=topic)

    async def get_article(self, article_id: str) -> dict[str, Any] | None:
        data = await self.call("GET", f"{WWW}/api/v4/articles/{article_id}", what="确认文章",
                               referer=article_url(article_id), allow_404=True)
        return data if isinstance(data, dict) else None

    # ---- 想法
    async def create_content_draft(self, action: str) -> str:
        data = await self.call("POST", CONTENT_DRAFTS_URL, what="创建草稿", body={"action": action},
                               check_code=True)
        inner = data.get("data") if isinstance(data, dict) else None
        content_id = str((inner or {}).get("content_id") or "") if isinstance(inner, dict) else ""
        if not content_id:
            raise PublishError(f"创建{action}草稿成功但没有返回 content_id", retryable=True)
        return content_id

    async def get_pin(self, pin_id: str) -> dict[str, Any] | None:
        data = await self.call("GET", f"{WWW}/api/v4/pins/{pin_id}", what="确认想法",
                               referer=pin_url(pin_id), allow_404=True)
        return data if isinstance(data, dict) else None

    # ---- 发布（不可逆：mark_submit 在真正发出请求前一刻调用，之后失败一律不重试）
    async def publish_content(self, payload: dict[str, Any], *, origin: str, referer: str,
                              mark_submit: Callable[[], None]) -> dict[str, Any]:
        await self._throttle()
        what = "发布"
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers("POST", origin=origin, referer=referer, has_body=True)
        mark_submit()
        try:
            resp = await self.client.post(PUBLISH_URL, content=content, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            raise PublishError(f"发布请求已发出但没有收到结果（{type(exc).__name__}）：先到知乎确认，不要直接重发",
                               code=ErrorCode.UNKNOWN_OUTCOME) from exc
        data = _json(resp)
        raise_for_response(resp, data, what=what, account=self.account, after_submit=True)
        if not isinstance(data, dict):
            raise PublishError("发布接口返回格式异常：先到知乎确认是否已发布，不要直接重发",
                               code=ErrorCode.UNKNOWN_OUTCOME)
        return data

    # ---- 图片
    async def upload_image(self, prepared: PreparedImage, *, source: str, referer: str) -> ZhihuImage:
        """登记 → (需要时) OSS 上传 → 等待处理完成。同一次任务里相同图片只传一次。"""
        md5 = hashlib.md5(prepared.data).hexdigest()
        cached = self._image_cache.get((md5, source))
        if cached is not None:
            return cached
        origin = ZHUANLAN if referer.startswith(ZHUANLAN) else WWW
        what = f"登记图片 {prepared.name}"
        reg = await self.call("POST", IMAGES_URL, what=what, origin=origin, referer=referer,
                              body={"image_hash": md5, "source": source})
        upload_file = reg.get("upload_file") if isinstance(reg, dict) else None
        if not isinstance(upload_file, dict) or not upload_file.get("image_id"):
            raise PublishError(f"{what}返回格式异常", retryable=True)
        image_id = str(upload_file["image_id"])
        state = int(upload_file.get("state") or 0)
        if state == 2:
            token = reg.get("upload_token")
            if not isinstance(token, dict) or not {"access_id", "access_key", "access_token"} <= token.keys():
                raise PublishError(f"{what}没有返回上传凭证", retryable=True)
            await self.oss_put(str(upload_file.get("object_key") or f"v2-{md5}"), prepared, token)
        elif state != 1:  # 1 = 知乎已有这张图，跳过上传
            raise PublishError(f"{what}返回未知状态 {state}", retryable=True)
        info = await self.wait_image(image_id, prepared.name, referer=referer)
        img = ZhihuImage(
            src=str(info["src"]), original_src=str(info.get("original_src") or info["src"]),
            watermark=str(info.get("watermark") or "watermark"), watermark_src=str(info.get("watermark_src") or ""),
            width=prepared.width, height=prepared.height, ext=prepared.ext, image_id=image_id, md5=md5,
        )
        self._image_cache[(md5, source)] = img
        return img

    async def oss_put(self, object_key: str, prepared: PreparedImage, token: dict[str, Any]) -> None:
        headers = oss_put_headers(token, object_key, prepared.mime, formatdate(usegmt=True))
        try:
            resp = await self.client.put(f"{OSS_HOST}/{object_key}", content=prepared.data, headers=headers,
                                         timeout=120)
        except httpx.HTTPError as exc:
            raise PublishError(f"上传图片 {prepared.name} 失败（{type(exc).__name__}: {exc}）", retryable=True) from exc
        if resp.status_code != 200:
            raise PublishError(f"上传图片 {prepared.name} 到知乎图床失败（HTTP {resp.status_code}: "
                               f"{_snippet(resp)[:200]}）", retryable=True)

    async def wait_image(self, image_id: str, name: str, *, referer: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.image_timeout
        while True:
            info = await self.call("GET", f"{IMAGES_URL}/{image_id}", what=f"查询图片 {name} 的处理状态",
                                   referer=referer)
            status = str((info or {}).get("status") or "") if isinstance(info, dict) else ""
            if status == "success" and info.get("src"):
                return info
            if status in ("fail", "failed", "error"):
                raise PublishError(f"知乎处理图片 {name} 失败（{status}）")
            if time.monotonic() > deadline:
                raise PublishError(f"知乎处理图片 {name} 超过 {int(self.image_timeout)} 秒仍未完成，稍后重试",
                                   code=ErrorCode.TIMEOUT, retryable=True)
            await asyncio.sleep(IMAGE_POLL_INTERVAL)
