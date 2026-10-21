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

KNOWN_OPTIONS = ("repost_source",)


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
        if post.ai_generated and opts.get("repost_source"):
            c.warn("options.repost_source", "内容类型声明可能是单选：同时声明转载和 AI 合成时以 AI 声明为准")
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
        common: dict[str, Any] = {
            "tags": list(post.tags),
            "publish_date": post.schedule or 0,
            "account_file": str(self.account_file),
            "publish_strategy": (xhs.XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED if scheduled
                                 else xhs.XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE),
            "debug": False,
            "headless": self.ctx.headless,
            "hooks": bridge.make_hooks(self.ctx, post.kind),
            "ai_generated": post.ai_generated,
            "repost_source": str(post.options.get("repost_source") or "") or None,
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
