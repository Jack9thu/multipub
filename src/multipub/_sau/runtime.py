"""multipub 新增（非上游文件）：给 vendored SAU 流程用的钩子、截止时间与异常。

上游的发布/上传循环都是 ``while True``，页面改版时会一直卡住。这里提供：

- ``Hooks``：适配器注入的回调（即将点击「发布」、需要短信验证码、保存证据）与各阶段超时；
- ``Deadline``：每个等待循环都带截止时间；
- ``PublishGuard``：限制最终「发布」按钮的重复点击（上游每 3.5 秒点一次，可能重复发布）；
- ``CaptchaWatch``：有头模式下页面出现人机验证时，等用户在窗口里完成，不点发布、不判被拒；
- ``toggle_state()`` / ``input_value()`` / ``schedule_value_matches()``：核实定时发布的开关和时间确实设上了；
- ``fail_step()`` / ``fail_rejected()``：超时或被拒后截图、判断卡在哪（登录页 / 人机验证 / 普通超时），
  再抛出对应异常；
- ``save_state_quietly()`` / ``close_quietly()``：收尾步骤出错不改变发布结果；
- 异常类型：适配器据此映射为 LoginRequired / NeedsAction / UNKNOWN_OUTCOME 等。

本模块不导入 patchright，可在没有浏览器的环境里单独测试。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NoReturn

from .utils.log import sau_logger


# ---------------------------------------------------------------- exceptions

class SauError(RuntimeError):
    """vendored 流程抛出的、适配器需要区分处理的错误。"""


class CookieInvalid(SauError):
    """cookie 文件不存在或已失效，需要重新扫码登录。"""


class UploadFailed(SauError):
    """平台提示上传失败且重试次数用完。"""


class StepTimeout(SauError, TimeoutError):
    """某个等待步骤超过截止时间。after_submit=True 表示已经点过「发布」，结果未知。"""

    def __init__(self, message: str, *, step: str = "", after_submit: bool = False,
                 screenshot: str | None = None):
        super().__init__(message)
        self.step = step
        self.after_submit = after_submit
        self.screenshot = screenshot


class SmsCodeTimeout(StepTimeout):
    """抖音弹出短信验证，但在截止时间内没有拿到验证码。"""


class CaptchaRequired(StepTimeout):
    """页面停在人机验证（滑块等），需要人工处理。"""


class LoginPageDetected(StepTimeout, CookieInvalid):
    """流程中途被踢回登录页。"""


class SmsVerifyFailed(SmsCodeTimeout):
    """验证码提交了几轮，短信验证弹窗仍在（验证码错误/过期）。"""


class PublishRejected(SauError):
    """点击「发布」后平台接口明确返回失败，且之后没有弹出验证：作品没有创建。"""

    def __init__(self, message: str, *, screenshot: str | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class DeclarationFailed(SauError):
    """调用方明确要求的设置（抖音自主声明、小红书来源转载、关闭头条同步、定时发布）没能设置上，拒绝继续发布。

    消息里已写明处理办法，适配器原样转成 validation 错误。"""


class ScheduleFailed(DeclarationFailed):
    """定时发布的开关/时间没能核实设置上（点击被吞掉会变成立即发布），在点击「发布」之前拒绝继续。"""


# ---------------------------------------------------------------- hooks

@dataclass
class Hooks:
    """适配器注入的回调与超时（秒）。回调都是同步函数，异常会被记录后忽略。"""

    before_publish_click: Callable[[], Any] | None = None  # 即将第一次点击最终「发布」
    sms_code_required: Callable[[Path], Any] | None = None  # 需要短信验证码，参数是输入文件
    on_evidence: Callable[[Path], Any] | None = None  # 保存了一张证据截图
    evidence_dir: Path | None = None
    platform: str = "sau"
    page_timeout: float = 300  # 选好文件后等待进入发布表单页
    upload_timeout: float = 3000  # 等待视频/图片上传完成
    publish_timeout: float = 240  # 点击「发布」后等待成功跳转
    sms_timeout: float = 600  # 等待用户提供短信验证码（Claude Code 要先看到 needs_input 再去问用户）
    poll_interval: float = 2.0  # 轮询间隔：验证码文件、抖音等发布表单出现
    reclick_gap: float = 20  # 上次点击「发布」没有任何反应时，至少隔这么久才再点
    max_publish_clicks: int = 3  # 最终「发布」按钮最多点几次（含被平台弹窗拦下的那次）
    reject_grace: float = 15  # 发布接口明确失败后，等这么久仍没有验证弹窗就判定为被拒

    def publish_guard(self) -> "PublishGuard":
        return PublishGuard(reclick_gap=self.reclick_gap, max_clicks=self.max_publish_clicks)

    def call(self, name: str, *args: Any) -> None:
        fn = getattr(self, name)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - 回调失败不应打断发布流程
            sau_logger.warning(f"回调 {name} 出错: {exc!r}")


class Deadline:
    """单调时钟截止时间。"""

    def __init__(self, seconds: float, step: str):
        self.seconds = float(seconds)
        self.step = step
        self._end = time.monotonic() + self.seconds

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._end

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def reset(self) -> None:
        self._end = time.monotonic() + self.seconds


class PublishGuard:
    """限制最终「发布」按钮的点击次数，避免重复发布。

    上游的发布循环每隔约 3.5 秒就点一次「发布」，直到跳到成功页；平台处理慢时可能提交两次。
    第一次点击之后，只有下面两种情况才允许再点：

    - 平台明确拦下了上一次点击（弹出短信验证、提示先设封面）：立即允许再点一次；
    - 距上次点击超过 ``reclick_gap`` 秒，且期间没有观察到发布请求（说明上次点击没生效）。

    已拿到成功回执、有发布请求还没返回、或点击次数达到 ``max_clicks`` 时一律不再点。
    发布请求由 uploader 被动监听页面网络事件后通过 request_sent / request_done 告知；
    接口路径未在真实账号上验证，监听不到时只剩时间间隔和次数上限两道限制。
    """

    def __init__(self, *, reclick_gap: float = 20.0, max_clicks: int = 3,
                 clock: Callable[[], float] = time.monotonic):
        self.reclick_gap = float(reclick_gap)
        self.max_clicks = int(max_clicks)
        self._clock = clock
        self.clicks = 0
        self.last_click: float | None = None
        self.inflight = 0
        self.requests_since_click = 0
        self.succeeded = False
        self.unblocked = False
        self.rejection: str | None = None
        self._rejected_at: float | None = None

    def may_click(self) -> bool:
        if self.clicks == 0:
            return True
        if self.succeeded or self.inflight > 0 or self.clicks >= self.max_clicks:
            return False
        if self.unblocked:
            return True
        if self.requests_since_click:  # 上次点击已经发出了发布请求：只等结果，不再点
            return False
        return self._clock() - (self.last_click or 0.0) >= self.reclick_gap

    def clicked(self) -> None:
        """即将点击（先记账：点击抛异常时也可能已经触发）。"""
        self.clicks += 1
        self.last_click = self._clock()
        self.requests_since_click = 0
        self.unblocked = False

    def click_returned(self) -> None:
        """点击调用返回（click() 可能等了很久按钮才可点）：间隔从这里重新算。"""
        self.last_click = self._clock()

    def blocked_by_platform(self) -> None:
        """平台弹窗拦下了上一次点击（短信验证 / 先设封面）：允许再点一次，之前的接口失败不算数。"""
        if self.clicks:
            self.unblocked = True
        self.clear_rejection()

    def clear_rejection(self) -> None:
        """页面弹出了验证：之前的接口失败多半就是「需要验证」，不算数（不额外允许再点）。"""
        self.rejection = None
        self._rejected_at = None

    def request_sent(self) -> None:
        self.inflight += 1
        self.requests_since_click += 1
        # 被拦下之后页面自己（或用户在窗口里）又提交了一次：不再替它补点
        self.unblocked = False

    def request_done(self, ok: bool | None, message: str = "") -> None:
        """ok=True 成功回执；False 平台明确失败；None 结果不明（请求失败、响应解析不了）。"""
        self.inflight = max(0, self.inflight - 1)
        if ok:
            self.succeeded = True
        elif ok is False:
            self.rejection = message or "发布接口返回失败"
            self._rejected_at = self._clock()

    def rejection_settled(self, grace: float) -> str | None:
        """平台明确拒绝且之后 grace 秒内没有新进展（无成功、无进行中的请求、无验证弹窗）时返回原因。"""
        if (self.rejection is None or self._rejected_at is None or self.succeeded
                or self.inflight > 0 or self.unblocked):
            return None
        return self.rejection if self._clock() - self._rejected_at >= grace else None


# ---------------------------------------------------------------- 页面检查

async def publish_button_ready(button: Any) -> bool:
    """最终发布按钮唯一、可见、可用时，点击才可能真正触发发布。

    disabled 的 <button> 被 force 点击时什么也不发生，非 force 点击会一直等按钮可用，
    选择器命中多个按钮时 click() 直接报错——这些都不算一次「点击」：不记账、不标记已提交，继续等。
    """
    try:
        if await button.count() != 1:
            return False
        if not (await button.is_visible() and await button.is_enabled()):
            return False
        if await button.get_attribute("disabled") is not None:
            return False
        return (await button.get_attribute("aria-disabled") or "").strip().lower() != "true"
    except Exception:  # noqa: BLE001 - 页面跳转中 locator 可能失效
        return False


_VISIBLE_TEXT_JS = """({texts, exact, exclude}) => {
    const skip = '[contenteditable="true"], input, textarea' + (exclude ? ', ' + exclude : '');
    for (const el of document.querySelectorAll('body *')) {
        if (el.closest(skip)) continue;
        const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
        if (!own || !texts.some(t => exact ? own === t : own.includes(t))) continue;
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (r.width && r.height && st.visibility !== 'hidden' && st.display !== 'none') return own.slice(0, 80);
    }
    return '';
}"""


async def find_visible_text(page: Any, texts: list[str], *, exact: bool = False, exclude: str = "") -> str:
    """页面上第一个可见、且直接文字包含（exact 时等于）texts 之一的元素的文字；没有返回 ""。

    输入框、contenteditable 编辑区和 exclude 选择器里的不算：用来确认声明等设置「显示出来了」，
    正文里碰巧写了同样的字、输入框里的值都不会被当成设置成功。
    """
    texts = [t for t in texts if t]
    if page is None or not texts:
        return ""
    try:
        found = await page.evaluate(_VISIBLE_TEXT_JS, {"texts": texts, "exact": exact, "exclude": exclude})
    except Exception:  # noqa: BLE001
        return ""
    return found if isinstance(found, str) else ""


_TOGGLE_STATE_JS = """el => {
    const nodes = [el, ...el.querySelectorAll('*')];
    for (const attr of ['aria-checked', 'data-checked']) {
        for (const n of nodes) {
            const v = (n.getAttribute(attr) || '').toLowerCase();
            if (v === 'true' || v === 'false') return v === 'true';
        }
    }
    const box = el.matches('input[type=radio], input[type=checkbox]') ? el
        : el.querySelector('input[type=radio], input[type=checkbox]');
    if (box) return !!box.checked;
    for (const n of nodes) {
        for (const c of n.classList) {
            const parts = c.toLowerCase().split(/[-_]+/);
            if (parts.some(p => p === 'checked' || p === 'active' || p === 'selected')) return true;
        }
    }
    return null;
}"""


async def toggle_state(locator: Any) -> bool | None:
    """单选项 / 开关是否处于选中状态：True / False；页面结构判断不了时返回 None。

    依次看 aria-checked、data-checked（抖音 label.radio-*）、内部原生 radio/checkbox、
    类名里的 checked / active / selected 片段（只能证明「选中」，证明不了「没选中」）。
    """
    try:
        if not await locator.count():
            return None
        state = await locator.evaluate(_TOGGLE_STATE_JS)
    except Exception:  # noqa: BLE001 - 页面跳转中 locator 可能失效
        return None
    return state if isinstance(state, bool) else None


async def input_value(locator: Any) -> str:
    """输入框当前的值；选择器命中的是外层容器时读里面的 input；读不到返回 ""。"""
    for read in (lambda: locator.input_value(timeout=3000),
                 lambda: locator.locator("input").first.input_value(timeout=3000),
                 lambda: locator.get_attribute("value")):
        try:
            value = await read()
        except Exception:  # noqa: BLE001 - 不是 input 元素 / 页面跳转中
            continue
        if value is not None:
            return str(value)
    return ""


def schedule_value_matches(value: Any, when: datetime) -> bool:
    """日期时间输入框显示的值是否就是 when（精确到分钟）。

    只比较其中的数字（年 月 日 时 分），兼容「2026-10-23 08:05」「2026/10/23 8:05」「2026年10月23日 08:05」等写法。
    """
    nums = [int(n) for n in re.findall(r"\d+", str(value or ""))]
    return nums[:5] == [when.year, when.month, when.day, when.hour, when.minute]


# ---------------------------------------------------------------- evidence

async def save_screenshot(page: Any, hooks: Hooks, name: str) -> str | None:
    """整页截图到 hooks.evidence_dir；失败只记日志。"""
    if page is None or hooks.evidence_dir is None:
        return None
    path = Path(hooks.evidence_dir) / f"{hooks.platform}-{name}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
    except Exception as exc:  # noqa: BLE001 - 截图是尽力而为
        sau_logger.warning(f"保存截图失败 {path}: {exc!r}")
        return None
    hooks.call("on_evidence", path)
    return str(path)


# 卡住时用于判断原因的页面特征（只在超时后用来归类错误，不影响正常流程）
CAPTCHA_TEXTS = ("请完成下列验证后继续", "请完成安全验证", "拖动下方滑块", "按住左边按钮拖动", "请完成验证")
CAPTCHA_SELECTORS = ("#captcha_container", ".captcha_verify_container", ".red-captcha", "iframe[src*='captcha']")
LOGIN_TEXTS = ("扫码登录", "手机号登录", "APP扫一扫登录")


async def _any_visible(page: Any, *, texts: tuple[str, ...] = (), selectors: tuple[str, ...] = ()) -> bool:
    candidates = [page.get_by_text(t).first for t in texts] + [page.locator(s).first for s in selectors]
    for loc in candidates:
        try:
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:  # noqa: BLE001 - 页面在跳转时 locator 可能失效
            continue
    return False


async def diagnose(page: Any) -> str:
    """粗略判断页面卡在哪：'captcha' / 'login' / ''。"""
    if page is None:
        return ""
    if await _any_visible(page, texts=CAPTCHA_TEXTS, selectors=CAPTCHA_SELECTORS):
        return "captcha"
    if await _any_visible(page, texts=LOGIN_TEXTS):
        return "login"
    return ""


class CaptchaWatch:
    """有头模式（--headed）下页面停在人机验证时，等用户在浏览器窗口里完成。

    上游的发布循环照常点「发布」；发布接口明确失败后 15 秒（``reject_grace``）就判为被拒，
    用户根本来不及拖滑块。这里：验证显示期间不点「发布」、之前的接口失败不算数；
    第一次看到验证时把发布截止时间重置一次（用户最多有 ``publish_timeout`` 秒），之后照常超时。
    无头模式下用户看不到窗口，行为不变（超时或被拒后按人机验证报错）。
    """

    def __init__(self, *, headless: bool, guard: PublishGuard, logger: Any = None):
        self.headless = bool(headless)
        self.guard = guard
        self.log = logger or sau_logger
        self.showing = False
        self.extended = False

    async def blocking(self, page: Any, deadline: Deadline) -> bool:
        """页面正显示人机验证（且是有头模式）时返回 True：调用方本轮不点「发布」，稍后再看。"""
        if self.headless or page is None:
            return False
        if await diagnose(page) != "captcha":
            if self.showing:
                self.showing = False
                self.log.info("人机验证已消失，继续等待发布结果")
            return False
        self.guard.clear_rejection()
        if not self.showing:
            self.showing = True
            if not self.extended:
                self.extended = True
                deadline.reset()
            self.log.warning(f"页面出现人机验证：请在浏览器窗口里完成，最多等 {int(deadline.remaining())} 秒"
                             "（验证期间不会点「发布」）")
        return True


async def fail_step(page: Any, hooks: Hooks, deadline: Deadline, *, after_submit: bool,
                    detail: str = "") -> NoReturn:
    """等待超时：截图、归类，然后抛出异常（绝不静默当成功）。"""
    reason = await diagnose(page)
    shot = await save_screenshot(page, hooks, f"timeout-{deadline.step}")
    where = "（已点击发布，结果未知）" if after_submit else ""
    base = f"{deadline.step} 超过 {int(deadline.seconds)} 秒{where}"
    if detail:
        base += f"；{detail}"
    sau_logger.error(base + (f"；截图: {shot}" if shot else ""))
    kw = {"step": deadline.step, "after_submit": after_submit, "screenshot": shot}
    if reason == "captcha":
        raise CaptchaRequired(base + "；页面停在人机验证", **kw)
    if reason == "login":
        raise LoginPageDetected(base + "；页面跳回了登录页", **kw)
    raise StepTimeout(base, **kw)


async def fail_rejected(page: Any, hooks: Hooks, message: str) -> NoReturn:
    """点击发布后平台明确拒绝：截图后抛 PublishRejected（页面停在验证/登录页时按那两类处理）。"""
    reason = await diagnose(page)
    shot = await save_screenshot(page, hooks, "rejected")
    base = f"平台拒绝发布：{message}"
    sau_logger.error(base + (f"；截图: {shot}" if shot else ""))
    kw = {"step": "发布", "after_submit": True, "screenshot": shot}
    if reason == "captcha":
        raise CaptchaRequired(base + "；页面停在人机验证", **kw)
    if reason == "login":
        raise LoginPageDetected(base + "；页面跳回了登录页", **kw)
    raise PublishRejected(base, screenshot=shot)


# ---------------------------------------------------------------- 收尾

async def save_state_quietly(context: Any, path: str) -> bool:
    """发布成功后回写 cookie；失败只告警（不能把已经成功的发布报成失败）。"""
    try:
        await context.storage_state(path=path)
    except Exception as exc:  # noqa: BLE001
        sau_logger.warning(f"发布已成功，但回写 cookie 失败（下次可能需要重新登录）: {exc!r}")
        return False
    return True


async def close_quietly(*targets: Any) -> None:
    """关闭 context / browser；出错只记日志，避免掩盖原来的异常或把成功变成失败。"""
    for target in targets:
        if target is None:
            continue
        try:
            await target.close()
        except Exception as exc:  # noqa: BLE001
            sau_logger.warning(f"关闭浏览器出错（忽略）: {exc!r}")


async def goto_resilient(page: Any, url: str, *, timeout_ms: int = 60_000, attempts: int = 2) -> None:
    """multipub patch: 打开页面只等 DOM 就绪（不等统计脚本等资源的 load 事件），慢网络下超时重试一次。

    上游多处 page.goto(url) 用默认的 load + 30 秒，国内外慢网络下抖音/小红书首页经常等不到 load。
    """
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as exc:  # noqa: BLE001 - patchright TimeoutError 不是内置 TimeoutError
            if attempt >= attempts or "Timeout" not in type(exc).__name__ + str(exc):
                raise
            sau_logger.warning(f"打开 {url} 超时，重试一次")

