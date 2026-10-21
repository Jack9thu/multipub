"""主进程侧：为每个平台任务启动一个 worker 子进程，转发事件，处理超时。

一个任务一个进程的好处：patchright / asyncio / loguru 的全局状态互不影响；
某个平台卡死时可以连同浏览器子进程整棵树杀掉（Windows 用 taskkill /T /F）。
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .models import ErrorCode, PlatformPost, Result, Status, now_iso

EventHandler = Callable[[dict[str, Any]], None]


class WorkerInterrupted(KeyboardInterrupt):
    """主进程被 Ctrl+C / 终止时抛出：worker 进程树已结束，result 记录了中断时的状态。"""

    def __init__(self, result: Result):
        super().__init__(result.message)
        self.result = result

# worker 输出 result 之后、进程退出之前允许的收尾时间
_GRACE_SECONDS = 15


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _close_stdin(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def run_worker(
    settings: Settings,
    *,
    action: str,
    platform: str,
    account: str,
    headless: bool,
    run_dir: Path,
    post: PlatformPost | None = None,
    timeout: int | None = None,
    on_event: EventHandler | None = None,
) -> Result:
    timeout = timeout or settings.timeout_for(action if action != "publish" or post is None
                                              else f"publish_{post.kind.value}")
    run_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "action": action,
        "platform": platform,
        "account": account,
        "home": str(settings.home),
        "headless": headless,
        "run_dir": str(run_dir),
        # worker 内部超时略短于外部硬超时，尽量让 worker 自己体面地结束并汇报
        "timeout": max(30, timeout - 30),
        # stdin 管道一直开着；主进程一旦退出（被杀、关窗口），worker 读到 EOF 就自行取消并退出
        "watch_parent": True,
        "post": post.to_dict() if post else None,
    }
    stderr_path = run_dir / f"{platform}-{action}.log"
    started = now_iso()

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True

    with stderr_path.open("w", encoding="utf-8") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "multipub.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_fh,
            env=_worker_env(),
            **popen_kwargs,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(job, ensure_ascii=False).encode("utf-8") + b"\n")
        proc.stdin.flush()

        lines: queue.Queue[bytes | None] = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()

        deadline = time.monotonic() + timeout
        result: Result | None = None
        submit_started = False
        eof = False
        try:
            while not eof:
                remaining = deadline - time.monotonic()
                if result is not None:
                    remaining = min(remaining, _GRACE_SECONDS)
                if remaining <= 0:
                    break
                try:
                    raw = lines.get(timeout=remaining)
                except queue.Empty:
                    break
                if raw is None:
                    eof = True
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = {"event": "log", "level": "debug", "message": text,
                             "platform": platform, "account": account}
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "submit_started":
                    submit_started = True
                if event.get("event") == "result":
                    try:
                        result = Result.from_dict(event["result"])
                    except (KeyError, TypeError, ValueError):
                        result = None
                    continue
                if on_event:
                    on_event(event)

        except BaseException:
            # Ctrl+C / SIGTERM：先结束整棵进程树（浏览器、biliup），再把中断时的状态交给调用方记录
            _close_stdin(proc)
            _kill_tree(proc)
            interrupted = result or Result(
                platform=platform, account=account, action=action, status=Status.FAILED,
                kind=post.kind.value if post else "",
                message=("已中断，但已触发发布：结果未知，请先到平台后台确认，不要直接重发" if submit_started
                         else "已中断（尚未触发发布）"),
                error_code=(ErrorCode.UNKNOWN_OUTCOME if submit_started else ErrorCode.INTERNAL).value,
                retryable=not submit_started, submit_started=submit_started,
                started_at=started, finished_at=now_iso(),
            )
            raise WorkerInterrupted(interrupted) from None
        if proc.poll() is None:
            try:
                proc.wait(timeout=5 if result is not None else 0.1)
            except subprocess.TimeoutExpired:
                pass
        _close_stdin(proc)
        _kill_tree(proc)

    if result is None:
        timed_out = time.monotonic() >= deadline
        if submit_started:
            code, msg = ErrorCode.UNKNOWN_OUTCOME.value, "worker 异常结束，但已触发发布：请先到平台后台确认，不要直接重发"
        elif timed_out:
            code, msg = ErrorCode.TIMEOUT.value, f"超过 {timeout} 秒未完成，已终止"
        else:
            code, msg = ErrorCode.INTERNAL.value, f"worker 异常退出（退出码 {proc.returncode}）"
        result = Result(
            platform=platform, account=account, action=action, status=Status.FAILED,
            kind=post.kind.value if post else "", message=f"{msg}；日志: {stderr_path}",
            error_code=code, retryable=not submit_started, submit_started=submit_started,
            started_at=started, finished_at=now_iso(),
        )
    result.started_at = result.started_at or started
    if str(stderr_path) not in result.evidence and stderr_path.stat().st_size > 0:
        result.evidence.append(str(stderr_path))
    return result
