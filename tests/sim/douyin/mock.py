"""抖音创作者中心（creator.douyin.com）的本地仿真：页面 + 接口。

由同目录的 sitecustomize.py 在 MULTIPUB_SIM=douyin 时安装到每个浏览器上下文（见 tests/sim/_routing.py）。

- 场景：环境变量 DY_SIM_FILE 指向的 JSON（每个请求都重新读取，测试可在运行中途修改），缺省值见 DEFAULTS；
- 记录：页面和接口发生的事（点击发布、创建接口收到的内容、短信验证……）逐行写入 DY_SIM_LOG（JSONL），
  测试据此断言「平台实际收到了什么」。

页面结构取自 vendored 流程使用的选择器、上游 social-auto-upload / postsail 的说明（label.radio-* + data-checked、
create_v2 回执、uc-ui-verify 短信组件、dy-creator-content-modal 封面弹窗……），行为（异步上传进度、上传完才出表单、
发布后跳到作品管理页、toast、弹窗）尽量贴近真实页面；具体由场景参数切换。
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
HOST = "creator.douyin.com"
VALID_SESSION = "sim-session-ok"

DEFAULTS: dict[str, Any] = {
    "valid_session": VALID_SESSION,
    "render_delay_ms": 0,          # SPA 渲染前的延迟（慢页面）
    "doc_delay_ms": 60,            # 文档请求的网络往返（真实网络里整页跳转不会是瞬间的）
    "page_version": "v2",          # v2: post/video，上传完才出表单；v1: content/publish，表单先出、按钮上传完才可用
    "disable_until_uploaded": True,
    "upload_ms": 1500,             # 视频上传耗时（进度条）
    "upload_fail_times": 0,        # 前 N 次上传在 40% 时失败
    "note_upload_ms": 800,
    "create": "ok",                # ok | reject | hang | noop | http500 | verify_then_ok
    "create_delay_ms": 200,
    "reject_msg": "作品内容存在违规风险，发布失败",
    "item_id": "7432109876543210987",
    "redirect": "assign",          # assign（整页跳转）| push（SPA 路由）| none（停在原页）
    "redirect_delay_ms": 300,
    "sms": "none",                 # none | on_click（第一次点发布弹短信验证，不发请求）| on_enter（进上传页就弹）
    "sms_resubmit": "manual",      # 验证通过后：manual 需要再点发布；auto 页面自己重新提交
    "sms_code": "123456",
    "sms_user_closes_ms": 0,       # >0：弹窗出现 N 毫秒后用户在窗口里自己完成验证（模拟 --headed）
    "captcha": "none",             # none | on_click（点发布时）| on_form（视频表单一出来就弹）
    "notice_on_form": False,       # 发布表单一出来就弹「创作者服务通知」（只有「我知道了」，Esc 关不掉）
    "publish_mask": False,         # 「发布」按钮上盖着关不掉的浮层
    "cover_stuck": False,          # 自定义封面一直「检测中」：封面弹窗关不掉（盖住「发布」）
    "require_cover": False,        # 没设封面时点发布提示「请设置封面后再发布」
    "schedule_swallow": False,     # 「定时发布」点击被吞掉
    "schedule_min_lead_min": 120,  # 日期框拒绝早于现在 + N 分钟的时间（回退成原值）
    "declarations": ["内容由AI生成", "内容为转载信息", "内容为个人观点或见解", "虚构演绎，仅供娱乐"],
    "collections": ["我的合集", "旅行日记"],
    "musics": ["晴天", "稻香"],
    "sync_default": False,
    "logout_after_pages": 0,       # >0：只有前 N 个页面是登录状态（之后跳回登录页）
    "login": {"qr_delay_ms": 800, "scan_after_ms": 0, "qr_expire_ms": 0, "scan_flag": ""},  # scan_after_ms=0：没人扫码
}


# ---------------------------------------------------------------- 场景与记录

def scenario() -> dict[str, Any]:
    sc = json.loads(json.dumps(DEFAULTS))
    path = os.environ.get("DY_SIM_FILE")
    if path and Path(path).is_file():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        login = {**sc["login"], **(data.pop("login", None) or {})}
        sc.update(data)
        sc["login"] = login
    return sc


def record(event: dict[str, Any]) -> None:
    path = os.environ.get("DY_SIM_LOG")
    if not path:
        return
    event = {"t": round(time.time(), 3), "pid": os.getpid(), **event}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_log(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 二维码图片

def qr_png(n: int) -> bytes:
    """第 n 张「二维码」：确定性的黑白方块 PNG（测试据此核对上报的图片就是页面上的那张）。"""
    from PIL import Image

    size = 21
    img = Image.new("RGB", (size * 8, size * 8), "white")
    px = img.load()
    for y in range(size):
        for x in range(size):
            if ((x * 7 + y * 13 + n * 31) % 5) < 2 or x in (0, size - 1) or y in (0, size - 1):
                for dy in range(8):
                    for dx in range(8):
                        px[x * 8 + dx, y * 8 + dy] = (0, 0, 0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def qr_data_url(n: int) -> str:
    return "data:image/png;base64," + base64.b64encode(qr_png(n)).decode()


# ---------------------------------------------------------------- 路由

_QR: dict[str, Any] = {"issued": {}, "first": None, "count": 0}


def _html(name: str, sim: dict[str, Any]) -> str:
    text = (HERE / name).read_text(encoding="utf-8")
    return text.replace("__SIM__", json.dumps(sim, ensure_ascii=False))


async def _json(route: Any, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
    h = {"content-type": "application/json; charset=utf-8", **(headers or {})}
    await route.fulfill(status=status, headers=h, body=json.dumps(payload, ensure_ascii=False))


def _logged_in(cookie_header: str, sc: dict[str, Any]) -> bool:
    cookies = dict(part.strip().split("=", 1) for part in cookie_header.split(";") if "=" in part)
    return cookies.get("sessionid") == sc["valid_session"]


INJECT_HOST = "patchright-init-script-inject.internal"


def real_url(request: Any) -> str:
    """patchright 有 init script（stealth.min.js）时，会先把文档请求 fallback 到
    https://patchright-init-script-inject.internal/ 再交给用户路由；原始 URL 只在 initializer 里。"""
    url = request.url
    if INJECT_HOST in url:
        try:
            url = request._impl_obj._initializer.get("url") or url
        except AttributeError:
            pass
    return url


