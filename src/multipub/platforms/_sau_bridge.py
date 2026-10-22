"""抖音 / 小红书适配器共用：本地校验工具、vendored SAU 的初始化、钩子、错误映射。

主进程会导入本模块（validate / doctor），所以这里只能有轻量依赖：
patchright、vendored uploader、loguru、Pillow 都在函数内部导入。
"""

from __future__ import annotations

import base64
import importlib.util
import re
import shutil
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Iterable, Iterator

from .._sau.uploader.base_video import BaseVideoUploader  # 纯标准库，导入很便宜
from ..models import (ErrorCode, Issue, Kind, LoginRequired, NeedsAction, PlatformPost,
                      PublishError, Result, Status, ValidationFailed)

if TYPE_CHECKING:
    from ..config import Settings
    from .base import JobContext, PlatformAdapter

VIDEO_EXTS = frozenset(BaseVideoUploader.SUPPORTED_VIDEO_EXTENSIONS)
IMAGE_EXTS = frozenset(BaseVideoUploader.SUPPORTED_IMAGE_EXTENSIONS)
MIN_SCHEDULE_LEAD = BaseVideoUploader.MIN_SCHEDULE_LEAD_TIME  # SAU 要求定时 > 现在 + 2 小时
SCHEDULE_MARGIN = timedelta(minutes=15)  # 排队、上传也要时间，太贴近下限时提醒
MAX_SCHEDULE_AHEAD = timedelta(days=14)
BAD_TAG_CHARS = (" ", "#", ",", "，", "\t")


# ================================================================ 校验（纯本地）

class Checker:
    """收集某个平台的校验问题。"""

    def __init__(self, platform: str):
        self.platform = platform
        self.issues: list[Issue] = []

    def error(self, field: str, message: str) -> None:
        self.issues.append(Issue("error", field, message, self.platform))

    def warn(self, field: str, message: str) -> None:
        self.issues.append(Issue("warning", field, message, self.platform))

    def length(self, field: str, text: str, limit: int, *, hard: bool = True, what: str = "") -> None:
        if len(text) > limit:
            msg = f"{what or field}最多 {limit} 字，当前 {len(text)} 字"
            if hard:
                self.error(field, msg)
            else:
                self.warn(field, msg + "，可能被平台截断")

    def ext(self, field: str, path: Path | None, allowed: Iterable[str]) -> None:
        if path is not None and path.suffix.lower() not in allowed:
            self.error(field, f"不支持的格式 {path.suffix or '(无扩展名)'}: {path.name}"
                              f"（支持 {', '.join(sorted(allowed))}）")

    def tags(self, tags: list[str], *, limit: int | None = None, soft_limit: int | None = None) -> None:
        if limit is not None and len(tags) > limit:
            self.error("tags", f"最多 {limit} 个标签，当前 {len(tags)} 个")
        elif soft_limit is not None and len(tags) > soft_limit:
            self.warn("tags", f"标签超过 {soft_limit} 个，多出的可能不被识别为话题")
        for tag in tags:
            if any(ch in tag for ch in BAD_TAG_CHARS):
                self.warn("tags", f"标签「{tag}」含空格、# 或逗号，会被拆成多个话题")

    def schedule(self, when: datetime | None, now: datetime | None = None) -> None:
        if when is None:
            return
        now = now or datetime.now()
        if when <= now + MIN_SCHEDULE_LEAD:  # 也覆盖已过去的时间（worker 里不会再跑 common_issues）
            self.error("schedule", "定时发布时间必须晚于当前时间 2 小时以上")
        elif when <= now + MIN_SCHEDULE_LEAD + SCHEDULE_MARGIN:
            self.warn("schedule", "定时时间离 2 小时下限不足 15 分钟，上传耗时后可能不满足要求")
        if when > now + MAX_SCHEDULE_AHEAD:
            self.warn("schedule", "定时时间超过 14 天，平台可能不接受")

    def options(self, options: dict[str, Any], known: Iterable[str], kind: Kind) -> None:
        known = set(known)
        for key in options:
            if key not in known:
                self.warn(f"options.{key}", f"未知参数（{kind.value} 不使用），已忽略")

    def bool_option(self, options: dict[str, Any], key: str) -> None:
        if key in options and not isinstance(options[key], bool):
            self.error(f"options.{key}", "应为 true 或 false")

    def str_option(self, options: dict[str, Any], key: str) -> None:
        value = options.get(key)
        if value is not None and not isinstance(value, str):
            self.error(f"options.{key}", "应为字符串")


