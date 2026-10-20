"""读取 post.yaml（一条内容的发布清单），生成各平台的 PlatformPost。

示例见 examples/ 与 docs/manifest.md。顶层字段是所有平台共用的默认值，
platforms.<平台> 下的同名字段覆盖默认值，其余键作为平台私有参数（options）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .models import SCHEDULE_FORMAT, Issue, Kind, PlatformPost

PLATFORMS = ("bilibili", "douyin", "xiaohongshu", "zhihu")

ALIASES = {
    "bilibili": "bilibili", "bili": "bilibili", "b站": "bilibili", "b": "bilibili",
    "douyin": "douyin", "dy": "douyin", "抖音": "douyin",
    "xiaohongshu": "xiaohongshu", "xhs": "xiaohongshu", "小红书": "xiaohongshu", "rednote": "xiaohongshu",
    "zhihu": "zhihu", "zh": "zhihu", "知乎": "zhihu",
}

# 可以在 platforms.<平台> 下覆盖的通用字段
COMMON_FIELDS = ("title", "body", "body_file", "tags", "video", "cover", "images",
                 "schedule", "ai_generated", "account")

ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_ACCOUNT = "default"


class ManifestError(ValueError):
    pass


def normalize_platform(name: str) -> str:
    key = name.strip().lower()
    if key not in ALIASES:
        from .platforms import available  # 测试用的 fake 平台等
        if key in available():
            return key
        raise ManifestError(f"未知平台: {name}（可选: {', '.join(PLATFORMS)}）")
    return ALIASES[key]


def parse_platform_list(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else re.split(r"[,，\s]+", value)
    out: list[str] = []
    for item in items:
        if item and item.strip():
            p = normalize_platform(item)
            if p not in out:
                out.append(p)
    return out


def parse_tags(value: Any) -> list[str]:
    """接受列表或 "a,b，c #d" 字符串；去掉开头的 #，去重保序。"""
    if value is None:
        return []
    raw = value if isinstance(value, list) else re.split(r"[,，;；\s]+", str(value))
    out: list[str] = []
    for item in raw:
        tag = str(item).strip().lstrip("#").rstrip("#").strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def parse_schedule(value: Any) -> datetime | None:
    if value in (None, "", False):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    text = str(value).strip().replace("T", " ")
    for fmt in (SCHEDULE_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(second=0)
        except ValueError:
            continue
    raise ManifestError(f"schedule 格式应为 'YYYY-MM-DD HH:MM'（本地时间），收到: {value!r}")


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any]
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def kind(self) -> Kind:
        return Kind(self.data["kind"])

    def targets(self, override: list[str] | None = None) -> list[str]:
        if override:
            return override
        return [p for p, cfg in self.platforms.items() if cfg.get("enabled", True) is not False]

    def _resolve(self, value: Any) -> Path | None:
        if value in (None, ""):
            return None
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def for_platform(self, platform: str, account_override: str | None = None) -> PlatformPost:
        platform = normalize_platform(platform)
        top = self.data
        over = dict(self.platforms.get(platform, {}))
        over.pop("enabled", None)

        def pick(key: str, default: Any = None) -> Any:
            if key in over and over[key] is not None:
                return over[key]
            return top.get(key, default)

        body = pick("body", "") or ""
        body_file = over.get("body_file") if over.get("body_file") else (None if "body" in over else top.get("body_file"))
        if body_file:
            bf = self._resolve(body_file)
            if bf is None or not bf.is_file():
                raise ManifestError(f"[{platform}] body_file 不存在: {body_file}")
            body = bf.read_text(encoding="utf-8-sig")

        account = account_override or pick("account") or DEFAULT_ACCOUNT
        options = {k: v for k, v in over.items() if k not in COMMON_FIELDS}

        return PlatformPost(
            platform=platform,
            account=str(account),
            kind=self.kind,
            title=str(pick("title", "") or "").strip(),
            body=str(body).strip("\n"),
            tags=parse_tags(pick("tags")),
            video=self._resolve(pick("video")),
            cover=self._resolve(pick("cover")),
            images=[p for p in (self._resolve(i) for i in (pick("images") or [])) if p is not None],
            schedule=parse_schedule(pick("schedule")),
            ai_generated=bool(pick("ai_generated", False)),
            options=options,
            source=str(self.path),
        )


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        p = p / "post.yaml"
    if not p.is_file():
        raise ManifestError(f"找不到清单文件: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("清单顶层必须是键值对")

    kind = str(data.get("kind", "")).strip().lower()
    if kind not in {k.value for k in Kind}:
        raise ManifestError("kind 必须是 video / note / article 之一")
    data["kind"] = kind

    platforms_raw = data.get("platforms") or {}
    if isinstance(platforms_raw, list):
        platforms_raw = {name: {} for name in platforms_raw}
    if not isinstance(platforms_raw, dict):
        raise ManifestError("platforms 必须是映射（平台名: 覆盖项）或平台名列表")
    platforms: dict[str, dict[str, Any]] = {}
    for name, cfg in platforms_raw.items():
        key = normalize_platform(str(name))
        if cfg is None or cfg is True:
            cfg = {}
        elif cfg is False:
            cfg = {"enabled": False}
        if not isinstance(cfg, dict):
            raise ManifestError(f"platforms.{name} 必须是映射")
        platforms[key] = cfg

    return Manifest(path=p, data=data, platforms=platforms)


def common_issues(post: PlatformPost) -> list[Issue]:
    """与平台无关的基础检查；平台限制由各适配器的 validate() 负责。"""
    issues: list[Issue] = []

    def err(f: str, m: str) -> None:
        issues.append(Issue("error", f, m, post.platform))

    if not ACCOUNT_RE.match(post.account):
        err("account", "账号名只能包含字母、数字、下划线、连字符（1-64 位）")
    if post.kind is Kind.VIDEO:
        if post.video is None:
            err("video", "视频内容必须提供 video 文件")
        elif not post.video.is_file():
            err("video", f"文件不存在: {post.video}")
    if post.kind is Kind.NOTE:
        if not post.images:
            err("images", "图文内容至少需要 1 张图片")
        for img in post.images:
            if not img.is_file():
                err("images", f"文件不存在: {img}")
    if post.kind is Kind.ARTICLE and not post.body.strip():
        err("body", "文章内容需要 body 或 body_file")
    if post.cover is not None and not post.cover.is_file():
        err("cover", f"文件不存在: {post.cover}")
    if post.schedule is not None and post.schedule <= datetime.now():
        err("schedule", "定时发布时间必须晚于当前时间")
    return issues
