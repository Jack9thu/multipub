"""平台适配器接口。

每个平台一个 PlatformAdapter 子类，运行在独立的 worker 子进程里（见 multipub.worker），
因此适配器可以放心使用 patchright / asyncio / 全局日志，不会污染主进程。

约定：
- validate() 是纯本地检查（不联网、不开浏览器），主进程在 publish 之前和 --dry-run 时调用。
- login() / check() / publish() 返回 Result；遇到需要人处理的情况抛 NeedsAction，
  遇到平台明确失败抛 PublishError，worker 会统一转换成 Result。
- 一旦即将点击"发布"按钮或调用发布接口，先调用 ctx.mark_submit_started()。
  之后的任何失败都不会被自动重试（避免重复发布）。
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar

from ..config import Settings
from ..models import Issue, Kind, PlatformPost, Result, Status, now_iso

EmitFn = Callable[[dict[str, Any]], None]


def stdout_emit(event: dict[str, Any]) -> None:
    """worker 的事件通道：stdout 上一行一个 JSON。日志一律走 stderr。"""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


@dataclass
class JobContext:
    settings: Settings
    platform: str
    account: str
    action: str
    headless: bool
    run_dir: Path
    emit: EmitFn = stdout_emit
    submit_started: bool = False
    evidence: list[str] = field(default_factory=list)

    @property
    def account_file(self) -> Path:
        return self.settings.account_file(self.platform, self.account)

    def event(self, name: str, **data: Any) -> None:
        self.emit({"event": name, "platform": self.platform, "account": self.account, **data})

    def log(self, message: str, level: str = "info") -> None:
        self.event("log", level=level, message=message)

    def qrcode(self, path: Path | str, note: str = "") -> None:
        """登录二维码已生成：CLI 会打印路径，Claude Code skill 会把图片展示给用户。"""
        self.event("qrcode", path=str(path), note=note)

    def needs_input(self, kind: str, message: str, path: Path | str | None = None) -> None:
        """需要人提供输入（如抖音短信验证码）。path 是需要写入的文件。"""
        self.event("needs_input", kind=kind, message=message, path=str(path) if path else None)

    def mark_submit_started(self) -> None:
        if not self.submit_started:
            self.submit_started = True
            self.event("submit_started")

    def evidence_path(self, name: str) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        p = self.run_dir / name
        self.evidence.append(str(p))
        return p


class PlatformAdapter(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str]
    kinds: ClassVar[frozenset[Kind]]
    # True 表示登录必须在真实终端里交互完成（如 biliup 扫码），不能经由 worker 子进程
    interactive_login: ClassVar[bool] = False

    def __init__(self, ctx: JobContext):
        self.ctx = ctx
        self.settings = ctx.settings

    # ------------------------------------------------------------ helpers
    @property
    def account_file(self) -> Path:
        return self.ctx.account_file

    def result(self, status: Status, *, kind: Kind | str = "", **kw: Any) -> Result:
        return Result(
            platform=self.name,
            account=self.ctx.account,
            action=self.ctx.action,
            status=status,
            kind=kind.value if isinstance(kind, Kind) else kind,
            submit_started=self.ctx.submit_started,
            evidence=list(self.ctx.evidence),
            finished_at=now_iso(),
            **kw,
        )

    # ------------------------------------------------------------ interface
    @classmethod
    def validate(cls, post: PlatformPost) -> list[Issue]:
        """平台限制检查（标题长度、图片数量、标签数量、定时范围……）。不联网。"""
        return []

    @classmethod
    def run_interactive_login(cls, settings: Settings, account: str) -> int:
        """interactive_login=True 的平台在主进程里直接跑交互式登录，返回退出码。"""
        raise NotImplementedError

    @classmethod
    def doctor(cls, settings: Settings) -> list[dict[str, Any]]:
        """环境自检项：[{"name": ..., "ok": bool, "detail": ...}]。不联网、不开浏览器。"""
        return []

    @classmethod
    def setup(cls, settings: Settings) -> list[dict[str, Any]]:
        """安装/下载平台需要的外部工具（如 biliup），返回与 doctor 相同格式的结果。"""
        return []

    @abstractmethod
    async def login(self) -> Result: ...

    @abstractmethod
    async def check(self) -> Result: ...

    @abstractmethod
    async def publish(self, post: PlatformPost) -> Result: ...
