"""B站适配器。

- 视频：biliup 命令行上传并投稿（版本固定，见 bili_biliup）。
- 图文：动态（opus），默认在 t.bilibili.com 发布框里用浏览器发（bili_note）；
  platforms.bilibili.mode: http 走实验性的 Web 接口（bili_http）。
- 登录：TV 端扫码（与 biliup 的「扫码登录」同一套接口），不需要终端也不需要浏览器，
  生成 biliup 兼容的登录文件 <home>/accounts/bilibili/<账号>.json，视频和图文共用。
  需要时也可以在真实终端里跑 biliup 自己的交互式登录（run_interactive_login）。

本模块顶层只导入标准库和核心模块；httpx / patchright / biliup 相关代码在方法里按需导入。
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..models import (ErrorCode, Issue, Kind, LoginRequired, PlatformPost, PublishError, Result,
                      Status, Unsupported, ValidationFailed)
from .base import JobContext, PlatformAdapter

NAME = "bilibili"

# ---- 视频限制
VIDEO_TITLE_MAX = 80
VIDEO_DESC_MAX = 2000
VIDEO_TAGS_MAX = 12
VIDEO_TAG_LEN_MAX = 20
VIDEO_SCHEDULE_MIN = timedelta(hours=2)
VIDEO_SCHEDULE_MAX = timedelta(days=15)
VIDEO_SCHEDULE_SAFE = timedelta(hours=2, minutes=30)
VIDEO_EXTS = {".mp4", ".flv", ".avi", ".wmv", ".mov", ".webm", ".mpeg", ".mpg", ".mkv",
              ".m4v", ".ts", ".rm", ".rmvb", ".3gp"}
COVER_EXTS = {".jpg", ".jpeg", ".png"}
COVER_BYTES_WARN = 5 * 1024 * 1024
CREATION_STATEMENTS = {-1, 1, 2, 3, 4}

# ---- 图文（动态）限制，来自线上发布器配置：标题 20、正文 1000、9 张图、单张 20MB、定时 5 分钟~7 天
NOTE_TITLE_MAX = 20
NOTE_TEXT_MAX = 1000
NOTE_IMAGES_MAX = 9
NOTE_IMAGE_BYTES_MAX = 20 * 1024 * 1024
NOTE_GIF_BYTES_MAX = 8 * 1024 * 1024
NOTE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
NOTE_SCHEDULE_MIN = timedelta(minutes=5)
NOTE_SCHEDULE_SAFE = timedelta(minutes=10)
NOTE_SCHEDULE_MAX = timedelta(days=7)
NOTE_MODES = ("browser", "http")

VIDEO_OPTIONS = frozenset({"tid", "tid_v2", "copyright", "source", "dynamic", "no_reprint", "is_only_self",
                           "selection_reply", "close_reply", "close_danmu", "creation_statement",
                           "extra_fields", "line", "limit", "submit"})
NOTE_OPTIONS = frozenset({"mode", "pic_mode", "tags_in_text", "confirm_rules"})
# config.toml [platforms.bilibili] 里的同意项：允许 multipub 代为确认《动态发布规范》
CONFIRM_RULES_CONFIG = "confirm_dynamic_rules"

LOGIN_POLL_SECONDS = 2.0
QR_MAX_REFRESH = 3

# 视频：biliup 比本次任务的总超时早这么多秒被杀掉，好给出明确结果；剩余时间不到 VIDEO_MIN_BUDGET 就不启动
VIDEO_TIMEOUT_MARGIN = 30
VIDEO_MIN_BUDGET = 60
TOKEN_PRECHECK_TIMEOUT = 10  # 上传前的令牌预检（每个网络阶段；整体最多两倍）


def _login_hint(account: str) -> str:
    return f"运行 mpub login bilibili --account {account}"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def note_text(post: PlatformPost) -> str:
    """动态正文：body 后面追加 #标签#（B站话题写法），已经写在正文里的不重复加。"""
    text = post.body.strip()
    if post.options.get("tags_in_text", True) is False or not post.tags:
        return text
    extra = [f"#{t}#" for t in post.tags if f"#{t}#" not in text]
    if not extra:
        return text
    return (text + "\n" if text else "") + " ".join(extra)


