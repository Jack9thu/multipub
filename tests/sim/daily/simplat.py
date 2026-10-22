"""日常使用模拟：在 CLI / worker 进程里注册几个测试平台。

- fake2 / fake3 / fake4：与 multipub.platforms.fake 相同的假平台（不开浏览器），换个名字，
  好在一次 publish 里同时发几个平台、各自给出不同结果。行为由 platforms.<名>.fake 控制，
  另外支持 unknown（点了发布后结果未知）、slow（fake_secs 秒后才发布）、scheduled、submitted。
- simweb：真开 Chromium 的平台（multipub.browser.open_context），页面是本地仿页面
  https://creator.sim.example/：未登录跳登录页、上传有进度、上传完才能点发布、发布接口可成功 / 拒绝 / 不返回。

每次真正「发出去」（假平台走到发布那一步、simweb 的创建接口被调用）都记一行到 MULTIPUB_SIM_DAILY_LOG，
测试据此判断有没有重复发布。场景参数在环境变量 MULTIPUB_SIM_DAILY（JSON）里。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

BASE = "https://creator.sim.example"


def scenario() -> dict[str, Any]:
    try:
        return json.loads(os.environ.get("MULTIPUB_SIM_DAILY") or "{}")
    except ValueError:
        return {}


def record(kind: str, **data: Any) -> None:
    path = os.environ.get("MULTIPUB_SIM_DAILY_LOG")
    if not path:
        return
    row = {"type": kind, "pid": os.getpid(), "t": time.time(), **data}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 假平台

def _fake_classes() -> dict[str, type]:
    from multipub.models import ErrorCode, NeedsAction, PlatformPost, PublishError, Result, Status
    from multipub.platforms.fake import FakeAdapter

    class SimFake(FakeAdapter):
        async def publish(self, post: PlatformPost) -> Result:
            mode = str(post.options.get("fake", "ok"))
            secs = float(post.options.get("fake_secs", 0) or 0)
            if mode not in ("ok", "slow", "unknown", "scheduled", "submitted", "hang_after_submit",
                            "crash_after_submit", "sms"):
                return await super().publish(post)
            self.ctx.log(f"sim publish mode={mode}")
            code = ""
            if mode == "sms":  # 与抖音一样：发布前要短信验证码，等 mpub verify-code 写文件
                path = self.settings.sau_home / "verify_code.txt"
                path.unlink(missing_ok=True)
                self.ctx.needs_input("sms_code", f"请输入手机收到的验证码（最多等 {int(secs or 30)} 秒）", path)
                deadline = time.monotonic() + (secs or 30)
                while time.monotonic() < deadline and not code:
                    await asyncio.sleep(0.3)
                    code = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
                if not code:
                    raise NeedsAction("等短信验证码超时", code=ErrorCode.SMS_CODE_REQUIRED)
                path.unlink(missing_ok=True)
            if mode == "slow" and secs:
                for i in range(int(secs)):
                    await asyncio.sleep(1)
                    self.ctx.log(f"上传中 {int((i + 1) * 100 / secs)}%")
            self.ctx.mark_submit_started()
            record("publish", platform=self.name, account=self.ctx.account, title=post.title, mode=mode, code=code,
                   body=post.body, tags=post.tags, schedule=post.to_dict()["schedule"],
                   files=[str(p) for p in ([post.video] if post.video else []) + post.images])
            if mode == "unknown":
                raise PublishError("点击发布后没有拿到结果（模拟）", code=ErrorCode.UNKNOWN_OUTCOME)
            if mode == "hang_after_submit":
                await asyncio.sleep(3600)
            if mode == "crash_after_submit":
                os._exit(9)
            if mode == "scheduled":
                return self.result(Status.SCHEDULED, kind=post.kind, item_id="s1",
                                   message=f"已定时：{post.to_dict()['schedule']}")
            if mode == "submitted":
                return self.result(Status.SUBMITTED, kind=post.kind, message="已提交审核")
            return self.result(Status.PUBLISHED, kind=post.kind, url=f"https://{self.name}.example/p/1",
                               item_id="1")

    out = {}
    for name in ("fake2", "fake3", "fake4"):
        out[name] = type(f"SimFake_{name}", (SimFake,), {"name": name, "display_name": name.capitalize()})
    return out


FAKES: dict[str, type] = {}


# --------------------------------------------------------------------------- 真浏览器平台

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><title>登录</title></head><body>
<h1>扫码登录</h1><div id=qr style="width:160px;height:160px;background:#000"></div>
<script>
setTimeout(() => { document.cookie = "sess=valid; path=/"; location.href = "/home"; }, __AFTER__ * 1000);
</script></body></html>"""

