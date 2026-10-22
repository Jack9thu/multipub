"""模拟测试用：让 worker 子进程里的每个浏览器上下文都走本地仿页面，并拒绝所有真实网络请求。

用法（在某个模拟目录的 sitecustomize.py 里）：

    from tests.sim._routing import install
    install(handler)   # async def handler(route, request) -> bool：处理了返回 True

没被 handler 处理的请求一律 abort，保证模拟时不会访问真实平台。
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

Handler = Callable[[Any, Any], Awaitable[bool]]


def install(handler: Handler) -> None:
    from patchright.async_api import Browser, BrowserType

    async def _route(route: Any, request: Any) -> None:
        try:
            if await handler(route, request):
                return
        except Exception as exc:  # noqa: BLE001 - 仿页面出错时让测试看得到
            print(f"[sim] handler error for {request.url}: {exc!r}", flush=True, file=__import__("sys").stderr)
        await route.abort()

    orig_new_context = Browser.new_context
    orig_persistent = BrowserType.launch_persistent_context

    async def new_context(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = await orig_new_context(self, *args, **kwargs)
        await ctx.route("**/*", _route)
        return ctx

    async def launch_persistent_context(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = await orig_persistent(self, *args, **kwargs)
        await ctx.route("**/*", _route)
        return ctx

    Browser.new_context = new_context  # type: ignore[method-assign]
    BrowserType.launch_persistent_context = launch_persistent_context  # type: ignore[method-assign]
    os.environ["MULTIPUB_SIM_ACTIVE"] = "1"
