"""patchright 浏览器启动的公共封装（知乎登录、B站图文等自研流程使用）。

vendored 的 social-auto-upload（抖音/小红书）有自己的启动代码，但同样读取这里的
浏览器设置（channel / executable_path），见 multipub/_sau/conf.py。
只在 worker 子进程里导入本模块。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from .config import Settings
from .platforms.base import JobContext

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--lang=zh-CN"]


def launch_kwargs(settings: Settings, headless: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"headless": headless, "args": list(LAUNCH_ARGS)}
    if settings.browser.executable_path:
        kwargs["executable_path"] = settings.browser.executable_path
    elif settings.browser.channel:
        kwargs["channel"] = settings.browser.channel
    return kwargs


def load_storage_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@asynccontextmanager
async def open_context(
    settings: Settings,
    *,
    headless: bool,
    storage_state: dict[str, Any] | str | Path | None = None,
    **context_kwargs: Any,
) -> AsyncIterator[Any]:
    """打开一个独立的浏览器上下文；退出时关闭浏览器。"""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs(settings, headless))
        try:
            opts: dict[str, Any] = {"locale": "zh-CN", "viewport": {"width": 1440, "height": 960}}
            opts.update(context_kwargs)
            if storage_state is not None:
                opts["storage_state"] = str(storage_state) if isinstance(storage_state, Path) else storage_state
            context = await browser.new_context(**opts)
            try:
                yield context
            finally:
                await context.close()
        finally:
            await browser.close()


async def snapshot(page: Any, ctx: JobContext, name: str) -> str | None:
    """保存整页截图作为证据；失败不影响主流程。"""
    try:
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.run_dir / f"{ctx.platform}-{name}.png"
        await page.screenshot(path=str(path), full_page=True)
    except Exception:  # noqa: BLE001
        return None
    ctx.evidence.append(str(path))
    return str(path)
