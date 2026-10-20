"""multipub 新增（非上游文件）：给 vendored SAU 流程用的钩子、截止时间与异常。

上游的发布/上传循环都是 ``while True``，页面改版时会一直卡住。这里提供：

- ``Hooks``：适配器注入的回调（即将点击「发布」、需要短信验证码、保存证据）与各阶段超时；
- ``Deadline``：每个等待循环都带截止时间；
- ``fail_step()``：超时后截图、判断卡在哪（登录页 / 人机验证 / 普通超时），再抛出对应异常；
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
