"""worker 子进程：执行一个平台的一次 login / check / publish。

用法（由 runner 调用，一般不需要手动执行）：

    python -m multipub.worker < job.json

job.json:
    {"action": "publish", "platform": "douyin", "account": "main", "home": "...",
     "headless": true, "run_dir": "...", "timeout": 900, "post": {...PlatformPost...}}

stdout：一行一个 JSON 事件（log / qrcode / needs_input / submit_started / result），
最后一行一定是 {"event": "result", ...}。stderr：平台日志（loguru 等）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .config import load_settings
from .models import (ErrorCode, PlatformPost, PublishError, Result, Status,
                     now_iso)
from .platforms import load_adapter
from .platforms.base import JobContext, stdout_emit


def _failure(ctx: JobContext, status: Status, code: str, message: str,
             retryable: bool, evidence: list[str] | None = None, kind: str = "",
             url: str | None = None) -> Result:
    return Result(
        platform=ctx.platform, account=ctx.account, action=ctx.action, status=status,
        kind=kind, message=message, error_code=code, url=url,
        # 已经触发过发布就绝不标记为可重试
        retryable=retryable and not ctx.submit_started,
        submit_started=ctx.submit_started,
        evidence=list(ctx.evidence) + list(evidence or []),
        finished_at=now_iso(),
    )


async def run_job(job: dict[str, Any], emit=stdout_emit) -> Result:
    settings = load_settings(job.get("home"))
    settings.ensure_dirs()
    action = job["action"]
    ctx = JobContext(
        settings=settings,
        platform=job["platform"],
        account=job["account"],
        action=action,
        headless=bool(job.get("headless", settings.browser.headless)),
        run_dir=Path(job.get("run_dir") or settings.runs_dir / "adhoc"),
        emit=emit,
        timeout=job.get("timeout"),
    )
    post = PlatformPost.from_dict(job["post"]) if job.get("post") else None
    kind = post.kind.value if post else ""
    started = now_iso()
    try:
        adapter = load_adapter(ctx.platform)(ctx)
        if action == "login":
            coro = adapter.login()
        elif action == "check":
            coro = adapter.check()
        elif action == "publish":
            if post is None:
                raise PublishError("publish 缺少 post", code=ErrorCode.INTERNAL)
            coro = adapter.publish(post)
        else:
            raise PublishError(f"未知动作: {action}", code=ErrorCode.INTERNAL)
        timeout = job.get("timeout")
        result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
    except PublishError as exc:
        result = _failure(ctx, exc.status, exc.code.value, str(exc), exc.retryable, exc.evidence, kind,
                          getattr(exc, "url", None))
    except asyncio.TimeoutError:
        msg = "超时"
        if ctx.submit_started:
            msg += "（已触发发布，结果未知：请先到平台后台确认，不要直接重发）"
            code = ErrorCode.UNKNOWN_OUTCOME.value
        else:
            code = ErrorCode.TIMEOUT.value
        result = _failure(ctx, Status.FAILED, code, msg, True, kind=kind)
    except Exception as exc:  # noqa: BLE001 - 兜底，保证一定输出 result
        traceback.print_exc(file=sys.stderr)
        code = ErrorCode.UNKNOWN_OUTCOME.value if ctx.submit_started else ErrorCode.INTERNAL.value
        result = _failure(ctx, Status.FAILED, code, f"{type(exc).__name__}: {exc}", False, kind=kind)
    result.started_at = result.started_at or started
    result.submit_started = result.submit_started or ctx.submit_started
    if ctx.submit_started:
        result.retryable = False
    return result


async def _run_watching_parent(job: dict[str, Any]) -> Result:
    """主进程退出时（stdin 读到 EOF）取消任务：适配器的 finally 会关浏览器、杀 biliup。"""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        print("主进程已退出，取消当前任务", file=sys.stderr, flush=True)
        loop.call_soon_threadsafe(task.cancel)
        killer = threading.Timer(20, lambda: os._exit(3))  # 清理卡住时强制退出
        killer.daemon = True
        killer.start()

    threading.Thread(target=watch, daemon=True).start()
    return await run_job(job)


def main() -> int:
    # Windows 控制台默认 GBK；统一 UTF-8，父进程按 UTF-8 解码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    # Windows 上 asyncio 默认就是 Proactor 事件循环，patchright 启动浏览器子进程依赖它
    # 第一行是任务；runner 会让 stdin 保持打开，用 EOF 表示主进程已经不在了
    job = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    if job.get("watch_parent"):
        try:
            result = asyncio.run(_run_watching_parent(job))
        except asyncio.CancelledError:
            return 3
    else:
        result = asyncio.run(run_job(job))
    stdout_emit({"event": "result", "result": result.to_dict()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
