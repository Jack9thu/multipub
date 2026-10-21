"""发布记录（history.jsonl）与账号锁。

- 每次发布结果追加一行 JSON，便于查重、对账、给 Claude Code 汇报。
- 同一平台同一账号同一时间只允许一个任务（抖音的 verify_code.txt、cookie 文件都是共享的）。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock, Timeout

from .config import Settings
from .models import ErrorCode, PlatformPost, Result, Status


def post_fingerprint(post: PlatformPost) -> str:
    """同一内容发到同一平台账号的指纹：文本 + 媒体文件（路径、大小、修改时间）。"""
    def file_sig(p: Path | None) -> list[Any] | None:
        if p is None:
            return None
        try:
            st = p.stat()
            return [p.name, st.st_size, int(st.st_mtime)]
        except OSError:
            return [p.name, None, None]

    payload = {
        "platform": post.platform,
        "account": post.account,
        "kind": post.kind.value,
        "title": post.title,
        "body": post.body,
        "video": file_sig(post.video),
        "images": [file_sig(p) for p in post.images],
        "options": post.options,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def append(settings: Settings, result: Result, *, run_id: str, fingerprint: str = "",
           source: str = "", title: str = "") -> None:
    settings.history_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": run_id, "fingerprint": fingerprint, "source": source, "title": title,
              **result.to_dict()}
    with FileLock(str(settings.history_file) + ".lock", timeout=30):
        with settings.history_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read(settings: Settings, limit: int | None = None) -> list[dict[str, Any]]:
    if not settings.history_file.is_file():
        return []
    rows = []
    for line in settings.history_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:] if limit else rows


_DONE = {Status.PUBLISHED.value, Status.SUBMITTED.value, Status.SCHEDULED.value}


def find_previous(settings: Settings, fingerprint: str) -> dict[str, Any] | None:
    """返回同一指纹最近一次"已发出"或"结果未知"的记录。"""
    for row in reversed(read(settings)):
        if row.get("fingerprint") != fingerprint or row.get("action") != "publish":
            continue
        if row.get("status") in _DONE:
            return row
        if row.get("status") == Status.FAILED.value and row.get("error_code") == ErrorCode.UNKNOWN_OUTCOME.value:
            return row  # 已触发发布但结果未知，同样需要人确认
    return None


@contextmanager
def account_lock(settings: Settings, platform: str, account: str, wait: float = 5) -> Iterator[None]:
    settings.locks_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.locks_dir / f"{platform}-{account}.lock"))
    try:
        lock.acquire(timeout=wait)
    except Timeout as exc:
        raise RuntimeError(f"{platform}/{account} 正被另一个任务使用，请稍后再试") from exc
    try:
        yield
    finally:
        lock.release()
