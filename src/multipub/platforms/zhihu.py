"""知乎适配器：文章 + 想法，接口优先。

- 登录：patchright 打开 https://www.zhihu.com/signin，等到 z_c0 cookie 出现后保存 storage_state
  到 <home>/accounts/zhihu/<账号>.json；同时尽力把登录二维码存成 PNG 上报（无头模式也能扫）。
- 发布：用登录文件里的 cookie 直接调知乎 Web 接口（zhihu_api），不开浏览器。
  - kind: article → 专栏文章：Markdown → 知乎 HTML（zhihu_html），本地/远程图片先传到知乎图床，
    建草稿 → 写正文 → 封面 / 话题 → content/publish；draft: true 时只存草稿。
  - kind: note → 想法（默认，最多 9 张图）；platforms.zhihu.as: article 改发文章（图片放在文末）。
- 视频暂不支持：kinds 不含 video，CLI 自动跳过。
- 遇到风控（40352）时记下验证页地址，下次 mpub login 会带着现有登录态打开它让人完成验证。

本模块顶层只导入标准库和核心模块；httpx / mistune / Pillow / patchright 在方法里按需导入。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ..config import Settings
from ..models import (ErrorCode, Issue, Kind, LoginRequired, NeedsAction, PlatformPost, PublishError, Result,
                      Status, Unsupported, ValidationFailed)
from .base import JobContext, PlatformAdapter

NAME = "zhihu"

ARTICLE_TITLE_MAX = 100
ARTICLE_TOPICS_MAX = 3  # 知乎文章最多 3 个话题（编辑器限制，未在接口上验证）
PIN_IMAGES_MAX = 9
PIN_TEXT_WARN = 1000  # 想法字数上限未知，超过这个数给警告
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

MODES = ("pin", "article")
# 创作声明：manifest 写法 → 知乎 disclaimer_type
STATEMENTS = {
    "none": "none",
    "ai": "ai_creation",  # 包含 AI 辅助创作
    "spoiler": "spoiler",  # 包含剧透
    "medical": "medical_advice",  # 包含医疗建议
    "fiction": "fictional_creation",  # 虚构创作
    "finance": "contain_finance",  # 包含理财内容
}
ARTICLE_OPTIONS = frozenset({"as", "draft", "statement", "column", "toc"})
PIN_OPTIONS = frozenset({"as", "tags_in_text"})

LOGIN_POLL_SECONDS = 1.0
QR_FALLBACK_AFTER = 8.0  # 这么久还没从接口拿到二维码，就截图
QR_API_PATH = "/api/v3/account/api/login/qrcode"
QR_SELECTORS = (".Qrcode-img", ".Qrcode-content img", "img[alt*='二维码']", ".Qrcode canvas")

_TRUE = {"true", "yes", "on", "1", "是"}
_FALSE = {"false", "no", "off", "0", "否", ""}


# ------------------------------------------------------------------ 纯函数（主进程 validate 也用）

def _as_bool(value: Any) -> bool | None:
    """YAML 的布尔或常见字符串写法；无法识别返回 None。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def publish_mode(post: PlatformPost) -> str:
    """pin（想法）或 article（文章）。"""
    default = "article" if post.kind is Kind.ARTICLE else "pin"
    return str(post.options.get("as") or default).strip().lower()


def statement_type(post: PlatformPost) -> str:
    """platforms.zhihu.statement 优先；没写时 ai_generated → 包含 AI 辅助创作。"""
    raw = post.options.get("statement")
    if raw not in (None, ""):
        return STATEMENTS.get(str(raw).strip().lower(), "none")
    return STATEMENTS["ai"] if post.ai_generated else "none"


def column_id(value: Any) -> str | None:
    """专栏 id（c_123…）；也接受专栏链接 https://www.zhihu.com/column/c_123…"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if "://" in text:
        text = urllib.parse.urlsplit(text).path.rstrip("/").rsplit("/", 1)[-1]
    return text or None


def pin_text(post: PlatformPost) -> str:
    """想法正文；tags_in_text: true 时把 #话题# 附在末尾（是否会变成真正的话题未验证）。"""
    text = post.body.strip()
    if _as_bool(post.options.get("tags_in_text")) is not True or not post.tags:
        return text
    extra = [f"#{t}#" for t in post.tags if f"#{t}#" not in text]
    return (text + "\n" if text and extra else text) + " ".join(extra)


