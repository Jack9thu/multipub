"""multipub 新增（非上游文件）：给 vendored SAU 流程用的钩子、截止时间与异常。

上游的发布/上传循环都是 ``while True``，页面改版时会一直卡住。这里提供：

- ``Hooks``：适配器注入的回调（即将点击「发布」、需要短信验证码、保存证据）与各阶段超时；
- ``Deadline``：每个等待循环都带截止时间；
- ``PublishGuard``：限制最终「发布」按钮的重复点击（上游每 3.5 秒点一次，可能重复发布）；
- ``fail_step()`` / ``fail_rejected()``：超时或被拒后截图、判断卡在哪（登录页 / 人机验证 / 普通超时），
  再抛出对应异常；
- ``save_state_quietly()`` / ``close_quietly()``：收尾步骤出错不改变发布结果；
- 异常类型：适配器据此映射为 LoginRequired / NeedsAction / UNKNOWN_OUTCOME 等。

本模块不导入 patchright，可在没有浏览器的环境里单独测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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
    """调用方要求的声明（如抖音自主声明）没能设置上，拒绝继续发布。"""


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
    sms_timeout: float = 300  # 等待用户提供短信验证码
    poll_interval: float = 2.0  # 轮询验证码文件的间隔
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
        self.rejection = None
        self._rejected_at = None

    def request_sent(self) -> None:
        self.inflight += 1
        self.requests_since_click += 1

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