HOME_HTML = """<!doctype html><html><head><meta charset=utf-8><title>创作中心</title></head>
<body><div id=user>模拟用户</div></body></html>"""

UPLOAD_HTML = """<!doctype html><html><head><meta charset=utf-8><title>发布作品</title></head><body>
<h1>发布作品</h1>
<input type=file id=file multiple>
<div id=progress>等待上传</div>
<input id=title placeholder="标题">
<button id=publish disabled>发布</button>
<div id=toast></div>
<script>
const S = __SCENARIO__;
document.getElementById('file').addEventListener('change', () => {
  const steps = Math.max(1, Math.round((S.upload_secs || 0) * 10));
  let p = 0;
  const t = setInterval(() => {
    p++;
    document.getElementById('progress').textContent = '上传中 ' + Math.round(p * 100 / steps) + '%';
    if (p >= steps) {
      clearInterval(t);
      document.getElementById('progress').textContent = '上传完成';
      document.getElementById('publish').disabled = false;
    }
  }, 100);
});
document.getElementById('publish').addEventListener('click', async () => {
  const r = await fetch('/api/create', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({title: document.getElementById('title').value})});
  const j = await r.json();
  if (j.code === 0) {
    document.getElementById('toast').textContent = '发布成功';
    setTimeout(() => { location.href = '/manage?id=' + j.id; }, 300);
  } else {
    document.getElementById('toast').textContent = j.msg;
  }
});
</script></body></html>"""

MANAGE_HTML = """<!doctype html><html><head><meta charset=utf-8><title>作品管理</title></head>
<body><h1>作品管理</h1></body></html>"""


async def handler(route: Any, request: Any) -> bool:
    url = request.url
    if not url.startswith(BASE):
        return False
    s = scenario()
    path = url[len(BASE):].split("?")[0] or "/"
    headers = await request.all_headers()
    logged_in = "sess=valid" in (headers.get("cookie") or "")

    async def html(body: str) -> None:
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    if path == "/login":
        await html(LOGIN_HTML.replace("__AFTER__", str(s.get("login_after", 1))))
    elif path in ("/home", "/upload") and not logged_in:
        # 不用 302：patchright 对 route.fulfill 的重定向不再拦截，跳转后的请求会直接走真实网络
        await html(f"<script>location.replace('{BASE}/login')</script>")
    elif path == "/home":
        await html(HOME_HTML)
    elif path == "/upload":
        await html(UPLOAD_HTML.replace("__SCENARIO__", json.dumps(s)))
    elif path == "/api/create":
        body = request.post_data or ""
        record("publish", platform="simweb", body=body)
        create = s.get("create", "ok")
        if create == "hang":
            await asyncio.sleep(3600)
        if create == "reject":
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"code": 1, "msg": "内容违规，发布失败"}, ensure_ascii=False))
        else:
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"code": 0, "id": "9001"}))
    elif path == "/manage":
        await html(MANAGE_HTML)
    else:
        await route.fulfill(status=404, body="not found")
    return True


