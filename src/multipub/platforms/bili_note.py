"""B站图文动态：浏览器模式（默认）。

在 https://t.bilibili.com/ 的动态发布框里填标题、正文、传图，点「发布」，
从 /x/dynamic/feed/create/dyn 的响应里拿 dyn_id_str -> https://www.bilibili.com/opus/<id>。

选择器来自 2026-10-20 线上发布器脚本（dyn-home index.ad97d12f.js），**未在真实账号上验证**，
所以每一步都有多个候选、明确的报错和截图。定时发布 / AI 声明 / 图片模式没有稳定的 DOM 可点
（自绘的日期时间选择器、级联菜单），改为直接设置发布器 Vue 组件的状态（主 world 里执行）。

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
    check: dict[str, Any] | None = None
    create_sent: bool = False
    created: dict[str, Any] | None = None
    created_status: int | None = None

    def on_request(self, req: Any) -> None:
        if CREATE_DYN_PATH in req.url and req.method == "POST":
            self.create_sent = True

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


class BrowserNotePublisher:
    def __init__(self, ctx: JobContext, *, account: str):
        self.ctx = ctx
        self.account = account
        self.net = _NetTracker()

    async def publish(self, *, storage_state: dict[str, Any], title: str, text: str, images: list[Path],
                      timer_pub_time: int | None, aigc: bool, pic_mode: int | None) -> dict[str, Any]:
        from ..browser import open_context, snapshot

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
        return await self._wait_created(page)

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

    async def _wait_created(self, page: Any) -> dict[str, Any]:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            created = self.net.created
            if created is not None:
                return self._parse_created(created)
            check = self.net.check
            if check is not None and check.get("code") not in (0, None):
                # submit_check 没通过，发布请求根本没有发出
                raise PublishError(f"B站发布前检查未通过（{check.get('code')}: {check.get('message')}）")
            if await _any_visible(page, SEL_CAPTCHA):
                raise NeedsAction("B站弹出了人机验证：加 --headed 重新发布前，先到动态页确认是否已发出",
                                  code=ErrorCode.CAPTCHA)
            await asyncio.sleep(0.5)
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
        if code is None:
            raise PublishError(f"发布接口返回异常（{data.get('message')}）：先到 B站动态页确认，不要直接重发",
                               code=ErrorCode.UNKNOWN_OUTCOME)
        raise PublishError(f"B站拒绝发布（{code}: {data.get('message')}）")
