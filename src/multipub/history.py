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


def _record(result: Result, *, run_id: str, fingerprint: str, source: str, title: str) -> dict[str, Any]:
    return {"run_id": run_id, "fingerprint": fingerprint, "source": source, "title": title, **result.to_dict()}


def _write_line(settings: Settings, record: dict[str, Any]) -> None:
    settings.history_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with FileLock(str(settings.history_file) + ".lock", timeout=30):
        with settings.history_file.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _pending_file(settings: Settings, run_id: str, platform: str, account: str) -> Path:
    return settings.home / "pending" / f"{run_id}-{platform}-{account}.json"


def append(settings: Settings, result: Result, *, run_id: str, fingerprint: str = "",
           source: str = "", title: str = "") -> None:
    """追加一条最终结果，并删掉同一次发布的「结果待定」记录（见 mark_pending）。"""
    _write_line(settings, _record(result, run_id=run_id, fingerprint=fingerprint, source=source, title=title))
    _pending_file(settings, run_id, result.platform, result.account).unlink(missing_ok=True)


def mark_pending(settings: Settings, result: Result, *, run_id: str, fingerprint: str = "",
                 source: str = "", title: str = "") -> None:
    """worker 一报 submit_started，主进程就先记一笔「已点发布、结果待定」（<数据目录>/pending/ 下一个文件）。

    正常结束时 append() 写最终结果并删掉它；主进程如果在中途被强行结束（任务管理器、关窗口、kill -9），
    它会留下来：查重据此拦住原样重发，之后的 mpub 发现没有任务在用这个账号时把它转成一条 unknown_outcome 记录。
    """
    path = _pending_file(settings, run_id, result.platform, result.account)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _record(result, run_id=run_id, fingerprint=fingerprint, source=source, title=title)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row.get("run_id"), row.get("platform"), row.get("account"))


def _read_lines(settings: Settings) -> list[dict[str, Any]]:
    if not settings.history_file.is_file():
        return []
    rows = []
    for line in settings.history_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _pending(settings: Settings) -> list[tuple[Path, dict[str, Any]]]:
    d = settings.home / "pending"
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            out.append((f, {**row, "pending": True}))
    return out


def read(settings: Settings, limit: int | None = None) -> list[dict[str, Any]]:
    """发布记录；末尾附上还没有最终结果的「结果待定」记录（"pending": true）。"""
    rows = _read_lines(settings)
    final = {_key(r) for r in rows}
    rows += [row for _, row in _pending(settings) if _key(row) not in final]
    return rows[-limit:] if limit else rows


def settle_pending(settings: Settings) -> None:
    """把发布途中被强行结束留下的「结果待定」记录转成正式记录（unknown_outcome）。

    账号锁拿得到，说明没有 mpub 正在用这个账号发布；拿不到就是还在发，留着不动。
    """
    final = {_key(r) for r in _read_lines(settings)}
    for path, row in _pending(settings):
        if _key(row) in final:
            path.unlink(missing_ok=True)
            continue
        settings.locks_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(settings.locks_dir / f"{row.get('platform')}-{row.get('account')}.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            continue
        try:
            row.pop("pending", None)
            row["message"] = "发布途中 mpub 被结束，已点过发布：结果未知，请先到平台后台确认，不要直接重发"
            _write_line(settings, row)
            path.unlink(missing_ok=True)
        finally:
            lock.release()


_DONE = {Status.PUBLISHED.value, Status.SUBMITTED.value, Status.SCHEDULED.value}


def find_previous(settings: Settings, fingerprint: str) -> dict[str, Any] | None:
    """返回同一指纹最近一次"已发出"或"结果未知"的记录。

    点了发布之后平台明确拒绝（platform_error 等）不算：适配器只在确定没发出时才这样报，可以原样重发；
    拿不准的一律报 unknown_outcome。
    """
    for row in reversed(read(settings)):
        if row.get("fingerprint") != fingerprint or row.get("action") != "publish":
            continue
        if row.get("status") in _DONE:
            return row
        if row.get("status") == Status.FAILED.value and row.get("error_code") == ErrorCode.UNKNOWN_OUTCOME.value:
            return row  # 已触发发布但结果未知（含发布途中被强行结束留下的「结果待定」记录），同样需要人确认
    return None


def describe_previous(row: dict[str, Any]) -> str:
    """查重跳过时给用户看的说明。"""
    status = row.get("status")
    when = row.get("finished_at", "")
    if status in _DONE:
        return f"同一内容已于 {when} 发过（状态 {status}）；确认要再发请加 --force"
    return (f"同一内容已于 {when} 点过发布但结果未知，可能已经发出：先到平台后台确认；确认没发出、要再发请加 --force"
            f"（只重发这个平台：--to {row.get('platform')} --force）")


class AccountBusy(RuntimeError):
    """同一平台账号正被另一个 mpub 任务使用（例如后台还在发布）。"""


@contextmanager
def account_lock(settings: Settings, platform: str, account: str, wait: float = 5) -> Iterator[None]:
    settings.locks_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.locks_dir / f"{platform}-{account}.lock"))
    try:
        lock.acquire(timeout=wait)
    except Timeout as exc:
        raise AccountBusy(f"{platform}/{account} 正被另一个 mpub 任务使用（可能后台还在发布），请等它结束后再试") from exc
    try:
        yield
    finally:
        lock.release()