def _web_class() -> type:
    from multipub.browser import load_storage_state, open_context, snapshot
    from multipub.models import ErrorCode, Kind, LoginRequired, PlatformPost, PublishError, Result, Status
    from multipub.platforms.base import PlatformAdapter

    class SimWebAdapter(PlatformAdapter):
        name = "simweb"
        display_name = "SimWeb"
        kinds = frozenset({Kind.VIDEO, Kind.NOTE})

        def _state(self) -> dict[str, Any] | None:
            return load_storage_state(self.account_file)

        async def login(self) -> Result:
            async with open_context(self.settings, headless=self.ctx.headless) as context:
                page = await context.new_page()
                await page.goto(f"{BASE}/login")
                shot = self.settings.qrcode_dir / f"simweb-{self.ctx.account}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot))
                self.ctx.qrcode(shot)
                await page.wait_for_url(f"{BASE}/home", timeout=30_000)
                self.account_file.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(self.account_file))
            return self.result(Status.OK, message="登录成功")

        async def check(self) -> Result:
            if not self.account_file.is_file():
                raise LoginRequired("未登录")
            async with open_context(self.settings, headless=True, storage_state=self._state()) as context:
                page = await context.new_page()
                await page.goto(f"{BASE}/home")
                if "/login" in page.url:
                    raise LoginRequired("登录已失效，请重新登录")
            return self.result(Status.OK, message="登录有效")

        async def publish(self, post: PlatformPost) -> Result:
            if not self.account_file.is_file():
                raise LoginRequired("未登录")
            s = scenario()
            async with open_context(self.settings, headless=self.ctx.headless, storage_state=self._state()) as context:
                page = await context.new_page()
                await page.goto(f"{BASE}/upload")
                if "/login" in page.url:
                    await snapshot(page, self.ctx, "login")
                    raise LoginRequired("登录已失效，请重新登录")
                files = [str(p) for p in ([post.video] if post.video else post.images)]
                await page.set_input_files("#file", files)
                await page.fill("#title", post.title)
                self.ctx.log("上传中……")
                await page.wait_for_selector("#publish:not([disabled])",
                                             timeout=float(s.get("upload_wait", 30)) * 1000)
                self.ctx.mark_submit_started()
                await page.click("#publish")
                try:
                    await page.wait_for_function(
                        "() => document.getElementById('toast').textContent.trim() !== ''",
                        timeout=float(s.get("result_wait", 15)) * 1000)
                except Exception as exc:  # noqa: BLE001
                    await snapshot(page, self.ctx, "after-submit")
                    raise PublishError("点了发布但没等到结果：请先到后台确认",
                                       code=ErrorCode.UNKNOWN_OUTCOME) from exc
                toast = (await page.text_content("#toast") or "").strip()
                if toast != "发布成功":
                    await snapshot(page, self.ctx, "rejected")
                    raise PublishError(f"平台拒绝：{toast}")
                await page.wait_for_url(f"{BASE}/manage**", timeout=10_000)
                item = page.url.split("id=")[-1]
                return self.result(Status.PUBLISHED, kind=post.kind, url=page.url, item_id=item)

    return SimWebAdapter


def _dead_proxy() -> None:
    """浏览器一律走一个不存在的代理：万一有请求绕过了路由（例如 fulfill 的 302 跳转），也连不到真实网络。"""
    try:
        from patchright.async_api import BrowserType
    except ImportError:
        return
    orig = BrowserType.launch

    async def launch(self: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("proxy", {"server": "http://127.0.0.1:9"})
        return await orig(self, *args, **kwargs)

    BrowserType.launch = launch  # type: ignore[method-assign]


def install() -> None:
    import multipub.platforms as reg

    for name in ("fake2", "fake3", "fake4"):
        reg._REGISTRY[name] = f"tests.sim.daily.simplat:{name}"
        reg.DISPLAY_NAMES[name] = name.capitalize()
    reg._REGISTRY["simweb"] = "tests.sim.daily.simplat:simweb"
    reg.DISPLAY_NAMES["simweb"] = "SimWeb"
    _dead_proxy()
    # 浏览器只在 worker 里打开；路由补丁在没导入 patchright 前装上也没关系
    try:
        from tests.sim._routing import install as install_routing

        install_routing(handler)
    except ImportError:
        pass


def __getattr__(name: str) -> Any:  # load_adapter 用 getattr(module, "fake2") 取类
    if name in ("fake2", "fake3", "fake4"):
        if not FAKES:
            FAKES.update(_fake_classes())
        return FAKES[name]
    if name == "simweb":
        return _web_class()
    raise AttributeError(name)
