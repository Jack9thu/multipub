"""读取 post.yaml（一条内容的发布清单），生成各平台的 PlatformPost。

示例见 examples/ 与 docs/manifest.md。顶层字段是所有平台共用的默认值，
platforms.<平台> 下的同名字段覆盖默认值，其余键作为平台私有参数（options）。
"""

from __future__ import annotations

import re
import sys
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
        if item is None:  # 列表里的空项（如 "- " 或被当成注释的 "- #话题"）
            continue
        tag = str(item).strip().lstrip("#").rstrip("#").strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def _flag(value: Any) -> bool:
    """ai_generated: "false" / "否" 这类加了引号的写法不能当成 true（会替用户做 AI 声明）。"""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "on", "1", "是")
    return bool(value)


def parse_schedule(value: Any) -> datetime | None:
    if value in (None, "", False):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:  # 不加引号、带时区的写法：换算成本机时间
            value = value.astimezone()
        return value.replace(tzinfo=None, second=0, microsecond=0)
    text = str(value).strip().replace("T", " ")
    for fmt in (SCHEDULE_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(second=0)
        except ValueError:
            continue
    raise ManifestError(f"schedule 格式应为 'YYYY-MM-DD HH:MM'（本地时间），收到: {value!r}")


# 这些字段的值一律按原文当字符串：YAML 1.1 会把不加引号的 12:30 读成 750、1.10 读成 1.1、
# no/on 读成布尔、007 读成 7，直接发出去就是错的标题/正文/账号名
TEXT_KEYS = frozenset({
    "title", "body", "body_file", "tags", "video", "cover", "images", "account",
    # 平台私有的文字参数
    "dynamic", "source", "collection", "product_title", "repost_source", "bgm",
    "thumbnail_portrait", "thumbnail_landscape",
})


class _Loader(yaml.SafeLoader):
    pass


def _as_text(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode) and node.style is None and node.tag != "tag:yaml.org,2002:str":
        if node.tag == "tag:yaml.org,2002:null":
            return None
        return node.value
    if isinstance(node, yaml.SequenceNode):
        return [_as_text(loader, item) for item in node.value]
    return loader.construct_object(node, deep=True)


def _construct_map(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    data: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            hash(key)
        except TypeError as exc:
            raise ManifestError(f"清单里有无法使用的键: {key!r}") from exc
        if isinstance(key, str) and key in TEXT_KEYS:
            data[key] = _as_text(loader, value_node)
        else:
            data[key] = loader.construct_object(value_node, deep=True)
    return data


_Loader.add_constructor("tag:yaml.org,2002:map", _construct_map)

_KEY_LINE = re.compile(r"^(?P<key>[^\s#'\"\-][^:#]*?)\s*:(?:\s+(?P<value>.*))?$")
# 行内注释：# 前面是空白（或在值开头），后面紧跟文字，例如「#科技」—— 多半是用户想写的话题
_HASHTAG_COMMENT = re.compile(r"(?:^|\s)#[^\s#]")
_HASHTAG_KEYS = ("title", "body", "tags")


def _hashtag_comments(text: str) -> list[str]:
    """找出被 YAML 当成注释吞掉的 #话题（title/body/tags 的值里不加引号的 #xxx）。"""
    found: list[str] = []
    key, key_indent, block_indent = "", -1, None
    for no, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if block_indent is not None:
            if not stripped or indent > block_indent:
                continue  # | / > 块里的正文，# 是普通字符
            block_indent = None
        if not stripped or stripped.startswith("#"):
            continue
        value = None
        m = _KEY_LINE.match(stripped)
        if m:
            key, key_indent = m.group("key").strip(), indent
            value = (m.group("value") or "").strip()
            if value[:1] in ("|", ">"):
                block_indent = indent
                continue
        elif stripped.startswith("- ") and key == "tags" and indent >= key_indent:
            value = stripped[2:].strip()
        if not value or key not in _HASHTAG_KEYS or value[:1] in ("'", '"', "[", "{"):
            continue
        hit = _HASHTAG_COMMENT.search(value)
        if hit:
            found.append(f"第 {no} 行 {key} 里的「{value[hit.start():].strip()}」")
    return found


def read_text_file(path: Path) -> str:
    """读 Windows 上各种编辑器存出来的文本：UTF-8（带不带 BOM）、PowerShell 5.1 重定向写出的 UTF-16、
    记事本「ANSI」（中文系统是 GBK）。"""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{path} 的编码无法识别，请另存为 UTF-8：{exc}") from exc


def _yaml_error(exc: yaml.YAMLError) -> str:
    msg = f"YAML 解析失败: {exc}"
    if "escape" in str(exc):
        msg += ("\n提示：双引号里的反斜杠是转义符。Windows 路径请用单引号（'D:\\素材\\第12期.mp4'）"
                "或正斜杠（D:/素材/第12期.mp4），或把文件放在 post.yaml 同目录写相对路径")
    return msg


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
            body = read_text_file(bf)

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
            ai_generated=_flag(pick("ai_generated", False)),
            options=options,
            source=str(self.path),
        )


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        p = p / "post.yaml"
    if not p.is_file():
        raise ManifestError(f"找不到清单文件: {p}")
    text = read_text_file(p)
    try:
        data = yaml.load(text, Loader=_Loader) or {}  # noqa: S506 - SafeLoader 子类
    except yaml.YAMLError as exc:
        raise ManifestError(_yaml_error(exc)) from exc
    if not isinstance(data, dict):
        raise ManifestError("清单顶层必须是键值对")
    lost = _hashtag_comments(text)
    if lost:
        raise ManifestError("、".join(lost) + " 会被 YAML 当成注释而丢掉（# 前有空格就是注释）。"
                            "话题不用写 #：tags: [科技, 教程]；正文或标题里要带 #话题，请给整个值加引号，"
                            "如 body: \"今天去徒步了 #徒步\"，或用 body: | 多行写法")

    kind = str(data.get("kind", "")).strip().lower()
    if kind not in {k.value for k in Kind}:
        raise ManifestError("kind 必须是 video / note / article 之一")
    data["kind"] = kind

    platforms_raw = data.get("platforms") or {}
    if isinstance(platforms_raw, str):  # platforms: b站, 抖音
        platforms_raw = [x for x in re.split(r"[,，;；\s]+", platforms_raw) if x]
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


def _missing(path: Path) -> str:
    msg = f"文件不存在: {path}"
    text = str(path)
    if any(c in text for c in "\n\t\r\b\f\v\a\x00\x1b"):
        msg += "（路径里有被转义的字符：Windows 路径在 YAML 里请用单引号或正斜杠，双引号里的 \\n、\\t 会变成换行、制表符）"
    elif "\\" in text and sys.platform != "win32":
        msg += "（反斜杠路径只在 Windows 上有效，这里请用正斜杠 /）"
    return msg


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
            err("video", _missing(post.video))
    if post.kind is Kind.NOTE:
        if not post.images:
            err("images", "图文内容至少需要 1 张图片")
        for img in post.images:
            if not img.is_file():
                err("images", _missing(img))
    if post.kind is Kind.ARTICLE and not post.body.strip():
        err("body", "文章内容需要 body 或 body_file")
    if post.cover is not None and not post.cover.is_file():
        err("cover", _missing(post.cover))
    if post.schedule is not None and post.schedule <= datetime.now():
        err("schedule", "定时发布时间必须晚于当前时间")
    return issues
