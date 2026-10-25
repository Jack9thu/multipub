"""小红书：视频 + 图文笔记，基于 vendored social-auto-upload（见 multipub/_sau）。

浏览器流程在 vendored 的 xiaohongshu_uploader 里；这里负责参数映射、预校验、钩子和错误映射。
创作者中心域名可用环境变量 SAU_XHS_CREATOR_BASE_URL 切换（如 https://creator.rednote.com）。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models import Issue, Kind, PlatformPost, Result, Status, Unsupported
from . import _sau_bridge as bridge
from .base import PlatformAdapter

TITLE_MAX = 20
BODY_MAX = 1000
TAGS_MAX = 10
IMAGES_MAX = 18

KNOWN_OPTIONS = ("repost_source", "declaration", "collection")

# 「添加内容类型声明」下拉里能直接选的项。
# 来源：2026-10 调研到的开源发布工具抓取的网页端页面，未在真实账号上验证。
# 「来源转载」用 repost_source；「自主拍摄」要填地点和日期，不支持。
DECLARATION_OPTIONS = ("虚构演绎，仅供娱乐", "笔记含AI合成内容", "内容包含营销广告")
NO_DECLARATION = {False, "false", "none", "no", "off", "关闭", "无", "不声明"}
UNSUPPORTED_DECLARATIONS = {
    "来源转载": "转载请用 repost_source: <来源名称>",
    "内容来源声明": "转载请用 repost_source: <来源名称>；「自主拍摄」不支持",
    "自主拍摄": "「自主拍摄」要填地点和日期，multipub 不支持，请在网页上手动声明",
}


def resolve_declaration(post: PlatformPost) -> tuple[str | None, bool]:
    """内容类型声明：返回 (显式声明文字, 是否尽力选 AI 声明)。

    显式 options.declaration 优先（设置不上拒绝发布）；填 false 表示不声明（ai_generated 的 AI 声明也不选）；
    没填时沿用 ai_generated：尽力选「笔记含AI合成内容」，失败只告警。
    """
    value = post.options.get("declaration")
    if value is None:
        return None, post.ai_generated
    if value in NO_DECLARATION or (isinstance(value, str) and value.strip().lower() in NO_DECLARATION):
        return None, False
    return str(value).strip(), False


class XiaohongshuAdapter(PlatformAdapter):
    name = "xiaohongshu"
    display_name = "小红书"
    kinds = frozenset({Kind.VIDEO, Kind.NOTE})

    # ------------------------------------------------------------ 本地校验
    @classmethod
    def validate(cls, post: PlatformPost) -> list[Issue]:
        c = bridge.Checker(cls.name)
        opts = post.options
        if post.kind not in cls.kinds:
            c.error("kind", f"小红书不支持 {post.kind.value}")
            return c.issues
        if not post.title.strip():
            if post.kind is Kind.VIDEO:
                c.error("title", "小红书视频需要标题")
            elif not post.body.strip():
                c.error("title", "小红书图文需要标题（或至少有正文，用正文前 20 字作标题）")
            else:
                c.warn("title", "未填标题，将使用正文前 20 字作为标题")
        # SAU 会静默截断到 20 字，这里直接报错
        c.length("title", post.title, TITLE_MAX, what="小红书标题")
        utf16 = len(post.title.encode("utf-16-le")) // 2
        if len(post.title) <= TITLE_MAX < utf16:
            c.warn("title", f"标题里有 emoji 等字符，按网页的算法是 {utf16} 个字（emoji 算 2 个）；"
                            "标题框若因此截断标题，会拒绝发布（笔记不会发出），建议缩短")
        c.length("body", post.body, BODY_MAX, what="小红书正文")
        tags_text = "".join(f"#{t} " for t in post.tags)
        if len(post.body) <= BODY_MAX < len(post.body) + len(tags_text):
            c.warn("tags", "正文加话题超过 1000 字，末尾的话题可能加不上")
        c.tags(post.tags, limit=TAGS_MAX)
        if post.kind is Kind.VIDEO:
            c.ext("video", post.video, bridge.VIDEO_EXTS)
            c.ext("cover", post.cover, bridge.IMAGE_EXTS)
        else:
            if not 1 <= len(post.images) <= IMAGES_MAX:
                c.error("images", f"小红书图文需要 1-{IMAGES_MAX} 张图片，当前 {len(post.images)} 张")
            for img in post.images:
                c.ext("images", img, bridge.IMAGE_EXTS)
            if any(img.suffix.lower() == ".bmp" for img in post.images):
                c.warn("images", "小红书网页端可能不接受 BMP，建议转成 JPG / PNG")
            if post.cover is not None:
                c.warn("cover", "小红书图文以第一张图片为封面，cover 已忽略")
        c.options(opts, KNOWN_OPTIONS, post.kind)
        c.str_option(opts, "repost_source")
        c.str_option(opts, "collection")  # 发布页「加入合集」，按名称精确匹配
        if isinstance(opts.get("collection"), str) and not opts["collection"].strip():
            c.error("options.collection", "合集名不能为空；不加入合集就删掉这一项")
        decl = opts.get("declaration")
        if decl is True or (decl is not None and not isinstance(decl, (str, bool))):
            c.error("options.declaration", "应为页面上的声明选项文字（如「虚构演绎，仅供娱乐」），或 false 表示不声明")
        else:
            text, ai = resolve_declaration(post)
            if text in UNSUPPORTED_DECLARATIONS:
                c.error("options.declaration", UNSUPPORTED_DECLARATIONS[text])
            elif text is not None and text not in DECLARATION_OPTIONS:
                c.warn("options.declaration", f"「{text}」不在已知选项里（{' / '.join(DECLARATION_OPTIONS)}）："
                                              "文字必须和页面完全一致，选不上会拒绝发布（笔记不会发出）")
            if text is not None and post.ai_generated and text != DECLARATION_OPTIONS[1]:
                c.warn("options.declaration", f"ai_generated 为 true，但声明是「{text}」：内容类型声明是单选，"
                                              "以 declaration 为准，AI 合成内容请在正文里注明")
            elif decl is not None and text is None and post.ai_generated:
                c.warn("options.declaration", "ai_generated 为 true 但关闭了内容类型声明")
            if opts.get("repost_source") and (text is not None or ai):
                c.warn("options.repost_source", "内容类型声明可能是单选：同时声明转载和其他类型时，"
                                                "以后选的 AI / declaration 声明为准")
        c.schedule(post.schedule)
        return c.issues

    @classmethod
    def doctor(cls, settings: Settings) -> list[dict[str, Any]]:
        return bridge.doctor_items()

    # ------------------------------------------------------------ 浏览器动作
    def _sau(self) -> Any:
        from .._sau.uploader.xiaohongshu_uploader import main as xhs  # configure() 之后才能导入

        return xhs

    async def login(self) -> Result:
        with bridge.sau_session(self.ctx, "xiaohongshu"):
            xhs = self._sau()
            return await bridge.run_login(self, xhs.xiaohongshu_setup)

    async def check(self) -> Result:
        with bridge.sau_session(self.ctx, "xiaohongshu"):
            xhs = self._sau()
            return await bridge.run_check(self, xhs.cookie_auth)

    async def publish(self, post: PlatformPost) -> Result:
        if post.kind not in self.kinds:
            raise Unsupported(f"小红书不支持 {post.kind.value}")
        bridge.precheck(type(self), self.ctx, post)
        with bridge.sau_session(self.ctx, "xiaohongshu"):
            xhs = self._sau()
            uploader = self.build_uploader(xhs, post)
            async with bridge.translate_errors(self.ctx):
                await uploader.main()
        if post.schedule:
            status, msg = Status.SCHEDULED, f"已设置定时发布（{post.schedule:%Y-%m-%d %H:%M}）"
        else:
            status, msg = Status.SUBMITTED, "已提交，审核通过后公开"
        note_id = getattr(uploader, "published_note_id", None) or None
        if note_id:
            msg += f"；笔记 ID {note_id}"
        msg += "；小红书不返回可分享的笔记链接，请到创作服务平台「笔记管理」查看"
        for warning in getattr(uploader, "declaration_warnings", None) or []:
            msg += f"；注意：{warning}"
        return self.result(status, kind=post.kind, message=msg, item_id=note_id)

    def build_uploader(self, xhs: Any, post: PlatformPost) -> Any:
        """PlatformPost → vendored SAU 的 XiaoHongShuVideo / XiaoHongShuNote。"""
        scheduled = post.schedule is not None
        declaration, ai_declaration = resolve_declaration(post)
        common: dict[str, Any] = {
            "tags": list(post.tags),
            "publish_date": post.schedule or 0,
            "account_file": str(self.account_file),
            "publish_strategy": (xhs.XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED if scheduled
                                 else xhs.XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE),
            "debug": False,
            "headless": self.ctx.headless,
            "hooks": bridge.make_hooks(self.ctx, post.kind),
            "ai_generated": ai_declaration,  # 上传器里只用来决定要不要尽力选 AI 声明
            "repost_source": str(post.options.get("repost_source") or "") or None,
            "declaration": declaration,
            "collection": str(post.options.get("collection") or "").strip() or None,
        }
        if post.kind is Kind.VIDEO:
            return xhs.XiaoHongShuVideo(
                title=post.title,
                file_path=str(post.video),
                desc=post.body,
                thumbnail_path=str(post.cover) if post.cover else None,
                **common,
            )
        return xhs.XiaoHongShuNote(
            image_paths=[str(p) for p in post.images],
            note=post.body,
            title=post.title or None,
            **common,
        )