def asset_dirs(post: PlatformPost) -> list[Path]:
    """正文里相对路径图片的查找目录：body_file 所在目录优先，其次 manifest 所在目录。"""
    if not post.source:
        return [Path.cwd()]
    manifest_path = Path(post.source)
    dirs: list[Path] = []
    from ..manifest import ManifestError, load_manifest

    try:
        manifest = load_manifest(manifest_path)
    except (ManifestError, OSError, ValueError):
        manifest = None
    if manifest is not None:
        over = manifest.platforms.get(NAME, {})
        body_file = over.get("body_file") or (None if "body" in over else manifest.data.get("body_file"))
        if body_file:
            p = Path(str(body_file)).expanduser()
            dirs.append((p if p.is_absolute() else manifest.base_dir / p).parent)
    dirs.append(manifest_path.parent)
    return list(dict.fromkeys(d.resolve() for d in dirs))


def resolve_local_image(ref: str, dirs: list[Path]) -> Path | None:
    """Markdown 里的本地图片引用 → 文件路径（支持百分号编码、file:// 与绝对路径）。"""
    candidates = [ref]
    if ref.lower().startswith("file://"):
        candidates = [urllib.parse.unquote(urllib.parse.urlsplit(ref).path)]
        # Windows: file:///C:/x.png → /C:/x.png
        if len(candidates[0]) > 2 and candidates[0][0] == "/" and candidates[0][2] == ":":
            candidates[0] = candidates[0][1:]
    elif "%" in ref:
        candidates.append(urllib.parse.unquote(ref))
    for cand in candidates:
        p = Path(cand).expanduser()
        if p.is_absolute():
            if p.is_file():
                return p.resolve()
            continue
        for d in dirs:
            q = d / p
            if q.is_file():
                return q.resolve()
    return None


