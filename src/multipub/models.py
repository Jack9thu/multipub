"""跨模块共享的数据结构：内容类型、单平台发布视图、结果、错误。"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"


class Kind(str, enum.Enum):
    VIDEO = "video"  # 视频
    NOTE = "note"  # 图文：若干图片 + 一段文字（抖音图文 / 小红书笔记 / B站动态 / 知乎想法）
    ARTICLE = "article"  # 长文：Markdown 正文（知乎文章）


class Status(str, enum.Enum):
    OK = "ok"  # login / check 成功
    PUBLISHED = "published"  # 平台已公开，拿到了作品链接
    SUBMITTED = "submitted"  # 平台已接收（跳到成功页/返回成功），可能还在审核，没有链接
    SCHEDULED = "scheduled"  # 已设置定时发布
    DRAFT = "draft"  # 只保存了草稿
    DRY_RUN = "dry_run"  # 只做了本地校验
    SKIPPED = "skipped"  # 该平台不支持此内容类型 / 已发过
    NEEDS_ACTION = "needs_action"  # 需要人处理：登录过期、验证码、短信码
    FAILED = "failed"


OK_STATUSES = frozenset({Status.OK, Status.PUBLISHED, Status.SUBMITTED, Status.SCHEDULED,
                         Status.DRAFT, Status.DRY_RUN, Status.SKIPPED})


class ErrorCode(str, enum.Enum):
    LOGIN_REQUIRED = "login_required"
    VALIDATION = "validation"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    CAPTCHA = "captcha"  # 平台弹出人机验证
    SMS_CODE_REQUIRED = "sms_code_required"
    RISK_CONTROL = "risk_control"  # 412 / 40352 等风控拦截
    PLATFORM_ERROR = "platform_error"  # 平台返回了明确的失败
    UNKNOWN_OUTCOME = "unknown_outcome"  # 已点了发布但没拿到确定结果：先去后台确认，不要重发
    INTERNAL = "internal"


# ---------------------------------------------------------------- exceptions

class PublishError(Exception):
    """适配器内部抛出的、可映射为 Result 的错误。"""

    status: Status = Status.FAILED
    code: ErrorCode = ErrorCode.PLATFORM_ERROR
    retryable: bool = False

    def __init__(self, message: str, *, code: ErrorCode | None = None,
                 retryable: bool | None = None, evidence: list[str] | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.evidence = evidence or []


class NeedsAction(PublishError):
    """需要人介入（扫码登录、输入短信验证码、过人机验证）。"""

    status = Status.NEEDS_ACTION
    code = ErrorCode.LOGIN_REQUIRED


class LoginRequired(NeedsAction):
    code = ErrorCode.LOGIN_REQUIRED


class ValidationFailed(PublishError):
    code = ErrorCode.VALIDATION


class Unsupported(PublishError):
    status = Status.SKIPPED
    code = ErrorCode.UNSUPPORTED


# ---------------------------------------------------------------- validation

@dataclass
class Issue:
    level: str  # "error" | "warning"
    field: str
    message: str
    platform: str = ""

    def __str__(self) -> str:
        where = f"[{self.platform}] " if self.platform else ""
        return f"{where}{self.level}: {self.field}: {self.message}"


# ---------------------------------------------------------------- post view

@dataclass
class PlatformPost:
    """某一个平台实际要发的内容（已合并顶层字段与 platforms.<name> 覆盖项）。"""

    platform: str
    account: str
    kind: Kind
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    video: Path | None = None
    cover: Path | None = None
    images: list[Path] = field(default_factory=list)
    schedule: datetime | None = None  # 本地时间（naive）
    ai_generated: bool = False
    options: dict[str, Any] = field(default_factory=dict)  # 平台私有参数
    source: str = ""  # manifest 路径，便于记录

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account": self.account,
            "kind": self.kind.value,
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "video": str(self.video) if self.video else None,
            "cover": str(self.cover) if self.cover else None,
            "images": [str(p) for p in self.images],
            "schedule": self.schedule.strftime(SCHEDULE_FORMAT) if self.schedule else None,
            "ai_generated": self.ai_generated,
            "options": dict(self.options),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlatformPost":
        return cls(
            platform=data["platform"],
            account=data["account"],
            kind=Kind(data["kind"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            tags=list(data.get("tags") or []),
            video=Path(data["video"]) if data.get("video") else None,
            cover=Path(data["cover"]) if data.get("cover") else None,
            images=[Path(p) for p in data.get("images") or []],
            schedule=datetime.strptime(data["schedule"], SCHEDULE_FORMAT) if data.get("schedule") else None,
            ai_generated=bool(data.get("ai_generated")),
            options=dict(data.get("options") or {}),
            source=data.get("source") or "",
        )


# ---------------------------------------------------------------- result

@dataclass
class Result:
    platform: str
    account: str
    action: str  # login | check | publish
    status: Status
    kind: str = ""
    message: str = ""
    url: str | None = None
    item_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    # 一旦为 True，说明"发布"按钮/接口已经触发过：失败时不要自动重发，先去平台后台确认
    submit_started: bool = False
    evidence: list[str] = field(default_factory=list)  # 截图等文件路径
    started_at: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["ok"] = self.ok
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Result":
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        data["status"] = Status(data["status"])
        return cls(**data)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