class _ProgressLog:
    """biliup 输出：全部写 stderr（worker 日志），重要的行和心跳低频转发给 ctx.log。

    on_uploaded：看到「视频已全部传完」（之后 biliup 马上发投稿请求）时调用一次，用来标记 submit_started。"""

    IMPORTANT = ("user:", "Upload completed", "投稿成功", "ERROR", "WARN", "Error", "限流")

    def __init__(self, ctx: JobContext, min_interval: float = 5.0,
                 on_uploaded: Callable[[], None] | None = None):
        self.ctx = ctx
        self.min_interval = min_interval
        self.on_uploaded = on_uploaded
        self.started = time.monotonic()
        self.last = 0.0

    def __call__(self, line: str | None) -> None:
        now = time.monotonic()
        if line is None:
            self.ctx.log(f"biliup 上传中……已用时 {int(now - self.started) // 60} 分钟")
            self.last = now
            return
        print(line, file=sys.stderr, flush=True)
        if self.on_uploaded is not None:
            from .bili_biliup import marks_submit_phase

            if marks_submit_phase(line):
                self.on_uploaded()
                self.on_uploaded = None
        if any(m in line for m in self.IMPORTANT) and now - self.last >= self.min_interval:
            self.ctx.log(f"biliup: {line[-200:]}", to_stderr=False)  # 上面已经原样写过 stderr
            self.last = now


