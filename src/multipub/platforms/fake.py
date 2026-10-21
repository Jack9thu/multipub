"""测试用假平台（MULTIPUB_ENABLE_FAKE=1 时注册）。行为由 options.fake 控制：

    ok (默认) | fail | needs_action | hang | crash_after_submit | hang_after_submit
"""

from __future__ import annotations

import asyncio
import os

from ..models import Issue, Kind, LoginRequired, PlatformPost, PublishError, Result, Status
from .base import PlatformAdapter


class FakeAdapter(PlatformAdapter):
    name = "fake"
    display_name = "Fake"
    kinds = frozenset({Kind.VIDEO, Kind.NOTE, Kind.ARTICLE})

    @classmethod
    def validate(cls, post: PlatformPost) -> list[Issue]:
        if len(post.title) > 50:
            return [Issue("error", "title", "标题超过 50 字", "fake")]
        return []

    async def login(self) -> Result:
        self.ctx.qrcode(self.ctx.run_dir / "fake-qr.png")
        self.account_file.parent.mkdir(parents=True, exist_ok=True)
        self.account_file.write_text("{}", encoding="utf-8")
        return self.result(Status.OK, message="logged in")

    async def check(self) -> Result:
        if not self.account_file.is_file():
            raise LoginRequired("未登录")
        return self.result(Status.OK)

    async def publish(self, post: PlatformPost) -> Result:
        mode = post.options.get("fake", os.environ.get("MULTIPUB_FAKE_MODE", "ok"))
        self.ctx.log(f"fake publish mode={mode}")
        if mode == "fail":
            raise PublishError("平台拒绝", retryable=True)
        if mode == "needs_action":
            raise LoginRequired("cookie 失效")
        if mode == "hang":
            await asyncio.sleep(3600)
        self.ctx.mark_submit_started()
        if mode == "hang_after_submit":
            await asyncio.sleep(3600)
        if mode == "crash_after_submit":
            os._exit(9)
        return self.result(Status.PUBLISHED, kind=post.kind, url="https://example.com/p/1", item_id="1")