def _num(cfg: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _join(message: str, warnings: list[str]) -> str:
    return message + ("；注意：" + "；".join(warnings) if warnings else "")


def _with_draft(exc: PublishError, edit_url: str) -> PublishError:
    """草稿已经建好后出错：把编辑链接带进错误信息，方便手动处理。"""
    note = "文章编辑页" if exc.code is ErrorCode.UNKNOWN_OUTCOME else "草稿已保存"
    new = type(exc)(f"{exc}（{note}：{edit_url}）", code=exc.code, retryable=exc.retryable,
                    evidence=exc.evidence)
    if hasattr(exc, "url"):
        new.url = exc.url  # type: ignore[attr-defined]
    return new


class _QrCapture:
    """登录二维码：优先拦截页面自己请求的二维码接口（拿到 link 用 segno 重画），拿不到就截图。"""

    def __init__(self, ctx: JobContext, path: Path):
        self.ctx = ctx
        self.path = path
        self.done = False
        self.tried_fallback = False

    def _note(self) -> str:
        note = "用知乎 App 扫一扫登录"
        if not self.ctx.headless:
            note += "；也可以直接在弹出的浏览器窗口里用手机号登录"
        return note

    async def on_response(self, response: Any) -> None:
        if urllib.parse.urlsplit(response.url).path.rstrip("/") != QR_API_PATH:
            return
        try:
            data = await response.json()
            link = data.get("link") if isinstance(data, dict) else None
            if not link:
                return
            import segno

            self.path.parent.mkdir(parents=True, exist_ok=True)
            segno.make(str(link), error="m").save(str(self.path), scale=8, border=4)
        except Exception as exc:  # noqa: BLE001 - 页面事件回调里不能抛
            self.ctx.log(f"生成登录二维码失败: {exc}", level="debug")
            return
        refreshed = self.done
        self.done = True
        self.ctx.qrcode(self.path, note=("二维码已刷新；" if refreshed else "") + self._note())

    async def fallback(self, page: Any) -> None:
        if self.tried_fallback:
            return
        self.tried_fallback = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for selector in QR_SELECTORS:
                loc = page.locator(selector).first
                if await loc.count() and await loc.is_visible():
                    await loc.screenshot(path=str(self.path))
                    break
            else:
                await page.screenshot(path=str(self.path))
        except Exception as exc:  # noqa: BLE001 - 尽力而为
            self.ctx.log(f"二维码截图失败: {exc}", level="debug")
            return
        self.done = True
        self.ctx.qrcode(self.path, note="页面截图（含二维码）；" + self._note())

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


# ------------------------------------------------------------------ 适配器

class ZhihuAdapter(PlatformAdapter):
    name = NAME
    display_name = "知乎"
    kinds = frozenset({Kind.NOTE, Kind.ARTICLE})
    _cookies: list[dict[str, Any]] = []  # _api() 读到的 cookie（check 用来算过期时间）

    # ================================================================ 校验（纯本地）
    @classmethod
    def validate(cls, post: PlatformPost) -> list[Issue]:
        issues: list[Issue] = []

        def err(f: str, m: str) -> None:
            issues.append(Issue("error", f, m, NAME))

        def warn(f: str, m: str) -> None:
            issues.append(Issue("warning", f, m, NAME))

        if post.kind is Kind.VIDEO:
            err("kind", "知乎暂不支持视频")
            return issues
        o = post.options
        mode = publish_mode(post)
        if mode not in MODES:
            err("platforms.zhihu.as", "只能是 pin（想法）或 article（文章）")
            return issues
        if post.kind is Kind.ARTICLE and mode != "article":
            err("platforms.zhihu.as", "长文只能发成知乎文章（as: article）")
            return issues

        known = ARTICLE_OPTIONS if mode == "article" else PIN_OPTIONS
        for key in o:
            if key in known or (key == "draft" and mode == "pin"):
                continue
            if key in ARTICLE_OPTIONS | PIN_OPTIONS:
                warn(f"platforms.zhihu.{key}", f"{'文章' if mode == 'article' else '想法'}不使用该参数，已忽略")
            else:
                warn(f"platforms.zhihu.{key}", f"未知参数，会被忽略（支持: {', '.join(sorted(known))}）")
        for key in ("draft", "toc", "tags_in_text"):
            if key in o and _as_bool(o[key]) is None:
                err(f"platforms.zhihu.{key}", f"应为 true / false，收到 {o[key]!r}")
        if post.schedule is not None:
            err("schedule", "知乎暂不支持定时发布：去掉 schedule，或这次不发知乎（--to 指定其他平台）")
        for img in post.images:
            cls._check_image_ext(img, "images", err, warn)

        if mode == "article":
            cls._validate_article(post, err, warn)
        else:
            cls._validate_pin(post, err, warn)
        return issues

    @staticmethod
    def _check_image_ext(path: Path, field: str, err: Callable[[str, str], None],
                         warn: Callable[[str, str], None]) -> None:
        ext = path.suffix.lower()
        if ext not in IMAGE_EXTS:
            err(field, f"{path.name}: 知乎只支持 jpg / png / gif / webp / bmp")
        elif ext == ".gif":
            warn(field, f"{path.name}: 暂不支持动图，会转成静态 JPEG（只保留第一帧）")

    @classmethod
    def _validate_article(cls, post: PlatformPost, err: Callable[[str, str], None],
                          warn: Callable[[str, str], None]) -> None:
        from .zhihu_html import collect_image_urls, is_remote, is_zhimg, raw_html_images

        o = post.options
        title = post.title.strip()
        if not title:
            err("title", "知乎文章必须有标题")
        elif len(title) > ARTICLE_TITLE_MAX:
            err("title", f"知乎文章标题最多 {ARTICLE_TITLE_MAX} 字（当前 {len(title)}），可在 platforms.zhihu.title 单独设置")
        if not post.body.strip() and not post.images:
            err("body", "文章正文为空")

        dirs = asset_dirs(post)
        for ref in collect_image_urls(post.body):
            if is_remote(ref):
                continue
            if ref.lower().startswith("data:"):
                err("body", "不支持 data: 内嵌图片，请改成图片文件")
                continue
            path = resolve_local_image(ref, dirs)
            if path is None:
                err("body", f"找不到正文里引用的图片: {ref}（相对路径按 Markdown 文件所在目录解析）")
            else:
                cls._check_image_ext(path, "body", err, warn)
        raw_imgs = [u for u in raw_html_images(post.body) if not is_zhimg(u)]
        if raw_imgs:
            warn("body", f"HTML <img> 标签里的图片不会上传到知乎图床（{raw_imgs[0]} 等 {len(raw_imgs)} 张），"
                         "发布后可能显示不出来：改用 Markdown 写法 ![说明](路径)")
        if post.cover is not None:
            cls._check_image_ext(post.cover, "cover", err, warn)

        raw_statement = o.get("statement")
        if raw_statement not in (None, "") and str(raw_statement).strip().lower() not in STATEMENTS:
            err("platforms.zhihu.statement", f"创作声明只能是: {' / '.join(STATEMENTS)}")
        elif post.ai_generated and raw_statement not in (None, "") and str(raw_statement).strip().lower() != "ai":
            warn("platforms.zhihu.statement", f"ai_generated 为 true，但 statement 设为 {raw_statement}：以 statement 为准")
        if "column" in o and not column_id(o.get("column")):
            err("platforms.zhihu.column", "专栏 id 不能为空（形如 c_1234567890）")
        if len(post.tags) > ARTICLE_TOPICS_MAX:
            warn("tags", f"知乎文章最多 {ARTICLE_TOPICS_MAX} 个话题，只取前 {ARTICLE_TOPICS_MAX} 个")
        elif not post.tags:
            warn("tags", "没有设置话题（tags）：文章没有话题可能无法发布或推荐受限，建议至少 1 个")
        if _as_bool(o.get("draft")) and (statement_type(post) != "none" or column_id(o.get("column"))):
            warn("platforms.zhihu.draft", "只存草稿时创作声明 / 专栏不会生效，需在编辑页点发布时自己选择")

    @staticmethod
    def _validate_pin(post: PlatformPost, err: Callable[[str, str], None],
                      warn: Callable[[str, str], None]) -> None:
        o = post.options
        if _as_bool(o.get("draft")):
            err("platforms.zhihu.draft", "想法没有草稿箱：draft: true 只对文章有效（可改 as: article）")
        if len(post.images) > PIN_IMAGES_MAX:
            err("images", f"知乎想法最多 {PIN_IMAGES_MAX} 张图（当前 {len(post.images)}）")
        text = pin_text(post)
        if len(text) > PIN_TEXT_WARN:
            warn("body", f"想法正文 {len(text)} 字，超过 {PIN_TEXT_WARN} 字可能被截断或拒绝（上限未验证），长内容建议 as: article")
        if post.cover is not None:
            warn("cover", "知乎想法不使用封面，已忽略")
        if post.tags and _as_bool(o.get("tags_in_text")) is not True:
            warn("tags", "想法不会关联话题，tags 已忽略；设 tags_in_text: true 可把 #话题# 附在正文末尾")
        if post.ai_generated:
            warn("ai_generated", "想法没有创作声明选项，ai_generated 不会生效；需要「包含 AI 辅助创作」声明可改 as: article")

    # ================================================================ 公共
    def _verify_file(self) -> Path:
        """上次遇到风控时记下的验证页地址（下次 login 打开）。不用 .json 后缀，免得被当成账号。"""
        return self.settings.accounts_dir / NAME / f"{self.ctx.account}.verify.txt"

    def _ua_file(self) -> Path:
        """登录浏览器的 User-Agent，发布时沿用（同样不用 .json 后缀）。"""
        return self.settings.accounts_dir / NAME / f"{self.ctx.account}.ua.txt"

    def _saved_user_agent(self) -> str:
        from . import zhihu_api

        try:
            return zhihu_api.normalize_user_agent(self._ua_file().read_text(encoding="utf-8-sig"))
        except OSError:
            return ""

    def _remember_verify(self, url: str) -> None:
        if not url:
            return
        path = self._verify_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url, encoding="utf-8")

    def _pending_verify(self) -> str:
        path = self._verify_file()
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8-sig").strip()

    @asynccontextmanager
    async def _api(self) -> AsyncIterator[Any]:
        from . import zhihu_api

        cookies = zhihu_api.load_cookies(self.account_file, account=self.ctx.account)
        self._cookies = cookies
        cfg = self.settings.platform_config(NAME)
        async with zhihu_api.make_client(cookies, user_agent=self._saved_user_agent() or None) as client:
            api = zhihu_api.ZhihuApi(
                client, account=self.ctx.account, log=self.ctx.log,
                write_interval=max(zhihu_api.MIN_WRITE_INTERVAL,
                                   _num(cfg, "write_interval", zhihu_api.DEFAULT_WRITE_INTERVAL)),
                image_timeout=_num(cfg, "image_timeout", zhihu_api.DEFAULT_IMAGE_TIMEOUT),
            )
            if not api.xsrf:
                self.ctx.log("登录文件里没有 _xsrf cookie，写接口可能被拒；可重新登录", level="warning")
            yield api

    # ================================================================ 登录
    async def login(self) -> Result:
        from ..browser import load_storage_state
        from . import zhihu_api

        state = load_storage_state(self.account_file)
        pending = self._pending_verify() if state is not None else ""  # 没有登录态时验证无意义
        if state is not None and not pending:
            try:
                async with self._api() as api:
                    me = await api.me()
                return self.result(Status.OK, message=f"已登录：{me.get('name')}，无需重新登录；"
                                                      f"要换号先删除 {self.account_file}")
            except zhihu_api.RiskControl as exc:
                pending = exc.url
                self.ctx.log("登录态有效但被知乎风控，打开验证页", level="warning")
            except PublishError as exc:  # 包括 LoginRequired：重新登录
                self.ctx.log(f"现有登录不可用（{exc}），重新登录", level="debug")

        # 只有需要过安全验证时才带上旧登录态；正常登录用干净的浏览器上下文
        new_state = await self._browser_login(state if pending else None, pending)
        self.account_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.account_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.account_file)
        self._verify_file().unlink(missing_ok=True)

        who = ""
        try:
            async with self._api() as api:
                who = str((await api.me()).get("name") or "")
        except zhihu_api.RiskControl as exc:
            self._remember_verify(exc.url)
            raise
        except LoginRequired as exc:
            raise LoginRequired(f"浏览器里拿到了 z_c0，但接口仍提示未登录（{exc}）") from exc
        except PublishError as exc:
            self.ctx.log(f"登录后校验失败（{exc}），登录文件已保存", level="warning")
        return self.result(Status.OK, message=f"登录成功{('：' + who) if who else ''}；登录文件: {self.account_file}")

    async def _browser_login(self, state: dict[str, Any] | None, verify_url: str) -> dict[str, Any]:
        from ..browser import open_context, snapshot
        from . import zhihu_api

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(60, self.settings.timeout_for("login") - 45)
        qr = _QrCapture(self.ctx, self.settings.qrcode_dir / f"zhihu-{self.ctx.account}.png")
        if verify_url:
            self.ctx.log(f"请在打开的浏览器窗口里完成知乎安全验证：{verify_url}")
            if self.ctx.headless:
                self.ctx.log("安全验证需要人操作，无头模式下看不到页面：请去掉 --headless 重新运行", level="warning")
        initial = next((c.get("value") for c in (state or {}).get("cookies", []) if c.get("name") == "z_c0"), None)

        async with open_context(self.settings, headless=self.ctx.headless, storage_state=state) as context:
            page = await context.new_page()
            if not verify_url:
                page.on("response", qr.on_response)
            try:
                await page.goto(verify_url or zhihu_api.SIGNIN_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:  # noqa: BLE001 - patchright 的 TimeoutError / Error
                await snapshot(page, self.ctx, "login-open")
                raise PublishError(f"打开知乎页面失败（{type(exc).__name__}: {exc}）", retryable=True) from exc
            opened = loop.time()
            try:
                while not await self._browser_logged_in(context, page, initial):
                    if loop.time() > deadline:
                        await snapshot(page, self.ctx, "login-timeout")
                        raise PublishError(f"等待登录超时：重新运行 mpub login zhihu --account {self.ctx.account}",
                                           code=ErrorCode.TIMEOUT)
                    if not verify_url and not qr.done and loop.time() - opened > QR_FALLBACK_AFTER:
                        await qr.fallback(page)
                    await asyncio.sleep(LOGIN_POLL_SECONDS)
            finally:
                qr.cleanup()
            self.ctx.log("已登录，保存登录态")
            await self._remember_user_agent(page)
            try:  # 再打开一次首页，补齐 _xsrf / d_c0 等 cookie
                await page.goto(zhihu_api.WWW_HOME, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                self.ctx.log(f"打开知乎首页失败（{exc}），直接保存", level="debug")
            return await context.storage_state()

    async def _remember_user_agent(self, page: Any) -> None:
        from . import zhihu_api

        try:
            ua = zhihu_api.normalize_user_agent(await page.evaluate("() => navigator.userAgent"))
        except Exception as exc:  # noqa: BLE001 - 尽力而为，拿不到就用默认 UA
            self.ctx.log(f"读取浏览器 UA 失败（{exc}），发布时用默认 UA", level="debug")
            return
        if ua:
            path = self._ua_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ua, encoding="utf-8")

    async def _browser_logged_in(self, context: Any, page: Any, initial_z_c0: str | None) -> bool:
        if page.is_closed():
            raise PublishError("浏览器窗口已关闭，登录没有完成")
        try:
            cookies = await context.cookies("https://www.zhihu.com")
            url = page.url
        except Exception as exc:  # noqa: BLE001 - 用户关掉了浏览器
            raise PublishError(f"浏览器已关闭，登录没有完成（{type(exc).__name__}）") from exc
        z_c0 = next((c.get("value") for c in cookies if c.get("name") == "z_c0" and c.get("value")), None)
        if not z_c0:
            return False
        if initial_z_c0 is None or z_c0 != initial_z_c0:
            return True  # 新拿到的登录凭证
        # 带着旧登录态过验证：离开验证页 / 登录页才算完成
        return not any(p in url for p in ("/signin", "/account/unhuman", "/account/risk_control"))

    # ================================================================ 检查
    async def check(self) -> Result:
        from . import zhihu_api

        try:
            async with self._api() as api:
                me = await api.me()
        except zhihu_api.RiskControl as exc:
            self._remember_verify(exc.url)
            raise
        message = f"已登录：{me.get('name')}"
        if me.get("is_bind_phone") is False:
            message += "（账号没有绑定手机号，知乎可能不允许发布）"
        days = zhihu_api.cookie_days_left(self._cookies)
        if days is not None:
            message += f"，登录凭证约 {max(0, int(days))} 天后过期"
        return self.result(Status.OK, message=message)

    # ================================================================ 发布
    async def publish(self, post: PlatformPost) -> Result:
        from . import zhihu_api

        errors = [i for i in self.validate(post) if i.level == "error"]
        if errors:
            raise ValidationFailed("；".join(f"{i.field}: {i.message}" for i in errors))
        if post.kind not in self.kinds:
            raise Unsupported(f"知乎暂不支持 {post.kind.value}")
        try:
            if publish_mode(post) == "article":
                return await self._publish_article(post)
            return await self._publish_pin(post)
        except zhihu_api.RiskControl as exc:
            self._remember_verify(exc.url)
            raise

    async def _image(self, api: Any, ref: str | Path, *, source: str, referer: str,
                     dirs: list[Path]) -> Any:
        """读取 / 下载一张图片，必要时转成 JPEG，传到知乎图床。"""
        from . import zhihu_api, zhihu_html

        if isinstance(ref, str) and zhihu_html.is_remote(ref):
            name = urllib.parse.urlsplit(ref).path.rsplit("/", 1)[-1][:80] or "remote-image"
            raw = await zhihu_api.download_image(ref)
        else:
            path = ref if isinstance(ref, Path) else resolve_local_image(ref, dirs)
            if path is None:
                raise ValidationFailed(f"找不到正文里引用的图片: {ref}")
            name = path.name
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ValidationFailed(f"读取图片失败: {path}（{exc}）") from exc
        prepared = zhihu_api.prepare_image(raw, name)
        if prepared.converted_from:
            extra = "（动图只保留第一帧）" if prepared.converted_from == "GIF" else ""
            self.ctx.log(f"{name}: {prepared.converted_from} 转成 JPEG 上传{extra}", level="warning")
        return await api.upload_image(prepared, source=source, referer=referer)

    def _save_publish_evidence(self, api: Any) -> None:
        """发布结果不是干净的成功时，把发布接口的原始响应存进本次运行目录（响应里没有 cookie）。"""
        if api.last_publish is None:
            return
        status, text = api.last_publish
        try:
            path = self.ctx.evidence_path(f"{NAME}-publish-response.txt")
            path.write_text(f"HTTP {status}\n\n{text}", encoding="utf-8")
        except OSError as exc:
            self.ctx.log(f"保存发布接口响应失败: {exc}", level="debug")

    async def _verify(self, getter: Callable[[str], Any], item_id: str) -> bool | None:
        """发布后确认能查到：True 能查到，False 查不到（可能在审核），None 没确认成。绝不抛错。"""
        try:
            return (await getter(item_id)) is not None
        except PublishError as exc:  # 包括风控：内容已经发出去了，不能再报失败
            self.ctx.log(f"发布后确认失败（{exc}），以发布接口的结果为准", level="warning")
            return None

    async def _attach_topics(self, api: Any, draft_id: str, tags: list[str]) -> list[str]:
        """话题尽力添加：找不到 / 失败只记警告，不影响发布（风控与登录失效除外）。"""
        warnings: list[str] = []
        for tag in tags[:ARTICLE_TOPICS_MAX]:
            try:
                topic = await api.find_topic(draft_id, tag)
                if topic is None:
                    warnings.append(f"没找到话题「{tag}」")
                    continue
                await api.add_article_topic(draft_id, topic)
                self.ctx.log(f"已添加话题「{topic.get('name') or tag}」")
            except LoginRequired:
                raise
            except NeedsAction as exc:  # 风控 / 限流：话题只是锦上添花，跳过；真被封的话发布接口会再拦下
                self.ctx.log(f"添加话题时被知乎拦截（{exc}），跳过剩下的话题", level="warning")
                warnings.append("话题被知乎风控拦截，没有添加")
                break
            except PublishError as exc:
                self.ctx.log(f"添加话题「{tag}」失败：{exc}", level="warning")
                warnings.append(f"话题「{tag}」没加上")
        return warnings

    async def _publish_article(self, post: PlatformPost) -> Result:
        from . import zhihu_api, zhihu_html

        o = post.options
        title = post.title.strip()
        draft_only = _as_bool(o.get("draft")) is True
        toc = _as_bool(o.get("toc")) is True
        disclaimer = statement_type(post)
        column = column_id(o.get("column"))
        dirs = asset_dirs(post)
        warnings: list[str] = []
        write_url = f"{zhihu_api.ZHUANLAN}/write"

        async with self._api() as api:
            me = await api.me()
            self.ctx.log(f"以「{me.get('name')}」的身份发布知乎文章")

            # 1. 图片先传图床（失败不会留下草稿）
            refs = [r for r in zhihu_html.collect_image_urls(post.body) if not zhihu_html.is_zhimg(r)]
            referenced = {p for p in (resolve_local_image(r, dirs) for r in refs if not zhihu_html.is_remote(r)) if p}
            extra = [p for p in post.images if p.resolve() not in referenced]
            total = len(refs) + len(extra)
            images: dict[str, Any] = {}
            tail: list[Any] = []
            for i, ref in enumerate([*refs, *extra], start=1):
                self.ctx.log(f"上传图片 {i}/{total}: {ref.name if isinstance(ref, Path) else ref}")
                img = await self._image(api, ref, source="article", referer=write_url, dirs=dirs)
                if isinstance(ref, Path):
                    tail.append(img)
                else:
                    images[ref] = img
            html = zhihu_html.render_article(post.body, images, title=title, append=tail)
            cover = None
            if post.cover is not None:
                self.ctx.log(f"上传封面: {post.cover.name}")
                cover = await self._image(api, post.cover, source="article", referer=write_url, dirs=dirs)

            # 2. 草稿：建 → 写正文 → 封面 → 话题
            draft_id = await api.create_article_draft(title)
            edit_url = zhihu_api.article_edit_url(draft_id)
            self.ctx.log(f"已创建草稿：{edit_url}")
            try:
                await api.patch_article_draft(draft_id, {"title": title, "content": html, "table_of_contents": toc,
                                                         "delta_time": 30, "can_reward": False})
                if cover is not None:
                    try:
                        await api.patch_article_draft(draft_id, {"titleImage": cover.original_src,
                                                                 "isTitleImageFullScreen": False, "delta_time": 30},
                                                      what="设置封面")
                    except NeedsAction:
                        raise
                    except PublishError as exc:
                        self.ctx.log(f"设置封面失败：{exc}", level="warning")
                        warnings.append("封面没设置上")
                warnings += await self._attach_topics(api, draft_id, post.tags)
            except PublishError as exc:
                raise _with_draft(exc, edit_url) from exc

            if draft_only:
                message = "已保存为草稿（未发布）：打开链接确认后手动点「发布」"
                if disclaimer != "none" or column:
                    message += "；创作声明 / 专栏要在发布时自己选择"
                return self.result(Status.DRAFT, kind=post.kind, url=edit_url, item_id=draft_id,
                                   message=_join(message, warnings))

            # 3. 发布（不可逆）
            payload = zhihu_api.article_publish_payload(
                draft_id=draft_id, title=title, html=html, text_len=zhihu_html.text_length(html),
                disclaimer_type=disclaimer, toc=toc, column=column)
            rejected = "；内容已在草稿里，可打开链接修改后手动发布"
            try:
                data = await api.publish_content(payload, origin=zhihu_api.ZHUANLAN, referer=edit_url,
                                                 mark_submit=self.ctx.mark_submit_started)
            except PublishError as exc:
                self._save_publish_evidence(api)
                if exc.code is ErrorCode.PLATFORM_ERROR and not isinstance(exc, NeedsAction):
                    return self.result(Status.FAILED, kind=post.kind, url=edit_url, item_id=draft_id,
                                       error_code=ErrorCode.PLATFORM_ERROR.value,
                                       message=f"知乎拒绝发布：{exc}{rejected}")
                raise _with_draft(exc, edit_url) from exc
            code = zhihu_api.publish_code(data)
            article_id = zhihu_api.parse_publish_id(data) or draft_id
            url = zhihu_api.article_url(article_id)
            if code is None:
                if await self._verify(api.get_article, article_id):
                    return self.result(Status.PUBLISHED, kind=post.kind, url=url, item_id=article_id,
                                       message=_join("文章已发布", warnings))
                self._save_publish_evidence(api)
                raise PublishError(f"发布接口返回了无法识别的结果：先打开 {edit_url} 确认是否已发布，不要直接重发",
                                   code=ErrorCode.UNKNOWN_OUTCOME)
            if code not in (0, "0"):
                self._save_publish_evidence(api)
                return self.result(
                    Status.FAILED, kind=post.kind, url=edit_url, item_id=draft_id,
                    error_code=ErrorCode.PLATFORM_ERROR.value,
                    message=f"知乎拒绝发布（{code}: {zhihu_api.error_message(data)}）{rejected}")
            visible = await self._verify(api.get_article, article_id)
        if visible is False:
            return self.result(Status.SUBMITTED, kind=post.kind, url=url, item_id=article_id,
                               message=_join("知乎已接受发布，但暂时查不到文章（可能在审核中），稍后打开链接确认", warnings))
        return self.result(Status.PUBLISHED, kind=post.kind, url=url, item_id=article_id,
                           message=_join("文章已发布", warnings))

    async def _publish_pin(self, post: PlatformPost) -> Result:
        from . import zhihu_api, zhihu_html

        text = pin_text(post)
        async with self._api() as api:
            me = await api.me()
            self.ctx.log(f"以「{me.get('name')}」的身份发布知乎想法")
            images = []
            for i, path in enumerate(post.images, start=1):
                self.ctx.log(f"上传图片 {i}/{len(post.images)}: {path.name}")
                images.append(await self._image(api, path, source="pin", referer=zhihu_api.WWW_HOME, dirs=[]))
            draft_id = await api.create_content_draft("pin")
            html = zhihu_html.pin_html(text, images)
            payload = zhihu_api.pin_publish_payload(draft_id=draft_id, title=post.title.strip(), html=html,
                                                    text_len=zhihu_html.text_length(html), images=images)
            try:
                data = await api.publish_content(payload, origin=zhihu_api.WWW, referer=zhihu_api.WWW_HOME,
                                                 mark_submit=self.ctx.mark_submit_started)
            except PublishError:
                self._save_publish_evidence(api)
                raise
            code = zhihu_api.publish_code(data)
            if code not in (None, 0, "0"):
                self._save_publish_evidence(api)
                raise PublishError(f"知乎拒绝发布想法（{code}: {zhihu_api.error_message(data)}）")
            pin_id = zhihu_api.parse_publish_id(data)
            if not pin_id:
                self._save_publish_evidence(api)
                if code is None:
                    raise PublishError("发布接口返回了无法识别的结果：先到知乎个人主页「想法」里确认，不要直接重发",
                                       code=ErrorCode.UNKNOWN_OUTCOME)
                return self.result(Status.SUBMITTED, kind=post.kind,
                                   message="知乎返回发布成功，但没有想法 id：到个人主页「想法」里查看")
            visible = await self._verify(api.get_pin, pin_id)
        url = zhihu_api.pin_url(pin_id)
        if visible is False:
            return self.result(Status.SUBMITTED, kind=post.kind, url=url, item_id=pin_id,
                               message="知乎已接受发布，但暂时查不到想法（可能在审核中），稍后打开链接确认")
        return self.result(Status.PUBLISHED, kind=post.kind, url=url, item_id=pin_id, message="想法已发布")

    # ================================================================ 环境
    @classmethod
    def doctor(cls, settings: Settings) -> list[dict[str, Any]]:
        try:
            import mistune  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            return [{"name": "deps", "ok": False, "detail": f"缺少依赖: {exc}（重新安装 multipub）"}]
        return [{"name": "deps", "ok": True, "detail": "mistune / Pillow 可用（Markdown 转换、图片转码）"}]