def manifest_dir(post: PlatformPost) -> Path:
    return Path(post.source).parent if post.source else Path.cwd()


def option_path(post: PlatformPost, key: str) -> Path | None:
    """平台私有参数里的文件路径：相对于 post.yaml 所在目录。"""
    value = post.options.get(key)
    if value in (None, "", False):
        return None
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (manifest_dir(post) / p).resolve()


def check_option_file(c: Checker, post: PlatformPost, key: str, allowed: Iterable[str]) -> Path | None:
    path = option_path(post, key)
    if path is None:
        return None
    if not path.is_file():
        c.error(f"options.{key}", f"文件不存在: {path}")
    c.ext(f"options.{key}", path, allowed)
    return path


def doctor_items() -> list[dict[str, Any]]:
    """doctor 自检项（不开浏览器）：终端二维码依赖是否可用（可选）、stealth.min.js 是否随包分发。"""
    from .._sau.utils.base_social_media import STEALTH_JS_PATH

    has_cv = all(importlib.util.find_spec(m) is not None for m in ("cv2", "numpy"))
    return [
        {"name": "terminal_qr", "ok": True,
         "detail": "已安装 opencv，可在日志中打印终端二维码" if has_cv
         else "未安装 opencv（可选：pip install \"multipub[terminal-qr]\"）；扫码请看浏览器窗口或二维码图片"},
        {"name": "stealth_js", "ok": STEALTH_JS_PATH.is_file(), "detail": str(STEALTH_JS_PATH)},
    ]


def login_hint(platform: str, account: str) -> str:
    return f"mpub login {platform} --account {account}"


# ================================================================ worker 侧

UPLOAD_RESERVE = 600  # 任务总超时里留给上传以外步骤（开页面、填表、发布、短信）的时间
LOGIN_OVERHEAD = 120  # 登录任务里留给 cookie 校验、抓二维码、轮询开销的时间
SMS_TIMEOUT = 600.0  # 等短信验证码：Claude Code 要先注意到 needs_input 事件、再去问用户，5 分钟常常不够
SMS_RESERVE = 60  # 提交验证码之后还要点发布、等跳转、回写 cookie


def sau_timeouts(settings: "Settings", platform: str, kind: Kind,
                 job_timeout: float | None = None) -> dict[str, float]:
    """各等待步骤的截止时间（秒），可在 config.toml 的 [platforms.<平台>] 里覆盖。

    上传等待 = 本次任务的总超时（ctx.timeout，即 --timeout 或 timeouts.publish_<kind>）− 600 秒。"""
    cfg = settings.platform_config(platform)
    total = job_timeout or settings.timeout_for(f"publish_{kind.value}")
    defaults = {
        "page_timeout": 300.0,
        "upload_timeout": float(max(180, total - UPLOAD_RESERVE)),
        "publish_timeout": 240.0,
        "sms_timeout": SMS_TIMEOUT,
    }
    return {k: float(cfg.get(k, v)) for k, v in defaults.items()}


_EMOJI_PREFIX = re.compile(r"^\W+")


@contextmanager
def sau_session(ctx: "JobContext", business_name: str) -> Iterator[None]:
    """配置 vendored SAU，并把它的重要日志（SUCCESS 及以上）转成 ctx.log 事件。"""
    from loguru import logger

    from .._sau import conf

    conf.configure(ctx.settings, headless=ctx.headless)

    def sink(message: Any) -> None:
        record = message.record
        text = _EMOJI_PREFIX.sub("", record["message"]).strip()
        if text:
            ctx.log(text, level="warning" if record["level"].no >= 30 else "info")

    handler_id = logger.add(
        sink, level="SUCCESS", format="{message}", colorize=False,
        filter=lambda r: r["extra"].get("business_name") in (business_name, "sau"),
    )
    try:
        yield
    finally:
        logger.remove(handler_id)


