"""B站图文动态：浏览器模式（默认）。

在 https://t.bilibili.com/ 的动态发布框里填标题、正文、传图，点「发布」，
从 /x/dynamic/feed/create/dyn 的响应里拿 dyn_id_str -> https://www.bilibili.com/opus/<id>。

选择器来自 2026-10-20 线上发布器脚本（dyn-home index.ad97d12f.js），**未在真实账号上验证**，
所以每一步都有多个候选、明确的报错和截图。定时发布 / AI 声明 / 图片模式没有稳定的 DOM 可点
（自绘的日期时间选择器、级联菜单），改为直接设置发布器 Vue 组件的状态（主 world 里执行）。

立即发布时，没有 cookie dy_spec_agreed=1 的浏览器会先弹《动态发布规范》确认框（「确认并发送」才发出）。
新开的浏览器上下文总是没有这个 cookie，所以：用户已同意（配置 confirm_dynamic_rules / 清单 confirm_rules，
或之前在有头模式里亲手点过）就带上该 cookie；否则有头模式等人点，无头模式在传图之前就停下来问人。

只在 worker 子进程里导入。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import ErrorCode, LoginRequired, NeedsAction, PublishError, ValidationFailed
from .base import JobContext

PUBLISH_PAGE = "https://t.bilibili.com/"

SEL_PUBLISHER = (".bili-dyn-publishing",)
SEL_TITLE = ("input.bili-dyn-publishing__title__input", ".bili-dyn-publishing__title input")
SEL_TEXT = (".bili-dyn-publishing__input [contenteditable='true']",
            ".bili-dyn-publishing__input [contenteditable]",
            ".bili-dyn-publishing__input textarea")
SEL_PIC_TOOL = (".bili-dyn-publishing__tools__item.pic",)
SEL_PIC_PANEL = ".bili-dyn-publishing__image-upload"
SEL_PIC_ADD = (".bili-dyn-publishing__image-upload .bili-pics-uploader__add", ".bili-pics-uploader__add")
SEL_PIC_ERROR_MSG = ".bili-pics-uploader-item-error__msg"
SEL_PIC_ERROR = ".bili-pics-uploader-item-error"
SEL_DATETIME = ".bili-dyn-publishing__datetime"
SEL_PUBLISH = (".bili-dyn-publishing__action.launcher",)
SEL_CAPTCHA = (".geetest_panel", ".geetest_box", "iframe[src*='geetest']", ".bili-mini-mask")
SEL_LOGIN_ENTRY = (".header-login-entry", ".bili-header .login-btn")
# 《动态发布规范》确认框：立即发布时，若 cookie dy_spec_agreed != 1 就会弹出，点「确认并发送」才真正发出
SEL_SPEC_POPUP = (".bili-dyn-specification-popup",)
SEL_SPEC_CONFIRM_BTN = ".bili-dyn-specification-popup__btn"
SPEC_CONFIRM_TEXT = "确认并发送"
SPEC_COOKIE = "dy_spec_agreed"
SPEC_URL = "https://t.bilibili.com/h5/dynamic/specification"
SPEC_HUMAN_WAIT = 180  # 有头模式下等人点确认的秒数
SPEC_CANCEL_GRACE = 8  # 弹窗关闭后这么久还没有发出请求，视为点了「取消」
POLL_SECONDS = 0.5  # 等待发布结果时的轮询间隔

UPLOAD_BFS_PATH = "/x/dynamic/feed/draw/upload_bfs"
SUBMIT_CHECK_PATH = "/x/dynamic/feed/create/submit_check"
CREATE_DYN_PATH = "/x/dynamic/feed/create/dyn"

# 在主 world 里找到发布器组件（Vue 2，元素上有 __vue__），设置定时 / AI 声明 / 图片模式
_VUE_OPTIONS_JS = """
(args) => {
  const root = document.querySelector('.bili-dyn-publishing');
  if (!root) return {ok: false, reason: 'no-root'};
  const isPublisher = (vm) => vm && vm.tools && vm.tools.timingSetting && vm.creationOption;
  let vm = root.__vue__;
  while (vm && !isPublisher(vm)) vm = vm.$parent;
  if (!vm && root.__vue__) {
    const queue = [root.__vue__];
    while (queue.length && !vm) {
      const cur = queue.shift();
      if (isPublisher(cur)) vm = cur; else queue.push(...(cur.$children || []));
    }
  }
  if (!vm) return {ok: false, reason: 'no-vm'};
  const res = {ok: true};
  if (args.timer_ms) {
    const t = vm.tools.timingSetting;
    if (!t.visible) return {ok: false, reason: 'timing-hidden'};
    t.active = true; t.data.id = 0; t.data.next = true; t.data.datetime = args.timer_ms;
    res.timing = t.data.datetime === args.timer_ms;
  }
  if (args.aigc) {
    const c = vm.creationOption;
    res.aigc = !!(c && c.list && c.list.length > 1);
    if (res.aigc) c.value = 1;
  }
  if (args.pic_mode !== null && args.pic_mode !== undefined && vm.picModeOption) {
    vm.picModeOption.value = args.pic_mode;
    res.pic_mode = true;
  }
  return res;
}
"""


@dataclass
class _NetTracker:
    """监听发布器发出的接口请求，作为比 DOM 更可靠的状态信号。"""

    uploaded: int = 0
    upload_errors: list[str] = field(default_factory=list)
    check_sent: bool = False
    check: dict[str, Any] | None = None
    create_sent: bool = False
    created: dict[str, Any] | None = None
    created_status: int | None = None

    def on_request(self, req: Any) -> None:
        if req.method != "POST":
            return
        if CREATE_DYN_PATH in req.url:
            self.create_sent = True
        elif SUBMIT_CHECK_PATH in req.url:
            self.check_sent = True

    async def on_response(self, resp: Any) -> None:
        url = resp.url
        if not any(p in url for p in (UPLOAD_BFS_PATH, SUBMIT_CHECK_PATH, CREATE_DYN_PATH)):
            return
        try:
            data = await resp.json()
        except Exception:  # noqa: BLE001 - 412 等非 JSON 响应
            data = {"code": None, "message": f"HTTP {resp.status}"}
        if not isinstance(data, dict):
            data = {"code": None, "message": "非 JSON 响应"}
        if UPLOAD_BFS_PATH in url:
            if data.get("code") == 0:
                self.uploaded += 1
            else:
                self.upload_errors.append(f"{data.get('code')}: {data.get('message')}")
        elif SUBMIT_CHECK_PATH in url:
            self.check = data
        elif CREATE_DYN_PATH in url:
            self.created_status = resp.status
            self.created = data


async def _first_visible(page: Any, selectors: tuple[str, ...], timeout_ms: int) -> Any | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001 - 页面跳转中
                pass
        await asyncio.sleep(0.3)
    return None


async def _any_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def spec_needs_action_message() -> str:
    return ("B站立即发布动态前会弹出《动态发布规范》确认框（" + SPEC_URL + "），需要你本人同意，multipub 不会擅自代点。"
            "阅读后二选一再重新发布：① 加 --headed 运行，在浏览器窗口里点「确认并发送」（之后本账号会记住）；"
            "② 在 config.toml 的 [platforms.bilibili] 加 confirm_dynamic_rules = true"
            "（或清单里 platforms.bilibili.confirm_rules: true），由 multipub 代为确认。动态尚未发出")


def with_spec_cookie(storage_state: dict[str, Any]) -> dict[str, Any]:
    """已同意过规范：像浏览器一样带上 dy_spec_agreed=1（网页端写的就是这个 cookie，有效期一年）。"""
    from .bili_cookies import to_playwright_cookie

    cookies = [c for c in storage_state.get("cookies", []) if c.get("name") != SPEC_COOKIE]
    cookies.append(to_playwright_cookie({"name": SPEC_COOKIE, "value": "1", "expires": time.time() + 365 * 86400,
                                         "http_only": 0, "secure": 0}))
    return {**storage_state, "cookies": cookies}


class BrowserNotePublisher:
    """spec_consent：用户已同意《动态发布规范》（配置项或之前亲手点过确认）；spec_marker：记住同意的文件。"""

    def __init__(self, ctx: JobContext, *, account: str, spec_consent: bool = False,
                 spec_marker: Path | None = None):
        self.ctx = ctx
        self.account = account
        self.spec_consent = spec_consent
        self.spec_marker = spec_marker
        self.net = _NetTracker()

    async def publish(self, *, storage_state: dict[str, Any], title: str, text: str, images: list[Path],
                      timer_pub_time: int | None, aigc: bool, pic_mode: int | None) -> dict[str, Any]:
        from ..browser import open_context, snapshot

        if self.spec_consent:
            storage_state = with_spec_cookie(storage_state)
        async with open_context(self.ctx.settings, headless=self.ctx.headless,
                                storage_state=storage_state) as context:
            page = await context.new_page()
            page.on("request", self.net.on_request)
            page.on("response", self.net.on_response)
            try:
                data = await self._run(page, title=title, text=text, images=images,
                                       timer_pub_time=timer_pub_time, aigc=aigc, pic_mode=pic_mode)
            except Exception:
                await snapshot(page, self.ctx, "note-error")
                raise
            await asyncio.sleep(2)
            await snapshot(page, self.ctx, "note-done")
            return data

    # -------------------------------------------------------------- steps
    async def _run(self, page: Any, *, title: str, text: str, images: list[Path],
                   timer_pub_time: int | None, aigc: bool, pic_mode: int | None) -> dict[str, Any]:
        log = self.ctx.log
        log("打开 t.bilibili.com 动态发布框")
        await page.goto(PUBLISH_PAGE, wait_until="domcontentloaded", timeout=60_000)
        publisher = await _first_visible(page, SEL_PUBLISHER, 30_000)
        if publisher is None:
            if "passport.bilibili.com" in page.url or await _any_visible(page, SEL_LOGIN_ENTRY):
                raise LoginRequired(f"B站网页未登录：运行 mpub login bilibili --account {self.account}")
            raise PublishError("找不到动态发布框（.bili-dyn-publishing），页面可能改版")

        # 定时发布走另一条分支，不弹规范确认框；立即发布且没同意过时，无头模式先停下来问人（还没传图）
        spec_pending = not timer_pub_time and not await self._spec_agreed(page)
        if spec_pending and not self.spec_consent:
            if self.ctx.headless:
                raise NeedsAction(spec_needs_action_message(), code=ErrorCode.VALIDATION)
            log("发布时会弹出《动态发布规范》确认框：请在浏览器窗口里阅读后点「确认并发送」", level="warning")

        if title:
            await self._fill_title(page, title)
        if text:
            await self._fill_text(page, text)
        await self._upload_images(page, images)

        if timer_pub_time or aigc or pic_mode is not None:
            await self._apply_options(page, timer_pub_time=timer_pub_time, aigc=aigc, pic_mode=pic_mode)

        button = await _first_visible(page, SEL_PUBLISH, 10_000)
        if button is None:
            raise PublishError("找不到「发布」按钮（.bili-dyn-publishing__action.launcher）")
        if not await self._wait_enabled(button, 15):
            raise PublishError("「发布」按钮一直是禁用状态（内容为空或图片未传完？）")
        if timer_pub_time and timer_pub_time <= time.time() + 330:
            raise ValidationFailed("定时发布时间距现在已不足 5 分钟，B站会改成立即发布；请调整 schedule")

        self.ctx.mark_submit_started()
        log("点击发布")
        try:
            await button.click(timeout=10_000)
        except Exception as exc:  # noqa: BLE001 - 被弹窗遮挡等：点击没有发生
            if not self.net.create_sent:
                raise PublishError(f"点击「发布」失败（可能被弹窗遮挡）: {type(exc).__name__}；没有发出发布请求，"
                                   "确认动态页没有新动态后可加 --force 重试") from exc
            raise
        return await self._wait_created(page, spec_pending=spec_pending)

    async def _spec_agreed(self, page: Any) -> bool:
        """与网页端 _checkSpecification 相同的判断：cookie dy_spec_agreed 为 1 就不弹框。"""
        try:
            cookies = await page.context.cookies([PUBLISH_PAGE])
        except Exception as exc:  # noqa: BLE001 - 拿不到就按"会弹框"处理
            self.ctx.log(f"读取 cookie 失败（{type(exc).__name__}），按需要确认规范处理", level="debug")
            return False
        return any(c.get("name") == SPEC_COOKIE and str(c.get("value", "")).strip() == "1" for c in cookies)

    async def _confirm_spec(self, page: Any) -> None:
        popup = page.locator(SEL_SPEC_POPUP[0])
        candidates = (popup.locator(SEL_SPEC_CONFIRM_BTN, has_text=SPEC_CONFIRM_TEXT),
                      popup.get_by_text(SPEC_CONFIRM_TEXT, exact=True))
        for loc in candidates:
            try:
                if await loc.first.is_visible():
                    await loc.first.click(timeout=10_000)
                    self.ctx.log("已按你此前的同意（confirm_dynamic_rules / confirm_rules / 确认记录）"
                                 "确认《动态发布规范》")
                    return
            except Exception as exc:  # noqa: BLE001 - 换下一个候选
                self.ctx.log(f"点击「{SPEC_CONFIRM_TEXT}」失败: {type(exc).__name__}", level="debug")
        raise PublishError(f"《动态发布规范》确认框里找不到「{SPEC_CONFIRM_TEXT}」按钮，动态没有发出；"
                           "可加 --headed 手动确认")

    def _remember_spec(self) -> None:
        """用户在有头模式里亲手点了确认：记下来，之后无头发布直接带 dy_spec_agreed（删除该文件即撤销）。"""
        if self.spec_marker is None:
            return
        try:
            self.spec_marker.parent.mkdir(parents=True, exist_ok=True)
            self.spec_marker.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 在浏览器里确认了 {SPEC_URL}\n",
                                        encoding="utf-8")
        except OSError as exc:
            self.ctx.log(f"记录规范确认失败（{exc}），下次还会询问", level="warning")

    async def _fill_title(self, page: Any, title: str) -> None:
        box = await _first_visible(page, SEL_TITLE, 10_000)
        if box is None:
            raise PublishError("找不到动态标题输入框（.bili-dyn-publishing__title__input）")
        await box.fill(title)
        if (await box.input_value()).strip() != title.strip():
            raise PublishError("动态标题没有填进去（输入框内容不一致）")

    async def _fill_text(self, page: Any, text: str) -> None:
        editor = await _first_visible(page, SEL_TEXT, 10_000)
        if editor is None:
            raise PublishError("找不到动态正文输入框（.bili-dyn-publishing__input）")
        await editor.click()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                await page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                await page.keyboard.press("Enter")
        typed = " ".join((await editor.inner_text()).split())
        first = " ".join(next((x for x in lines if x.strip()), "").split())[:10]
        if first and first not in typed:
            raise PublishError("动态正文没有填进去（编辑器内容不一致）")

    async def _upload_images(self, page: Any, images: list[Path]) -> None:
        log = self.ctx.log
        panel = page.locator(SEL_PIC_PANEL).first
        if not await panel.is_visible():
            tool = await _first_visible(page, SEL_PIC_TOOL, 10_000)
            if tool is None:
                raise PublishError("找不到「图片」按钮（.bili-dyn-publishing__tools__item.pic）")
            await tool.click()
        files = [str(p) for p in images]
        log(f"上传 {len(files)} 张图片")
        pending = list(files)
        while pending:
            add = await _first_visible(page, SEL_PIC_ADD, 10_000)
            if add is None:
                raise PublishError("找不到添加图片的按钮（.bili-pics-uploader__add）")
            async with page.expect_file_chooser(timeout=15_000) as chooser_info:
                await add.click()
            chooser = await chooser_info.value
            batch = pending if chooser.is_multiple() else pending[:1]
            await chooser.set_files(batch)
            pending = pending[len(batch):]
            if pending:
                await asyncio.sleep(1)

        deadline = time.monotonic() + 60 + 30 * len(files)
        while self.net.uploaded < len(files):
            if self.net.upload_errors:
                raise PublishError(f"图片上传失败: {self.net.upload_errors[0]}")
            if await page.locator(SEL_PIC_ERROR).count():
                msg_loc = page.locator(SEL_PIC_ERROR_MSG).first
                msg = (await msg_loc.inner_text()).strip() if await msg_loc.count() else ""
                raise PublishError(f"图片上传失败: {msg or '发布框显示上传错误'}")
            if time.monotonic() > deadline:
                raise PublishError(f"图片上传超时（{self.net.uploaded}/{len(files)}）", code=ErrorCode.TIMEOUT)
            await asyncio.sleep(1)
        await asyncio.sleep(1)  # 等预览渲染、发布器状态更新

    async def _apply_options(self, page: Any, *, timer_pub_time: int | None, aigc: bool,
                             pic_mode: int | None) -> None:
        args = {"timer_ms": timer_pub_time * 1000 if timer_pub_time else None,
                "aigc": aigc, "pic_mode": pic_mode}
        res = await page.evaluate(_VUE_OPTIONS_JS, args, isolated_context=False)
        if not isinstance(res, dict) or not res.get("ok"):
            reason = res.get("reason") if isinstance(res, dict) else res
            if timer_pub_time:
                raise PublishError(f"无法设置定时发布（{reason}）：可去掉 schedule，或改用 mode: http")
            self.ctx.log(f"无法设置 AI 声明 / 图片模式（{reason}），按默认发布", level="warning")
            return
        if timer_pub_time:
            panel = page.locator(SEL_DATETIME).first
            await asyncio.sleep(0.5)
            if not res.get("timing") or not await panel.is_visible():
                raise PublishError("定时发布没有生效（发布框没有显示时间设置）：可去掉 schedule，或改用 mode: http")
            self.ctx.log("已设置定时发布")
        if aigc and not res.get("aigc"):
            self.ctx.log("该账号的发布框没有「AI 创作声明」选项，未声明", level="warning")

    @staticmethod
    async def _wait_enabled(button: Any, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            cls = await button.get_attribute("class") or ""
            if "disabled" not in cls.split():
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_created(self, page: Any, *, spec_pending: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + 60
        spec_seen = False  # 看到了规范确认框
        spec_closed_at: float | None = None
        while time.monotonic() < deadline:
            created = self.net.created
            if created is not None:
                if spec_seen and not self.spec_consent:
                    self._remember_spec()
                return self._parse_created(created)
            check = self.net.check
            if check is not None and check.get("code") not in (0, None):
                # submit_check 没通过，发布请求根本没有发出
                raise PublishError(f"B站发布前检查未通过（{check.get('code')}: {check.get('message')}）")
            if await _any_visible(page, SEL_CAPTCHA):
                raise NeedsAction("B站弹出了人机验证：加 --headed 重新发布前，先到动态页确认是否已发出",
                                  code=ErrorCode.CAPTCHA)
            if spec_pending and not (self.net.check_sent or self.net.create_sent):
                visible = await _any_visible(page, SEL_SPEC_POPUP)
                if visible:
                    spec_closed_at = None
                    if not spec_seen:
                        spec_seen = True
                        if self.spec_consent:
                            await self._confirm_spec(page)
                        else:  # 有头模式：等人点（无头模式在发布前就已经停下）
                            deadline = max(deadline, time.monotonic() + SPEC_HUMAN_WAIT)
                            self.ctx.log("请在浏览器窗口里阅读《动态发布规范》并点「确认并发送」", level="warning")
                elif spec_seen:
                    spec_closed_at = spec_closed_at or time.monotonic()
                    sent = self.net.check_sent or self.net.create_sent
                    if not sent and time.monotonic() - spec_closed_at > SPEC_CANCEL_GRACE:
                        raise PublishError("《动态发布规范》确认框已关闭但没有发出发布请求（点了取消？），"
                                           "动态没有发出；"
                                           "确认动态页没有新动态后可加 --force 重发")
            await asyncio.sleep(POLL_SECONDS)
        if spec_seen and not (self.net.check_sent or self.net.create_sent):
            raise PublishError("等待确认《动态发布规范》超时，动态没有发出；确认动态页没有新动态后可加 --force 重发",
                               code=ErrorCode.TIMEOUT)
        if not self.net.create_sent:
            raise PublishError("点了发布但没有观察到发布请求（可能弹出了确认框），大概率没发出；"
                               "先到 B站动态页确认，再决定是否重发", code=ErrorCode.UNKNOWN_OUTCOME)
        raise PublishError("发布请求已发出但 60 秒内没有拿到结果：先到 B站动态页确认是否已发出，不要直接重发",
                           code=ErrorCode.UNKNOWN_OUTCOME)

    def _parse_created(self, data: dict[str, Any]) -> dict[str, Any]:
        code = data.get("code")
        if code == 0:
            d = data.get("data") or {}
            dyn_id = str(d.get("dyn_id_str") or d.get("dyn_id") or "")
            if not dyn_id:
                raise PublishError("发布返回成功但没有动态 id：先到 B站动态页确认，不要直接重发",
                                   code=ErrorCode.UNKNOWN_OUTCOME)
            d["dyn_id_str"] = dyn_id
            return d
        if self.net.created_status == 412 or code == -352:
            raise NeedsAction("发布请求被B站风控拦截（412/-352）：稍后用 --headed 重试前先确认动态页",
                              code=ErrorCode.RISK_CONTROL)
        if code == -101:
            raise LoginRequired(f"B站登录已失效：运行 mpub login bilibili --account {self.account}")
        if code == 4126021:
            raise PublishError("账号没有绑定手机，B站不允许发布动态：先在 B站 App 里绑定手机号")
        if code is None or code in (-500, -502, -503, -504):
            raise PublishError(f"发布接口返回异常（{code}: {data.get('message')}），可能已经发出："
                               "先到 B站动态页确认，不要直接重发", code=ErrorCode.UNKNOWN_OUTCOME)
        raise PublishError(f"B站拒绝发布（{code}: {data.get('message')}）")
