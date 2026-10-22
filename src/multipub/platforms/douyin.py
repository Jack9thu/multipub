"""抖音：视频 + 图文，基于 vendored social-auto-upload（见 multipub/_sau）。

浏览器流程（登录、cookie 校验、发布）都在 vendored 的 douyin_uploader 里；
这里负责参数映射、预校验、钩子（发布点击 / 短信验证码 / 截图）和错误映射。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import Issue, Kind, PlatformPost, Result, Status, Unsupported
from . import _sau_bridge as bridge
from .base import PlatformAdapter

TITLE_MAX_VIDEO = 30
TITLE_MAX_NOTE = 20
BODY_MAX = 1000
IMAGES_MAX = 35
TAGS_SOFT_MAX = 5
AI_DECLARATION = "内容由AI生成"  # 与 vendored DOUYIN_AI_DECLARATION 一致

VIDEO_OPTIONS = ("declaration", "collection", "product_link", "product_title", "sync_toutiao",
                 "thumbnail_landscape", "thumbnail_portrait")
NOTE_OPTIONS = ("bgm",)
NO_DECLARATION = (False, "", "none", "无")


def resolve_declaration(post: PlatformPost) -> str | None:
    """自主声明：显式 options.declaration 优先；未填且 ai_generated 时用「内容由AI生成」；否则不声明。"""
    value = post.options.get("declaration")
    if value is None:
        return AI_DECLARATION if post.ai_generated else None
    if value in NO_DECLARATION or (isinstance(value, str) and value.strip().lower() in NO_DECLARATION):
        return None
    return str(value).strip()


def utf16_len(text: str) -> int:
    """网页 input 的 maxlength 按 UTF-16 码元计数：emoji 等补充平面字符算 2 个。"""
    return len(text.encode("utf-16-le")) // 2


def _is_landscape(path: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.size
    except (OSError, ValueError):
        return False
    return width > height


def cover_slots(post: PlatformPost) -> tuple[Path | None, Path | None]:
    """(竖封面 3:4, 横封面 4:3)。显式 thumbnail_* 优先；否则按 cover 的宽高判断放哪一格。"""
    portrait = bridge.option_path(post, "thumbnail_portrait")
    landscape = bridge.option_path(post, "thumbnail_landscape")
    if post.cover is not None and portrait is None and landscape is None:
        if _is_landscape(post.cover):
            landscape = post.cover
        else:
            portrait = post.cover
    return portrait, landscape


class DouyinAdapter(PlatformAdapter):
    name = "douyin"
    display_name = "抖音"
    kinds = frozenset({Kind.VIDEO, Kind.NOTE})

    # ------------------------------------------------------------ 本地校验
    @classmethod
    def validate(cls, post: PlatformPost) -> list[Issue]:
        c = bridge.Checker(cls.name)
        opts = post.options
        if post.kind is Kind.VIDEO:
            if not post.title.strip():
                c.error("title", "抖音视频需要标题")
            # SAU 会静默截断到 30 字，这里直接报错，避免发出去的标题和预期不同
            c.length("title", post.title, TITLE_MAX_VIDEO, what="抖音视频标题")
            c.length("body", post.body, BODY_MAX, hard=False, what="作品描述")
            c.ext("video", post.video, bridge.VIDEO_EXTS)
            c.ext("cover", post.cover, bridge.IMAGE_EXTS)
            c.options(opts, VIDEO_OPTIONS, post.kind)
            portrait = bridge.check_option_file(c, post, "thumbnail_portrait", bridge.IMAGE_EXTS)
            landscape = bridge.check_option_file(c, post, "thumbnail_landscape", bridge.IMAGE_EXTS)
            if portrait and landscape:
                c.warn("options.thumbnail_landscape", "同时设置了竖封面和横封面，抖音流程只会应用竖封面")
            if post.cover is not None and (opts.get("thumbnail_portrait") or opts.get("thumbnail_landscape")):
                c.warn("cover", "已设置 thumbnail_portrait / thumbnail_landscape，cover 被忽略")
            for key in ("collection", "product_link", "product_title"):
                c.str_option(opts, key)
            c.bool_option(opts, "sync_toutiao")
            if bool(opts.get("product_link")) != bool(opts.get("product_title")):
                c.warn("options.product_link", "product_link 和 product_title 需要同时填写，否则不会挂商品")
            decl = opts.get("declaration")
            if decl is True or (decl is not None and not isinstance(decl, (str, bool))):
                c.error("options.declaration", "应为页面上的声明选项文字（如「内容由AI生成」），或 false 表示不声明")
            elif post.ai_generated and resolve_declaration(post) is None:
                c.warn("options.declaration", "ai_generated 为 true 但关闭了自主声明")
        elif post.kind is Kind.NOTE:
            if not post.title.strip():
                c.error("title", "抖音图文需要标题")
            c.length("title", post.title, TITLE_MAX_NOTE, what="抖音图文标题")
            c.length("body", post.body, BODY_MAX, what="抖音图文正文")
            if not 1 <= len(post.images) <= IMAGES_MAX:
                c.error("images", f"抖音图文需要 1-{IMAGES_MAX} 张图片，当前 {len(post.images)} 张")
            for img in post.images:
                c.ext("images", img, bridge.IMAGE_EXTS)
            if post.cover is not None:
                c.warn("cover", "抖音图文以第一张图片为封面，cover 已忽略")
            c.options(opts, NOTE_OPTIONS, post.kind)
            c.str_option(opts, "bgm")
            if post.ai_generated:
                c.warn("ai_generated", "抖音图文流程不支持自动勾选 AI 声明，发布后请在抖音 App 内补充")
        else:
            c.error("kind", f"抖音不支持 {post.kind.value}")
        limit = TITLE_MAX_VIDEO if post.kind is Kind.VIDEO else TITLE_MAX_NOTE
        if len(post.title) <= limit < utf16_len(post.title):
            c.warn("title", f"标题含 emoji 等字符：抖音网页按 2 个字计数时会超过 {limit} 字被截断"
                            "（发布前会读回核对，被截断则中止、不会发出），建议缩短")
        typed = len(post.body.strip()) + sum(len(t) + 2 for t in post.tags)  # 话题以「 #话题」接在正文后面
        if len(post.body) <= BODY_MAX < typed:
            c.warn("tags", f"正文加话题共 {typed} 字，超过 {BODY_MAX} 字时末尾的话题可能被平台截掉")
        c.tags(post.tags, soft_limit=TAGS_SOFT_MAX)
        c.schedule(post.schedule)
        return c.issues

    @classmethod
    def doctor(cls, settings: Settings) -> list[dict[str, Any]]:
        return bridge.doctor_items()

    # ------------------------------------------------------------ 浏览器动作
    def _sau(self) -> Any:
        from .._sau.uploader.douyin_uploader import main as dy  # configure() 之后才能导入

        return dy

    async def login(self) -> Result:
        with bridge.sau_session(self.ctx, "douyin"):
            dy = self._sau()
            return await bridge.run_login(self, dy.douyin_setup)

    async def check(self) -> Result:
        with bridge.sau_session(self.ctx, "douyin"):
            dy = self._sau()
            return await bridge.run_check(self, dy.cookie_auth)

    async def publish(self, post: PlatformPost) -> Result:
        if post.kind not in self.kinds:
            raise Unsupported(f"抖音不支持 {post.kind.value}")
        bridge.precheck(type(self), self.ctx, post)
        with bridge.sau_session(self.ctx, "douyin"):
            dy = self._sau()
            uploader = self.build_uploader(dy, post)
            async with bridge.translate_errors(self.ctx):
                if post.kind is Kind.VIDEO:
                    await uploader.douyin_upload_video()
                else:
                    await uploader.douyin_upload_note()
        return self._success(post, getattr(uploader, "published_item_id", None),
                             notes=list(getattr(uploader, "notes", None) or []))

    def build_uploader(self, dy: Any, post: PlatformPost) -> Any:
        """PlatformPost → vendored SAU 的 DouYinVideo / DouYinNote。"""
        hooks = bridge.make_hooks(
            self.ctx, post.kind,
            sms_message="抖音要求短信验证：收到验证码后运行 mpub verify-code douyin <验证码>",
        )
        scheduled = post.schedule is not None
        common: dict[str, Any] = {
            "tags": list(post.tags),
            "publish_date": post.schedule or 0,
            "account_file": str(self.account_file),
            "publish_strategy": dy.DOUYIN_PUBLISH_STRATEGY_SCHEDULED if scheduled else dy.DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
            "debug": False,
            "headless": self.ctx.headless,
            "hooks": hooks,
        }
        opts = post.options
        if post.kind is Kind.VIDEO:
            portrait, landscape = cover_slots(post)
            return dy.DouYinVideo(
                title=post.title,
                file_path=str(post.video),
                desc=post.body,
                thumbnail_landscape_path=str(landscape) if landscape else None,
                thumbnail_portrait_path=str(portrait) if portrait else None,
                productLink=str(opts.get("product_link") or ""),
                productTitle=str(opts.get("product_title") or ""),
                collection_name=str(opts["collection"]) if opts.get("collection") else None,
                declaration=resolve_declaration(post),
                # 三态：没写 → None（不碰开关，保持页面默认）；true → 打开；false → 关闭（关不掉则拒绝发布）
                sync_toutiao=opts["sync_toutiao"] if isinstance(opts.get("sync_toutiao"), bool) else None,
                **common,
            )
        return dy.DouYinNote(
            image_paths=[str(p) for p in post.images],
            note=post.body,
            title=post.title,
            bgm=str(opts.get("bgm") or ""),
            **common,
        )

    def _success(self, post: PlatformPost, item_id: str | None, notes: list[str] | None = None) -> Result:
        url = None
        if item_id:
            url = f"https://www.douyin.com/{'video' if post.kind is Kind.VIDEO else 'note'}/{item_id}"
        if post.schedule:
            status, msg = Status.SCHEDULED, f"已设置定时发布（{post.schedule:%Y-%m-%d %H:%M}）"
        else:
            status, msg = Status.SUBMITTED, "已提交，审核通过后公开"
        if not url:
            msg += "；抖音未返回作品链接，请到创作者中心「作品管理」查看"
        for note in notes or []:  # 与清单不一致的地方（封面没设上、合集没找到等）
            msg += f"；{note}"
        return self.result(status, kind=post.kind, message=msg, url=url, item_id=item_id)