def make_hooks(ctx: "JobContext", kind: Kind, *, sms_message: str = "") -> Any:
    from .._sau.runtime import Hooks

    timeouts = sau_timeouts(ctx.settings, ctx.platform, kind, ctx.timeout)
    sms_wait = timeouts["sms_timeout"]
    started = time.monotonic()  # ≈ worker 开始计任务总超时的时刻

    def on_sms(path: Path) -> None:
        # 等验证码不能超过任务剩下的时间（再留出提交后的发布时间）：让 SAU 自己的截止时间先到，
        # 报出 sms_code_required / unknown_outcome，而不是被 worker 的总超时直接打断；提示里的秒数也就是真的
        wait = sms_wait
        if ctx.timeout:
            left = ctx.timeout - (time.monotonic() - started) - SMS_RESERVE
            wait = min(wait, max(60.0, left))
        hooks.sms_timeout = wait
        note = f"{sms_message or '需要短信验证码'}（发布正在等待，{int(wait)} 秒内有效"
        if not ctx.headless:
            note += "；也可以直接在弹出的浏览器窗口里输入验证码并点「验证」"
        ctx.needs_input("sms_code", note + "；中途退出后重新发布会重新发送短信）", path=path)

    hooks = Hooks(
        before_publish_click=ctx.mark_submit_started,
        sms_code_required=on_sms,
        on_evidence=lambda p: ctx.evidence.append(str(p)),
        evidence_dir=ctx.run_dir,
        platform=ctx.platform,
        **timeouts,
    )
    return hooks


class QrRelay:
    """SAU 每次生成/刷新二维码都会回调：复制到 qrcode_dir/<平台>-<账号>.png 并上报 qrcode 事件。"""

    def __init__(self, ctx: "JobContext"):
        self.ctx = ctx
        self.dest = ctx.settings.qrcode_dir / f"{ctx.platform}-{ctx.account}.png"
        self.reported = 0

    def __call__(self, payload: dict[str, Any]) -> None:
        src = str(payload.get("image_path") or "")
        data_url = str(payload.get("image_data_url") or "")
        try:
            self.dest.parent.mkdir(parents=True, exist_ok=True)
            if src and Path(src).is_file():
                shutil.copyfile(src, self.dest)
            elif data_url.startswith("data:image/") and ";base64," in data_url:
                self.dest.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            else:
                return
        except (OSError, ValueError) as exc:
            self.ctx.log(f"保存二维码图片失败: {exc}", level="warning")
            return
        self.reported += 1
        if payload.get("fallback"):
            note = "没定位到二维码元素，这是登录页截图，请扫描其中的二维码"
            if not self.ctx.headless:
                note += "；也可以直接在弹出的浏览器窗口里扫码或登录"
        else:
            note = "二维码过期后会自动刷新，请以最新图片为准"
        self.ctx.qrcode(self.dest, note=note)


def qrcode_callback(ctx: "JobContext") -> tuple[QrRelay, Path]:
    relay = QrRelay(ctx)
    return relay, relay.dest


SetupFn = Callable[..., Awaitable[dict[str, Any]]]


async def run_login(adapter: "PlatformAdapter", setup_fn: SetupFn) -> Result:
    """调用 SAU 的 *_setup(handle=True)：cookie 仍有效则直接返回，否则扫码登录并保存 storage_state。"""
    ctx = adapter.ctx
    account_file = ctx.account_file
    account_file.parent.mkdir(parents=True, exist_ok=True)
    hint = login_hint(ctx.platform, ctx.account)
    callback, qr_path = qrcode_callback(ctx)
    budget = ctx.timeout or (ctx.settings.timeout_for("login") - 30)
    wait = int(max(60, budget - LOGIN_OVERHEAD))
    # SAU 只按轮询次数计时，cookie 校验、抓二维码等步骤另算，所以留出余量，免得被 worker 的总超时打断
    if ctx.headless:
        ctx.log("无头模式登录：请扫描上报的二维码图片；如需短信二次验证请去掉 --headless 重试")
    try:
        detail = await setup_fn(str(account_file), handle=True, return_detail=True,
                                qrcode_callback=callback, headless=ctx.headless, login_timeout=wait)
    finally:
        try:
            qr_path.unlink(missing_ok=True)
        except OSError as exc:  # 例如 Windows 上图片正被看图软件占用：不影响登录结果
            ctx.log(f"删除二维码图片失败（可手动删除）: {exc}", level="warning")

    status = detail.get("status")
    if detail.get("success") and account_file.is_file():
        msg = "登录仍有效，无需重新扫码" if status == "cookie_valid" else "扫码登录成功"
        return adapter.result(Status.OK, message=f"{msg}（{account_file}）")
    if status == "timeout":
        msg = f"等待扫码超时（约 {wait} 秒），请重新运行 {hint}"
        if not callback.reported:
            msg += "；本次没有拿到二维码图片" + ("，请去掉 --headless 在浏览器窗口里扫码" if ctx.headless else "")
        raise PublishError(msg, code=ErrorCode.TIMEOUT, retryable=True)
    if status == "cookie_invalid":
        raise LoginRequired(f"扫码流程结束但没有拿到有效登录态，请重试 {hint}")
    raise PublishError(f"登录失败: {detail.get('message') or status}", code=ErrorCode.INTERNAL, retryable=True)