class BilibiliAdapter(PlatformAdapter):
    name = NAME
    display_name = "B站"
    kinds = frozenset({Kind.VIDEO, Kind.NOTE})
    # 登录走 worker 里的 TV 扫码（无需终端）；biliup 交互式登录见 run_interactive_login
    interactive_login = False

    # ================================================================ 校验（纯本地）
    @classmethod
    def validate(cls, post: PlatformPost) -> list[Issue]:
        issues: list[Issue] = []

        def err(f: str, m: str) -> None:
            issues.append(Issue("error", f, m, NAME))

        def warn(f: str, m: str) -> None:
            issues.append(Issue("warning", f, m, NAME))

        known = VIDEO_OPTIONS if post.kind is Kind.VIDEO else NOTE_OPTIONS
        for key in post.options:
            if key not in known:
                warn(f"platforms.bilibili.{key}",
                     f"未知参数，会被忽略（{post.kind.value} 支持: {', '.join(sorted(known))}）")
        if post.kind is Kind.VIDEO:
            cls._validate_video(post, err, warn)
        elif post.kind is Kind.NOTE:
            cls._validate_note(post, err, warn)
        return issues

    @staticmethod
    def _validate_video(post: PlatformPost, err: Callable[[str, str], None],
                        warn: Callable[[str, str], None]) -> None:
        o = post.options
        title = post.title.strip()
        if not title:
            err("title", "B站视频必须有标题")
        elif len(title) > VIDEO_TITLE_MAX:
            err("title", f"标题最多 {VIDEO_TITLE_MAX} 字（当前 {len(title)}）")
        if len(post.body) > VIDEO_DESC_MAX:
            err("body", f"简介最多 {VIDEO_DESC_MAX} 字（当前 {len(post.body)}）")
        elif not post.body.strip():
            warn("body", "简介为空，将使用标题作为简介（B站要求简介非空）")
        if not post.tags:
            err("tags", "B站投稿至少需要 1 个标签")
        elif len(post.tags) > VIDEO_TAGS_MAX:
            err("tags", f"标签最多 {VIDEO_TAGS_MAX} 个（当前 {len(post.tags)}）")
        for tag in post.tags:
            if len(tag) > VIDEO_TAG_LEN_MAX:
                err("tags", f"标签「{tag}」超过 {VIDEO_TAG_LEN_MAX} 字")
            if "," in tag or "，" in tag:
                err("tags", f"标签「{tag}」不能含逗号（biliup 用逗号分隔标签）")

        tid = _as_int(o.get("tid"))
        if o.get("tid") in (None, ""):
            err("platforms.bilibili.tid",
                "必须填写分区 id（如 231=计算机技术、21=日常），见 docs/platforms/bilibili.md")
        elif tid is None or not 0 < tid <= 65535:
            err("platforms.bilibili.tid", f"分区 id 必须是正整数，收到 {o.get('tid')!r}")
        if o.get("tid_v2") not in (None, "") and (_as_int(o.get("tid_v2")) or 0) <= 0:
            err("platforms.bilibili.tid_v2", f"新版分区 id 必须是正整数，收到 {o.get('tid_v2')!r}")
        copyright_ = _as_int(o.get("copyright", 1))
        if copyright_ not in (1, 2):
            err("platforms.bilibili.copyright", "只能是 1（自制）或 2（转载）")
        elif copyright_ == 2 and not str(o.get("source") or "").strip():
            err("platforms.bilibili.source", "转载（copyright: 2）必须填写转载来源 source")
        statement = o.get("creation_statement")
        if statement not in (None, "") and _as_int(statement) not in CREATION_STATEMENTS:
            err("platforms.bilibili.creation_statement",
                "只能是 -1（无）/ 1（AI）/ 2（虚构演绎）/ 3（营销）/ 4（个人观点）")
        elif post.ai_generated and statement in (None, ""):
            warn("ai_generated", "将以创作声明 id=1（AI）提交；该字段未在真实账号验证，发布后请到稿件页确认")
        elif post.ai_generated and _as_int(statement) != 1:
            warn("platforms.bilibili.creation_statement",
                 f"ai_generated 为 true，但创作声明设成了 {statement}（不是 1=AI），不会声明 AI 生成")
        if o.get("extra_fields") is not None and not isinstance(o.get("extra_fields"), dict):
            err("platforms.bilibili.extra_fields", "必须是映射（会作为 JSON 合并进投稿参数）")

        from .bili_biliup import APP_ONLY_FLAGS, LINE_CHOICES, SUBMIT_CHOICES, VIDEO_FLAGS

        for key in VIDEO_FLAGS:  # "false" / "no" 之类的字符串不能当成 true
            if o.get(key) is not None and not isinstance(o[key], bool):
                err(f"platforms.bilibili.{key}", f"只能是 true 或 false，收到 {o[key]!r}")
        if o.get("line") and str(o["line"]).lower() not in LINE_CHOICES:
            err("platforms.bilibili.line", f"上传线路只能是: {', '.join(LINE_CHOICES)}")
        submit = str(o.get("submit") or "app").lower()
        if submit not in SUBMIT_CHOICES:
            err("platforms.bilibili.submit", f"投稿接口只能是: {', '.join(SUBMIT_CHOICES)}")
        elif submit != "app":
            ignored = [k for k in APP_ONLY_FLAGS if o.get(k) is True]
            if ignored:
                warn("platforms.bilibili.submit",
                     f"{' / '.join(ignored)} 只在 app 投稿接口生效（biliup 注明），submit: {submit} 时不起作用")
        if o.get("limit") not in (None, "") and not 1 <= (_as_int(o.get("limit")) or 0) <= 64:
            err("platforms.bilibili.limit", "并发数应为 1~64 的整数")

        if post.video is not None and post.video.suffix.lower() not in VIDEO_EXTS:
            warn("video", f"B站可能不支持 {post.video.suffix or '无扩展名'} 格式")
        if post.cover is not None:
            if post.cover.suffix.lower() not in COVER_EXTS:
                err("cover", "B站封面只支持 jpg / png")
            elif post.cover.is_file() and post.cover.stat().st_size > COVER_BYTES_WARN:
                warn("cover", "封面超过 5MB，可能被拒")
        if post.schedule is not None:
            delta = post.schedule - datetime.now()
            if delta < VIDEO_SCHEDULE_MIN:
                err("schedule", "B站定时投稿需晚于当前时间 2 小时以上")
            elif delta > VIDEO_SCHEDULE_MAX:
                err("schedule", "B站定时投稿最多提前 15 天")
            elif delta < VIDEO_SCHEDULE_SAFE:
                warn("schedule", "距现在不到 2.5 小时：上传耗时可能让投稿时不足 2 小时而被拒")

    @staticmethod
    def _validate_note(post: PlatformPost, err: Callable[[str, str], None],
                       warn: Callable[[str, str], None]) -> None:
        o = post.options
        mode = str(o.get("mode", "browser")).lower()
        if mode not in NOTE_MODES:
            err("platforms.bilibili.mode", "只能是 browser（默认）或 http（实验性）")
        title = post.title.strip()
        if len(title) > NOTE_TITLE_MAX:
            err("title", f"B站动态标题最多 {NOTE_TITLE_MAX} 字（当前 {len(title)}），"
                         "可在 platforms.bilibili.title 单独设置")
        text = note_text(post)
        if len(text) > NOTE_TEXT_MAX:
            err("body", f"动态正文（含末尾的 #标签#）最多 {NOTE_TEXT_MAX} 字（当前 {len(text)}）")
        if len(post.images) > NOTE_IMAGES_MAX:
            err("images", f"B站动态最多 {NOTE_IMAGES_MAX} 张图（当前 {len(post.images)}）")
        for img in post.images:
            suffix = img.suffix.lower()
            if suffix not in NOTE_IMAGE_EXTS:
                err("images", f"{img.name}: B站动态只支持 jpg / png / gif")
                continue
            size = img.stat().st_size if img.is_file() else 0
            # 发布框的校验：floor(大小 / 1MB) >= 上限就拒绝（GIF 8MB，其他 20MB）
            if suffix == ".gif" and size >= NOTE_GIF_BYTES_MAX:
                err("images", f"{img.name}: GIF 必须小于 8MB")
            elif size >= NOTE_IMAGE_BYTES_MAX:
                err("images", f"{img.name}: 单张图片必须小于 20MB")
        if o.get("pic_mode") not in (None, "") and _as_int(o.get("pic_mode")) not in (0, 1):
            err("platforms.bilibili.pic_mode", "只能是 0（九宫格，默认）或 1（大封面）")
        for key in ("confirm_rules", "tags_in_text"):
            if key in o and not isinstance(o[key], bool):
                err(f"platforms.bilibili.{key}", "只能是 true 或 false")
        if post.cover is not None:
            warn("cover", "B站动态不使用封面，已忽略")
        if post.schedule is not None:
            delta = post.schedule - datetime.now()
            if delta < NOTE_SCHEDULE_MIN:
                err("schedule", "B站动态定时需晚于当前时间 5 分钟以上")
            elif delta > NOTE_SCHEDULE_MAX:
                err("schedule", "B站动态定时最多提前 7 天")
            elif delta < NOTE_SCHEDULE_SAFE:
                warn("schedule", "距现在不到 10 分钟，传图耗时后可能不足 5 分钟")

    # ================================================================ 环境
    @classmethod
    def doctor(cls, settings: Settings) -> list[dict[str, Any]]:
        from . import bili_biliup

        return bili_biliup.doctor_items(settings)

    @classmethod
    def setup(cls, settings: Settings) -> list[dict[str, Any]]:
        from . import bili_biliup

        return [bili_biliup.install(settings)]

    @classmethod
    def run_interactive_login(cls, settings: Settings, account: str) -> int:
        from . import bili_biliup

        return bili_biliup.run_interactive_login(settings, account)

    # ================================================================ 登录
    def _load_cred(self, *, require_token: bool) -> Any:
        from .bili_cookies import CredentialError, load_credential, strip_bom

        if strip_bom(self.account_file):  # 同一个文件要交给 biliup，它不认 BOM
            self.ctx.log(f"登录文件开头有 BOM，已去掉：{self.account_file}", level="warning")
        try:
            cred = load_credential(self.account_file)
        except CredentialError as exc:
            raise LoginRequired(f"{exc}；{_login_hint(self.ctx.account)}") from exc
        if require_token and not cred.has_token:
            raise LoginRequired("登录文件缺少 token_info（不是扫码登录生成的），biliup 无法上传视频；"
                                + _login_hint(self.ctx.account))
        return cred

    async def _nav(self, cred: Any, *, with_buvid: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
        """用登录文件里的 cookie 调 nav；with_buvid 时先补 buvid3/buvid4（只读接口）。返回 (nav, 补充的 cookie)。"""
        from . import bili_http

        extra: dict[str, str] = {}
        async with bili_http.make_client(cred.cookie_dict, referer="https://www.bilibili.com/") as client:
            if with_buvid:
                try:
                    extra = await bili_http.ensure_buvid(client)
                except PublishError as exc:  # 只是降低风控分，拿不到也继续
                    self.ctx.log(f"获取 buvid3 失败（{exc}），继续", level="warning")
            nav = await bili_http.fetch_nav(client)
        return nav, extra

    async def _token(self, cred: Any, *, timeout: float = 30) -> dict[str, Any]:
        """校验 biliup 需要的 access_token（只读，与 biliup 上传前的校验相同）。"""
        from . import bili_http

        async with bili_http.make_client(referer="https://www.bilibili.com/", timeout=timeout) as client:
            return await bili_http.oauth_token_info(client, str(cred.token_info["access_token"]),
                                                    cred.raw.get("platform"), account=self.ctx.account)

    async def login(self) -> Result:
        from . import bili_cookies, bili_http

        account = self.ctx.account
        if self.account_file.is_file():
            try:
                cred = self._load_cred(require_token=True)
                nav, _ = await self._nav(cred)
                if nav.get("isLogin") and await self._token(cred):
                    return self.result(Status.OK, message=f"已登录：{nav.get('uname')}（UID {nav.get('mid')}），"
                                                          f"无需重新扫码；要换号先删除 {self.account_file}")
            except PublishError as exc:  # 包括 LoginRequired：继续扫码
                self.ctx.log(f"现有登录不可用（{exc}），重新扫码", level="debug")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(60, self.settings.timeout_for("login") - 45)
        qr_path = self.settings.qrcode_dir / f"bilibili-{account}.png"
        async with bili_http.make_client(referer="https://www.bilibili.com/") as client:
            url, auth_code = await bili_http.tv_qr_start(client)
            self._show_qr(url, qr_path)
            refreshes = errors = 0
            scanned = False
            while True:
                if loop.time() > deadline:
                    raise PublishError(f"等待扫码超时：重新{_login_hint(account)}", code=ErrorCode.TIMEOUT)
                await asyncio.sleep(LOGIN_POLL_SECONDS)
                try:
                    code, data = await bili_http.tv_qr_poll(client, auth_code)
                except PublishError as exc:
                    errors += 1
                    if not exc.retryable or errors > 5:
                        raise
                    continue
                errors = 0
                if code == 0 and data:
                    break
                if code == 86090 and not scanned:
                    scanned = True
                    self.ctx.log("已扫码，请在手机上点「确认登录」")
                elif code == 86038:
                    refreshes += 1
                    if refreshes > QR_MAX_REFRESH:
                        raise PublishError(f"二维码多次过期：重新{_login_hint(account)}", code=ErrorCode.TIMEOUT)
                    url, auth_code = await bili_http.tv_qr_start(client)
                    scanned = False
                    self._show_qr(url, qr_path, note="二维码已过期，已刷新")
                elif code not in (86039, 86090):
                    raise PublishError(f"扫码登录失败（B站返回 {code}）")

        try:
            info = bili_cookies.login_info_from_tv_poll(data)
        except bili_cookies.CredentialError as exc:
            raise PublishError(str(exc)) from exc
        bili_cookies.write_login_info(self.account_file, info)
        qr_path.unlink(missing_ok=True)
        who = f"UID {info['token_info']['mid']}"
        try:
            nav, _ = await self._nav(self._load_cred(require_token=True))
            if nav.get("isLogin"):
                who = f"{nav.get('uname')}（UID {nav.get('mid')}）"
        except PublishError as exc:
            self.ctx.log(f"登录后校验失败（{exc}），登录文件已保存", level="warning")
        return self.result(Status.OK, message=f"登录成功：{who}；登录文件（biliup 兼容）: {self.account_file}")

    def _show_qr(self, url: str, path: Path, note: str = "") -> None:
        import segno

        path.parent.mkdir(parents=True, exist_ok=True)
        segno.make(url, error="m").save(str(path), scale=8, border=4)
        hint = "用 B站手机 App 扫码（会显示「TV 端登录」确认），二维码 3 分钟有效"
        self.ctx.qrcode(path, note=f"{note}；{hint}" if note else hint)

    async def check(self) -> Result:
        cred = self._load_cred(require_token=True)
        nav, _ = await self._nav(cred)
        if not nav.get("isLogin"):
            raise LoginRequired(f"B站登录已失效：{_login_hint(self.ctx.account)}")
        token = await self._token(cred)
        message = f"已登录：{nav.get('uname')}（UID {nav.get('mid')}），biliup 令牌有效"
        if token.get("refresh"):
            message += "（快到期了，下次上传视频时 biliup 会自动续期）"
        days = cred.days_left()
        if days is not None:
            message += f"，Cookie 约 {max(0, int(days))} 天后过期"
            if days < 15:
                message += "（上传视频时 biliup 会自动续期；也可以重新登录）"
        return self.result(Status.OK, message=message)

    # ================================================================ 发布
    _t0: float | None = None  # publish() 开始的时刻（worker 的总超时也从这时算起）

    async def publish(self, post: PlatformPost) -> Result:
        self._t0 = time.monotonic()
        errors = [i for i in self.validate(post) if i.level == "error"]
        if errors:
            raise ValidationFailed("；".join(f"{i.field}: {i.message}" for i in errors))
        if post.kind is Kind.VIDEO:
            return await self._publish_video(post)
        if post.kind is Kind.NOTE:
            return await self._publish_note(post)
        raise Unsupported(f"B站暂不支持 {post.kind.value}")

    def _video_timeout(self) -> float:
        """biliup 的期限：比 worker 的总超时（从 publish() 开始算）早 VIDEO_TIMEOUT_MARGIN 秒，
        自己杀掉 biliup 并给出明确结果；扣掉发布前准备（令牌预检等）已经用掉的时间。"""
        elapsed = time.monotonic() - self._t0 if self._t0 is not None else 0.0
        total = float(self.ctx.timeout) if self.ctx.timeout else self.settings.timeout_for("publish_video") - 30
        return total - elapsed - VIDEO_TIMEOUT_MARGIN

    async def _publish_video(self, post: PlatformPost) -> Result:
        from . import bili_biliup

        cred = self._load_cred(require_token=True)
        from . import bili_locate

        res = await asyncio.to_thread(bili_locate.resolve, self.settings)
        if res.path is None:
            raise PublishError(f"biliup 不可用（{res.detail}）：{res.problem}；没有启动上传（尚未投稿）",
                               code=ErrorCode.INTERNAL)
        exe = res.path
        probe = await self._preflight_biliup(exe)  # 起不来（缺运行库 / 超时）在这里按原因报
        if res.problem:
            raise PublishError(f"biliup 不兼容（{res.detail}）：{res.problem}；没有启动上传（尚未投稿）。"
                               "换一个：mpub biliup find / mpub biliup use --download", code=ErrorCode.INTERNAL)
        # 先做一次与 biliup 相同的只读令牌校验：登录失效时在"开始上传"之前就报 login_required
        try:
            await asyncio.wait_for(self._token(cred, timeout=TOKEN_PRECHECK_TIMEOUT), TOKEN_PRECHECK_TIMEOUT * 2)
        except LoginRequired:
            raise
        except (PublishError, asyncio.TimeoutError) as exc:  # 网络 / 风控：交给 biliup 自己校验
            self.ctx.log(f"预先校验登录令牌失败（{str(exc) or '超时'}），交给 biliup 处理", level="warning")
        rust_log = await asyncio.to_thread(bili_locate.supports_rust_log, exe)
        args = bili_biliup.build_upload_args(exe, self.account_file, post, rust_log=rust_log)
        missing = await asyncio.to_thread(bili_locate.unsupported_flags, exe, args)
        if missing:
            raise PublishError(f"当前 biliup（{res.detail}）不支持参数 {', '.join(missing)}；没有启动上传（尚未投稿）。"
                               "去掉清单里对应的选项，或换一个兼容的 biliup：mpub biliup find / "
                               "mpub biliup use --download", code=ErrorCode.INTERNAL)
        timeout = self._video_timeout()
        if timeout < VIDEO_MIN_BUDGET:
            raise PublishError(f"剩余时间只有 {max(0, int(timeout))} 秒，不够上传视频，没有启动 biliup（尚未投稿）："
                               "调大 --timeout 或 config.toml [timeouts] publish_video 后重试",
                               code=ErrorCode.TIMEOUT, retryable=True)
        assert post.video is not None
        size_mb = post.video.stat().st_size / 1024 / 1024
        self.ctx.log(f"biliup 上传 {post.video.name}（{size_mb:.1f} MB），完成后自动投稿")
        # biliup 是「上传 + 投稿」一条命令，投稿请求在视频全部传完之后才发。输出格式核对过的版本（v1.3.1）
        # 等看到「上传完成」再标记 submit_started：上传途中被中断 / 超时 / 出错都确定没投稿，不会被查重拦住；
        # 其他版本输出可能不同，保守起见启动前就标记。之后的任何失败都不自动重试
        watch = bili_biliup.output_format_verified(probe.version)
        if not watch:
            self.ctx.mark_submit_started()
        progress = _ProgressLog(self.ctx, on_uploaded=self.ctx.mark_submit_started if watch else None)
        rc, lines = await bili_biliup.run_streaming(args, cwd=self.ctx.run_dir, timeout=timeout,
                                                    on_line=progress, heartbeat=60)
        # 文件名 / 路径 / 标题会被 biliup 回显（如 pre_upload、Upload completed、file <路径>），解析时抹掉
        echo = bili_biliup.echo_strings(post.video, post.cover, self.account_file, post.title.strip())
        outcome = bili_biliup.parse_upload_output(lines, rc, echo=echo)
        if outcome.uploaded or outcome.submitted or outcome.response_code is not None or rc == 0:
            self.ctx.mark_submit_started()  # 兜底：只有「确定没传完就失败了」才保持未投稿
        return self._video_result(post, outcome, timeout)

    async def _preflight_biliup(self, exe: Path) -> Any:
        """标记投稿之前先试运行 `biliup --version`（纯本地）：缺 VC++ 运行库这类起不来的情况在这里报，
        而不是标记 submit_started 之后得到一个看不懂的退出码。返回 Probe（含版本号）。"""
        from . import bili_biliup

        probe = await asyncio.to_thread(bili_biliup.probe_cached, exe)
        if probe.ok:
            return probe
        message = f"{probe.detail}；没有启动上传（尚未投稿）"
        if probe.timed_out:
            raise PublishError(message, code=ErrorCode.TIMEOUT, retryable=True)
        raise PublishError(f"{message}，处理后重新发布", code=ErrorCode.INTERNAL)

    def _video_result(self, post: PlatformPost, o: Any, timeout: float) -> Result:
        from .bili_biliup import startup_failure_hint, video_url

        url = video_url(o.bvid, o.aid)
        if o.submitted:
            if o.returncode != 0:
                self.ctx.log(f"biliup 退出码 {o.returncode}，但输出显示已投稿成功", level="warning")
            if post.schedule is not None:
                status = Status.SCHEDULED
                message = f"已定时投稿，{post.schedule:%Y-%m-%d %H:%M} 发布（需先通过审核）"
            else:
                status, message = Status.SUBMITTED, "投稿成功，等待B站审核"
            if not url:
                message += "；没解析到 BV 号，请到创作中心确认"
            return self.result(status, kind=post.kind, url=url, item_id=o.bvid or o.aid, message=message)
        # 投稿响应的 code 优先于退出码和任何输出标记
        unknown = "视频已上传完，投稿请求结果未知：先到创作中心（member.bilibili.com）确认，不要直接重发"
        if o.response_code in (-500, -502, -503, -504):  # 服务端错误 / 超时：稿件可能已经建好
            raise PublishError(f"投稿接口返回服务端错误（{o.response_code}: {o.response_message}）；{unknown}",
                               code=ErrorCode.UNKNOWN_OUTCOME)
        if o.response_code not in (None, 0):
            raise PublishError(f"B站拒绝投稿（{o.response_code}: {o.response_message}）")
        hint = startup_failure_hint(o.returncode)
        if hint and not o.uploaded:  # 进程根本没起来（缺 DLL 等），什么都没发
            raise PublishError(f"{hint}；尚未投稿，处理后重新发布", code=ErrorCode.INTERNAL)
        if o.returncode == 0:
            return self.result(Status.SUBMITTED, kind=post.kind,
                               message="biliup 正常退出但没解析到投稿结果，请到创作中心确认")
        if o.returncode is None:
            if o.uploaded:
                raise PublishError(f"超时；{unknown}", code=ErrorCode.UNKNOWN_OUTCOME)
            raise PublishError(f"上传超过 {int(timeout)} 秒，已终止，尚未投稿；可在 config.toml [timeouts] "
                               "调大 publish_video 或加大 --timeout 后重新发布", code=ErrorCode.TIMEOUT)
        if o.login_error and not o.uploaded:
            raise LoginRequired(f"B站登录已失效（biliup: {o.error_text[-200:]}）；{_login_hint(self.ctx.account)}")
        if o.uploaded:
            raise PublishError(f"biliup 退出码 {o.returncode}；{unknown}", code=ErrorCode.UNKNOWN_OUTCOME)
        raise PublishError(f"biliup 上传失败（退出码 {o.returncode}）：{o.error_text}；"
                           "尚未投稿，确认原因后可重新发布")

    async def _web_session(self) -> tuple[Any, dict[str, Any], dict[str, str]]:
        """准备网页登录态：nav 不通过且有 token 时，用 biliup renew 刷新一次 cookie 再试。"""
        from . import bili_biliup

        cred = self._load_cred(require_token=False)
        nav, extra = await self._nav(cred, with_buvid=True)
        if not nav.get("isLogin") and cred.has_token:
            exe = bili_biliup.find_biliup(self.settings)
            if exe is not None:
                self.ctx.log("网页登录态已过期，尝试用 biliup renew 刷新")
                ok, tail = await bili_biliup.renew(exe, self.account_file, cwd=self.ctx.run_dir)
                if ok:
                    cred = self._load_cred(require_token=False)
                    nav, extra = await self._nav(cred, with_buvid=True)
                else:
                    self.ctx.log(f"biliup renew 失败: {tail}", level="warning")
        if not nav.get("isLogin"):
            raise LoginRequired(f"B站登录已失效：{_login_hint(self.ctx.account)}")
        return cred, nav, extra

    def spec_marker_path(self) -> Path:
        """记录"用户亲手确认过《动态发布规范》"的文件（不是 .json，不会被当成账号）。"""
        return self.account_file.with_name(f"{self.ctx.account}.dy_spec_agreed")

    async def _publish_note(self, post: PlatformPost) -> Result:
        from . import bili_cookies, bili_http

        mode = str(post.options.get("mode", "browser")).lower()
        title = post.title.strip()
        text = note_text(post)
        timer = int(post.schedule.timestamp()) if post.schedule else None
        pic_mode = _as_int(post.options.get("pic_mode"))
        cred, nav, extra = await self._web_session()
        self.ctx.log(f"以 {nav.get('uname')} 的身份发布图文动态（{mode} 模式）")
        warnings: list[str] = []

        if mode == "http":
            async with bili_http.make_client({**cred.cookie_dict, **extra}) as client:
                data = await bili_http.publish_note_http(
                    client, csrf=cred.csrf, account=self.ctx.account, mid=nav.get("mid") or cred.mid or 0,
                    keys=bili_http.wbi_keys(nav), title=title, text=text, images=post.images,
                    aigc=post.ai_generated, pic_mode=pic_mode or 0, timer_pub_time=timer,
                    mark_submit=self.ctx.mark_submit_started, log=self.ctx.log)
        else:
            from .bili_note import BrowserNotePublisher

            marker = self.spec_marker_path()
            if "confirm_rules" in post.options:  # 清单里明确写了就以它为准
                consent = post.options["confirm_rules"] is True
            else:
                consent = self.settings.platform_config(NAME).get(CONFIRM_RULES_CONFIG) is True or marker.is_file()
            wait = _as_int(self.settings.platform_config(NAME).get("publish_wait"))
            publisher = BrowserNotePublisher(self.ctx, account=self.ctx.account, spec_consent=consent,
                                             spec_marker=marker, publish_wait=max(10, wait or 60))
            data = await publisher.publish(
                storage_state=bili_cookies.to_storage_state(cred, extra), title=title, text=text,
                images=post.images, timer_pub_time=timer, aigc=post.ai_generated, pic_mode=pic_mode)
            # 浏览器模式以发布请求里实际提交的选项为准（定时没生效就是已立即发布）
            timer = getattr(publisher, "scheduled_at", timer)
            warnings = list(getattr(publisher, "warnings", []))

        dyn_id = str(data["dyn_id_str"])
        url = bili_http.opus_url(dyn_id)
        notes = "".join(f"；注意：{w}" for w in warnings)
        if timer:
            return self.result(Status.SCHEDULED, kind=post.kind, url=url, item_id=dyn_id,
                               message=f"已定时，{datetime.fromtimestamp(timer):%Y-%m-%d %H:%M} 发布"
                                       f"（之前链接打不开）{notes}")
        return self.result(Status.PUBLISHED, kind=post.kind, url=url, item_id=dyn_id, message=f"动态已发布{notes}")