async def handler(route: Any, request: Any) -> bool:
    url = urlparse(real_url(request))
    if url.netloc != HOST:
        return False
    sc = scenario()
    path = url.path

    if path == "/sim/record":
        try:
            record(json.loads(request.post_data or "{}"))
        except ValueError:
            pass
        await route.fulfill(status=204, body="")
        return True

    if path.startswith("/web/api/media/aweme/create"):
        return await _create(route, request, sc)

    if path == "/sim/qr":
        _QR["count"] += 1
        n = _QR["count"]
        _QR["issued"][n] = time.monotonic()
        _QR["first"] = _QR["first"] or time.monotonic()
        record({"type": "qr_issued", "n": n})
        await _json(route, {"token": n, "qrcode": qr_data_url(n)})
        return True

    if path == "/sim/qr_status":
        login = sc["login"]
        token = int((parse_qs(url.query).get("token") or ["0"])[0])
        now = time.monotonic()
        flag = login.get("scan_flag")
        scanned = (bool(flag) and Path(flag).exists()) or (
            not flag and login.get("scan_after_ms") and _QR["first"] is not None
            and (now - _QR["first"]) * 1000 >= login["scan_after_ms"])
        issued = _QR["issued"].get(token, now)
        if scanned and token == _QR["count"]:
            record({"type": "login_confirmed", "token": token})
            await _json(route, {"status": "confirmed",
                                "redirect_url": "https://creator.douyin.com/creator-micro/home"},
                        headers={"set-cookie": f"sessionid={sc['valid_session']}; Domain=.douyin.com; Path=/; "
                                               "Secure; HttpOnly; SameSite=Lax"})
        elif login.get("qr_expire_ms") and (now - issued) * 1000 >= login["qr_expire_ms"]:
            await _json(route, {"status": "expired"})
        else:
            await _json(route, {"status": "new"})
        return True

    if path == "/sim/sms/send":
        record({"type": "sms_sent"})
        await _json(route, {"ok": True})
        return True

    if path == "/sim/sms/verify":
        try:
            code = str(json.loads(request.post_data or "{}").get("code", ""))
        except ValueError:
            code = ""
        ok = code == sc["sms_code"]
        record({"type": "sms_verify", "code": code, "ok": ok})
        await _json(route, {"ok": ok})
        return True

    if request.resource_type == "document" and request.method == "GET":
        headers = await request.all_headers()
        logged = _logged_in(headers.get("cookie", ""), sc)
        if logged and sc.get("logout_after_pages"):  # 会话在第 N 个页面之后过期（cookie 校验通过、发布途中掉线）
            log = os.environ.get("DY_SIM_LOG")
            served = sum(1 for e in (read_log(log) if log else []) if e.get("type") == "page" and e.get("logged_in"))
            logged = served < sc["logout_after_pages"]
        sim = {k: v for k, v in sc.items() if k != "login"}
        sim["logged_in"] = logged
        sim["login"] = sc["login"]
        if path == "/":
            record({"type": "page", "page": "login", "logged_in": logged})
            body = _html("login.html", sim)
        elif path.startswith("/creator-micro/"):
            record({"type": "page", "page": path, "logged_in": logged})
            body = _html("app.html", sim)
        else:
            return False
        headers = {"content-type": "text/html; charset=utf-8"}
        if sc.get("doc_delay_ms"):
            import asyncio

            await asyncio.sleep(sc["doc_delay_ms"] / 1000)
        if logged:  # 已登录的页面会刷新一个会话 cookie（测试据此确认发布后回写了 storage_state）
            headers["set-cookie"] = (f"sim_refresh={int(time.time() * 1000)}; Domain=.douyin.com; Path=/; "
                                     "Max-Age=86400; Secure; SameSite=Lax")
        await route.fulfill(status=200, headers=headers, body=body)
        return True
    return False