async def run_check(adapter: "PlatformAdapter", cookie_auth: Callable[[str], Awaitable[bool]]) -> Result:
    ctx = adapter.ctx
    hint = login_hint(ctx.platform, ctx.account)
    if not ctx.account_file.is_file():
        raise LoginRequired(f"未登录：运行 {hint}")
    if not await cookie_auth(str(ctx.account_file)):
        raise LoginRequired(f"登录已失效（或网络异常导致校验失败）：运行 {hint}")
    return adapter.result(Status.OK, message="登录有效")


def precheck(adapter_cls: type["PlatformAdapter"], ctx: "JobContext", post: PlatformPost) -> None:
    """publish 前的廉价检查：SAU 要先开浏览器校验 cookie 才检查参数，这里提前拦住。"""
    errors = [i for i in adapter_cls.validate(post) if i.level == "error"]
    if errors:
        raise ValidationFailed("；".join(f"{i.field}: {i.message}" for i in errors))
    if not ctx.account_file.is_file():
        raise LoginRequired(f"未登录：运行 {login_hint(ctx.platform, ctx.account)}")


@asynccontextmanager
async def translate_errors(ctx: "JobContext") -> AsyncIterator[None]:
    """把 vendored 流程的异常映射成 multipub 的错误类型。其余异常交给 worker 兜底。"""
    from .._sau import runtime as rt

    hint = login_hint(ctx.platform, ctx.account)
    check_first = "请先到平台后台确认作品是否已发出，再决定是否重发"
    sms_hint = (f"验证码必须在发布进行中提交：收到短信验证提示后，在 sms_timeout 时限内运行 "
                f"mpub verify-code {ctx.platform} <验证码>（加 --headed 时也可以直接在浏览器窗口里输入）；"
                "重新发布会重新发送短信，之前的验证码作废")
    captcha_hint = "如需重发，请加 --headed 并在浏览器窗口里手动完成验证"
    try:
        yield
    except rt.StepTimeout as exc:
        # 点过发布之后，人机验证 / 短信验证 / 超时都说明结果未知：一律 unknown_outcome，
        # 发布记录会拦住不带 --force 的重发（needs_action 不会拦，可能重复发布）
        if exc.after_submit or ctx.submit_started:
            extra = ""
            if isinstance(exc, rt.SmsCodeTimeout):
                extra = f"；{sms_hint}"
            elif isinstance(exc, rt.CaptchaRequired):
                extra = f"；{captcha_hint}"
            where = "" if "已点击发布" in str(exc) else "（已点击过发布，结果未知）"
            raise PublishError(f"{exc}{where}。{check_first}{extra}",
                               code=ErrorCode.UNKNOWN_OUTCOME, retryable=False) from exc
        if isinstance(exc, rt.SmsCodeTimeout):
            raise NeedsAction(f"{exc}。{sms_hint}", code=ErrorCode.SMS_CODE_REQUIRED, retryable=False) from exc
        if isinstance(exc, rt.CaptchaRequired):
            raise NeedsAction(f"{exc}。请加 --headed 重新发布并手动完成验证", code=ErrorCode.CAPTCHA,
                              retryable=False) from exc
        if isinstance(exc, rt.CookieInvalid):
            raise LoginRequired(f"{exc}。请重新登录：{hint}") from exc
        raise PublishError(str(exc), code=ErrorCode.TIMEOUT, retryable=True) from exc
    except rt.PublishRejected as exc:
        raise PublishError(f"{exc}。作品应未创建；建议先到平台后台确认，再修改内容后重发",
                           code=ErrorCode.PLATFORM_ERROR, retryable=False) from exc
    except rt.DeclarationFailed as exc:  # 发布前拒绝：消息里已写明怎么处理
        raise ValidationFailed(str(exc)) from exc
    except rt.CookieInvalid as exc:
        raise LoginRequired(f"登录已失效：运行 {hint}") from exc
    except rt.UploadFailed as exc:
        raise PublishError(str(exc), code=ErrorCode.PLATFORM_ERROR,
                           retryable=not ctx.submit_started) from exc
    except (ValueError, FileNotFoundError) as exc:  # SAU 的参数校验错误
        if ctx.submit_started:
            raise
        raise ValidationFailed(str(exc)) from exc
