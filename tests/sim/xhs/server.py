"""小红书创作服务平台的本地仿页面 + 仿接口（只在模拟测试的 worker 子进程里使用）。

场景参数来自环境变量 MULTIPUB_SIM_XHS（JSON），见 DEFAULTS；页面和接口的关键事件
（创建笔记请求体、页面上报的点击/提示）追加写到 MULTIPUB_SIM_XHS_LOG（JSON Lines），测试据此断言。

页面结构按 multipub / 上游 SAU 用到的选择器、文字、URL 搭建，行为参考上游提交说明与 postsail / omnipost：
- 发布页是 SPA：DOMContentLoaded 之后才渲染（render_delay_ms），未登录时由前端跳 /login（login_redirect_ms）；
- 选完视频立即出现编辑区，预览区（input.upload-input 的兄弟 div.preview-new）异步显示上传进度；
- 话题：正文里输入 #xxx 后异步出现 #creator-editor-topic-container .item；
- 发布按钮点击后 POST edith.xiaohongshu.com/web_api/sns/v2/note，成功后跳 /publish/success?…
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PAGES = Path(__file__).resolve().parent / "pages"
CREATOR = "creator.xiaohongshu.com"
EDITH = "edith.xiaohongshu.com"
NOTE_ID = "64f1a2b3c4d5e6f708192a3b"

DEFAULTS: dict[str, Any] = {
    # 发布页
    "render_delay_ms": 300,          # 发布页 SPA 渲染出上传区之前的延迟
    "login_redirect_ms": 400,        # 未登录时多久跳 /login
    "image_accept": "image/png,image/jpeg,image/webp",  # 图文上传 input 的 accept
    "upload_seconds": 2.0,           # 视频上传耗时
    "upload_fail": 0,                # 视频上传前 N 次失败（页面显示「上传失败」「重新上传」）
    "image_upload_seconds": 0.5,     # 每张图片上传耗时
    "image_fail": [],                # 这些下标的图片上传失败（缩略图显示「上传失败」）
    "image_fail_mode": "block",      # block：有失败的图时点发布只提示；drop：照样发布（少了那几张图）
    "while_uploading": "toast",      # 上传中点「发布」：toast（提示后不提交）/ disabled（按钮不可用）
    "topics": "debounced",           # debounced：停止输入后才出候选；instant：输入 # 立即出热门话题、稍后才刷新；none
    "topic_delay_ms": 300,
    "cover": "ok",                   # ok / direct（弹窗里直接是上传框）/ broken（没有上传入口）/ stuck（「确定」后弹窗不关）
    "declaration": "ok",             # ok / none（没有「添加内容类型声明」）
    "decl_popover": "inline",        # inline：声明下拉框在 #publish-container 里；body：渲染在 body 下的浮层
    "schedule_switch": "ok",         # ok / swallow（点开关没反应）/ revert（日期框不接受输入）
    "sidebar_button": False,         # 侧栏「发布笔记」做成 <button>（发布按钮的子串选择器会命中两个）
    "draft": "",                     # 非空：发布页恢复了上次的草稿（标题 + 正文）
    "session_drop_after": -1,        # >=0：发布页打开这么多次之后服务端判定会话失效（cookie 校验通过、真正发布时掉线）
    "captcha_on_load": False,        # 编辑区出来后就弹人机验证（风控），遮住「发布」
    # 创建笔记接口
    "api": "ok",                     # ok / reject / captcha / expired（返回登录过期、页面跳登录）/ error500 / hang
    "api_delay": 0.3,
    "success_redirect": "query",     # query：/publish/success?source=…；bare：/publish/success；none：不跳转
    "success_delay_ms": 600,
    # 登录页
    "login_panel": "qrcode",         # qrcode：默认就是扫码面板；switch：默认手机号登录，点 img.css-wemwzq 切换；
                                     # nosw：切换图标的类名和「扫码登录」文字都改版了；broken：根本没有切换入口
    "login_after": -1,               # 打开登录页多少秒后「扫码成功」（-1：没人扫码）
    "qr_ttl": 0,                     # 二维码有效期（秒），0 = 不过期
}

TINY_PNG_2 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
TINY_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


_LOADS = {"publish": 0}  # 本进程里发布页被打开的次数（cookie 校验和真正发布都在同一个 worker 进程里）


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = os.environ.get("MULTIPUB_SIM_XHS", "")
    if raw:
        cfg.update(json.loads(raw))
    return cfg


def log_event(kind: str, **data: Any) -> None:
    path = os.environ.get("MULTIPUB_SIM_XHS_LOG")
    if not path:
        return
    rec = {"t": round(time.time(), 3), "type": kind, **data}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _page(name: str, cfg: dict[str, Any]) -> str:
    html = (PAGES / name).read_text(encoding="utf-8")
    return html.replace("/*SIM_CONFIG*/{}", json.dumps(cfg, ensure_ascii=False))


async def _html(route: Any, body: str, status: int = 200) -> None:
    await route.fulfill(status=status, content_type="text/html; charset=utf-8", body=body)


def _cors(request: Any) -> dict[str, str]:
    origin = request.headers.get("origin") or f"https://{CREATOR}"
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
        "access-control-allow-methods": "GET,POST,OPTIONS",
        "access-control-allow-headers": "content-type,x-s,x-t",
    }


def request_url(request: Any) -> str:
    """请求的真实地址。

    patchright 注入 add_init_script（stealth.min.js）时，自己的 context 路由会把文档请求
    route.fallback(url="https://patchright-init-script-inject.internal/") 交给后面的路由；
    再 fallback 下去 driver 会**真的去联网**取原文档再注入脚本。所以这里取回原地址、
    由仿页面直接 fulfill（driver 在 fulfill text/html 时同样会注入脚本），绝不 fallback。
    """
    url = request.url
    host = urlparse(url).hostname or ""
    if host.startswith("patchright-") and host.endswith(".internal"):
        impl = getattr(request, "_impl_obj", request)
        url = (getattr(impl, "_initializer", None) or {}).get("url") or url
    return url


async def handler(route: Any, request: Any) -> bool:
    cfg = config()
    full_url = request_url(request)
    url = urlparse(full_url)
    host, path = url.hostname or "", url.path
    if os.environ.get("MULTIPUB_SIM_XHS_DEBUG"):
        print(f"[sim-xhs] {request.method} {full_url}", file=sys.stderr, flush=True)

    if host == CREATOR:
        if path == "/__sim/event":
            try:
                data = json.loads(request.post_data or "{}")
            except json.JSONDecodeError:
                data = {"raw": request.post_data}
            log_event("page", **data)
            await route.fulfill(status=204, body="")
            return True
        if path.startswith("/publish/publish"):
            _LOADS["publish"] += 1
            drop = int(cfg.get("session_drop_after", -1))
            log_event("goto", url=full_url, n=_LOADS["publish"])
            await _html(route, _page("publish.html", dict(cfg, force_logout=0 <= drop < _LOADS["publish"])))
            return True
        if path.startswith("/publish/success"):
            log_event("success_page", url=full_url)
            await _html(route, _page("success.html", cfg))
            return True
        if path.startswith("/login"):
            log_event("goto", url=full_url)
            await _html(route, _page("login.html", dict(cfg, qr_png=TINY_PNG, qr_png2=TINY_PNG_2)))
            return True
        if path in ("/", "/new/home"):
            await _html(route, _page("home.html", cfg))
            return True
        if path == "/favicon.ico":
            await route.fulfill(status=404, body="")
            return True
        return False

    if host == EDITH and path.startswith("/web_api/sns/"):
        if request.method == "OPTIONS":
            await route.fulfill(status=204, headers=_cors(request), body="")
            return True
        if request.method == "POST" and path.rstrip("/").endswith("/note"):
            try:
                body = json.loads(request.post_data or "{}")
            except json.JSONDecodeError:
                body = {"raw": request.post_data}
            cookie = await request.header_value("cookie") or ""
            session = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("web_session=")), "")
            log_event("create", body=body, session=session)
            api = cfg["api"]
            if not session or session == "expired":
                api = "expired"
            if api == "hang":
                await asyncio.sleep(3600)
                return True
            await asyncio.sleep(float(cfg["api_delay"]))
            if api == "error500":
                await route.fulfill(status=500, headers=_cors(request), content_type="text/html",
                                    body="<html>502 Bad Gateway</html>")
                return True
            if api == "reject":
                payload = {"success": False, "code": -1, "msg": "标题或正文含有违规内容", "data": {}}
            elif api == "expired":
                payload = {"success": False, "code": -100, "msg": "登录已过期", "data": {}}
            elif api == "captcha":
                payload = {"success": False, "code": -9101, "msg": "需要完成验证", "data": {}}
            else:
                payload = {"success": True, "code": 0, "msg": "成功", "data": {"id": NOTE_ID, "score": 10}}
            await route.fulfill(status=200, headers=_cors(request), content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))
            return True
        return False

    if host.endswith("xiaohongshu.com") or host.endswith("xhscdn.com"):
        await route.fulfill(status=404, body="")  # 统计、静态资源等：直接 404，别让页面卡住
        return True
    return False


def install() -> None:
    from tests.sim._routing import install as _install

    _install(handler)
    print("[sim-xhs] routing installed", file=sys.stderr, flush=True)