async def _create(route: Any, request: Any, sc: dict[str, Any]) -> bool:
    import asyncio

    try:
        payload = json.loads(request.post_data or "{}")
    except ValueError:
        payload = {"raw": request.post_data}
    log = os.environ.get("DY_SIM_LOG")
    n = 1 + sum(1 for e in (read_log(log) if log else []) if e.get("type") == "create")  # 跨 worker 进程计数
    mode = sc["create"]
    record({"type": "create", "n": n, "mode": mode, "method": request.method, "url": request.url,
            "payload": payload})
    if mode == "hang":
        return True  # 不回应：请求一直挂着
    if sc.get("create_delay_ms"):
        await asyncio.sleep(sc["create_delay_ms"] / 1000)
    if mode == "http500":
        await route.fulfill(status=502, headers={"content-type": "text/html"}, body="<html>Bad Gateway</html>")
    elif mode == "reject":
        await _json(route, {"status_code": 4, "status_msg": sc["reject_msg"], "extra": {"now": int(time.time())}})
    elif mode == "verify_then_ok" and n == 1:
        await _json(route, {"status_code": 2190, "status_msg": "为了你的账号安全，请完成身份验证"})
    else:
        item_id = sc["item_id"] if n == 1 else str(int(sc["item_id"]) + n - 1)
        await _json(route, {"status_code": 0, "status_msg": "", "item_id": item_id,
                            "extra": {"logid": "20261022120000SIM", "now": int(time.time())}})
    return True


def install() -> None:
    from tests.sim._routing import install as _install

    _install(handler)
