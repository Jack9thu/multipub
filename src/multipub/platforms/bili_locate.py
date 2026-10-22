"""biliup 从哪来：由用户选择。

三种方式（记在 <数据目录>/tools/biliup/selected.json，不改用户的 config.toml）：

- path     用户直接给出 biliup 的路径          mpub biliup use <路径>
- auto     在本机查找，自动挑兼容的（或从列表选） mpub biliup find / mpub biliup use --auto / use <编号>
- download 用 mpub setup 下载的固定版本          mpub biliup use --download

优先级：config.toml 的 [platforms.bilibili] biliup_path（显式配置，最高）→ 上面记录的选择 →
都没有时：已经下载过固定版本就用它，否则视为「还没选」，由 mpub setup / Claude 询问用户。

兼容性不只看版本号：运行 `biliup --help` 和 `biliup upload --help`（纯本地，不联网），
确认 multipub 会用到的参数都在。绝不能不带参数运行 biliup（那会启动 Web 服务）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from . import bili_biliup as bb

MODES = ("path", "auto", "download")
# 每次上传都会用到的参数；缺任何一个都不能用
REQUIRED_UPLOAD_FLAGS = ("--title", "--desc", "--tid", "--tag", "--copyright")
HELP_TIMEOUT = 30.0


# ------------------------------------------------------------------ 选择记录

def state_file(settings: Settings) -> Path:
    return settings.tools_dir / "biliup" / "selected.json"


def read_choice(settings: Settings) -> dict[str, Any] | None:
    try:
        data = json.loads(state_file(settings).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("mode") not in MODES:
        return None
    return data


def write_choice(settings: Settings, mode: str, path: Path | None = None) -> dict[str, Any]:
    assert mode in MODES
    data: dict[str, Any] = {"mode": mode, "chosen_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if path is not None:
        data["path"] = str(path)
    f = state_file(settings)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    return data


def clear_choice(settings: Settings) -> None:
    state_file(settings).unlink(missing_ok=True)


# ------------------------------------------------------------------ 兼容性

@dataclass
class Capability:
    ok: bool
    version: str | None = None
    reason: str = ""  # 不 ok 时的原因
    global_flags: list[str] = field(default_factory=list)
    upload_flags: list[str] = field(default_factory=list)
    has_renew: bool = False


_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*|-[a-zA-Z])\b")
_CAP_CACHE: dict[tuple[str, int, int], Capability] = {}


def _help(exe: Path, args: list[str]) -> str:
    with bb.quiet_error_mode():
        proc = subprocess.run([str(exe), *args], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=HELP_TIMEOUT, env=bb.biliup_env(), **bb._no_window_kwargs())
    text = proc.stdout.decode("utf-8", "replace") + "\n" + proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 退出码 {proc.returncode}: {bb.strip_ansi(text).strip()[-200:]}")
    return bb.strip_ansi(text)


def capability(exe: Path) -> Capability:
    """检查一个 biliup 能不能用。能用的结果按路径 + 大小 + 修改时间缓存；不能用的每次重查（用户可能刚装好运行库）。"""
    try:
        st = exe.stat()
    except OSError:
        return Capability(False, reason=f"文件不存在: {exe}")
    if not exe.is_file():
        return Capability(False, reason=f"不是文件: {exe}")
    key = (str(exe), st.st_size, st.st_mtime_ns)
    if key in _CAP_CACHE:
        return _CAP_CACHE[key]
    probe = bb.probe_cached(exe)
    if not probe.ok:
        cap = Capability(False, reason=probe.detail)
    else:
        try:
            top = _help(exe, ["--help"])
            upload = _help(exe, ["upload", "--help"])
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            cap = Capability(False, version=probe.version, reason=f"无法读取帮助信息：{exc}")
        else:
            gflags = sorted(set(_FLAG_RE.findall(top)))
            uflags = sorted(set(_FLAG_RE.findall(upload)))
            missing = [f for f in REQUIRED_UPLOAD_FLAGS if f not in uflags]
            if "-u" not in gflags and "--user-cookie" not in gflags:
                missing.insert(0, "-u/--user-cookie")
            cap = Capability(not missing, version=probe.version,
                             reason=f"缺少参数 {', '.join(missing)}（版本可能太旧）" if missing else "",
                             global_flags=gflags, upload_flags=uflags,
                             has_renew=bool(re.search(r"^\s*renew\b", top, re.M)))
    if cap.ok:
        _CAP_CACHE[key] = cap
    return cap


def forget_checks() -> None:
    """doctor / setup 要「现在」的结论：清掉本进程里缓存的检查结果。"""
    _CAP_CACHE.clear()
    bb._PROBE_OK.clear()


def unsupported_flags(exe: Path, args: list[str]) -> list[str]:
    """build_upload_args 生成的参数里，这个 biliup 不认识的那些（为空表示都支持）。"""
    cap = capability(exe)
    if not cap.ok:
        return []
    try:
        i = args.index("upload")
    except ValueError:
        return []
    known_global = set(cap.global_flags)
    known_upload = set(cap.upload_flags)
    out: list[str] = []
    for a in args[1:i]:
        if a.startswith("-") and a.split("=", 1)[0] not in known_global:
            out.append(a.split("=", 1)[0])
    for a in args[i + 1:]:
        if a == "--":
            break
        if a.startswith("-") and a.split("=", 1)[0] not in known_upload:
            out.append(a.split("=", 1)[0])
    return out


def supports_rust_log(exe: Path) -> bool:
    cap = capability(exe)
    return not cap.ok or "--rust-log" in cap.global_flags


# ------------------------------------------------------------------ 本机查找

@dataclass
class Candidate:
    path: Path
    source: str
    version: str | None = None
    compatible: bool = False
    reason: str = ""
    cookies: list[str] = field(default_factory=list)  # 旁边找到的登录文件（只提示，不自动复制）

    def to_dict(self, index: int | None = None) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        if index is not None:
            d["index"] = index
        return d


def _names() -> tuple[str, ...]:
    return ("biliup.exe", "biliupR.exe") if sys.platform == "win32" else ("biliup", "biliupR")


def _sau_dirs() -> list[Path]:
    root = Path.home() / ".social-auto-upload" / "tools" / "biliup"
    return [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []


def _raw_candidates(settings: Settings) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for name in ("biliup", "biliupR"):
        hit = shutil.which(name)
        if hit:
            found.append((Path(hit), "PATH"))
    for d in _sau_dirs():
        found += [(d / n, "social-auto-upload") for n in _names()]
    tools = settings.tools_dir / "biliup"
    if tools.is_dir():
        for d in sorted(tools.iterdir()):
            if d.is_dir():
                found += [(d / n, f"multipub 下载（{d.name}）") for n in _names()]
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        found += [(downloads / n, "下载文件夹") for n in _names()]
        try:
            subdirs = [p for p in downloads.iterdir() if p.is_dir() and p.name.lower().startswith("biliup")]
        except OSError:
            subdirs = []
        for d in subdirs:
            found += [(d / n, "下载文件夹") for n in _names()]
    found += [(Path.cwd() / n, "当前目录") for n in _names()]
    return found


def _nearby_cookies(exe: Path) -> list[str]:
    """biliup 默认把登录信息写在运行目录的 cookies.json；social-auto-upload 的在它仓库的 cookies/ 下，位置不固定。"""
    p = exe.parent / "cookies.json"
    return [str(p)] if p.is_file() else []


def discover(settings: Settings) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for path, source in _raw_candidates(settings):
        if not path.is_file():
            continue
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        cap = capability(path)
        out.append(Candidate(path=path, source=source, version=cap.version, compatible=cap.ok,
                             reason=cap.reason, cookies=_nearby_cookies(path)))
    return out


def _version_key(v: str | None) -> tuple[int, ...]:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def best(settings: Settings, candidates: list[Candidate]) -> Candidate | None:
    """自动挑选：兼容的里面优先固定版本，其次版本最新。"""
    ok = [c for c in candidates if c.compatible]
    if not ok:
        return None
    return sorted(ok, key=lambda c: (c.version == settings.biliup_version, _version_key(c.version)),
                  reverse=True)[0]


# ------------------------------------------------------------------ 解析当前该用哪个

@dataclass
class Resolution:
    path: Path | None
    mode: str  # configured | path | auto | download | default | none
    detail: str  # 给人看的说明
    problem: str = ""  # 非空表示现在用不了
    version: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.problem

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "mode": self.mode, "path": str(self.path) if self.path else None,
                "version": self.version, "detail": self.detail, "problem": self.problem}


MODE_ZH = {"configured": "config.toml 的 biliup_path", "path": "你指定的路径", "auto": "自动查找",
           "download": "下载的固定版本", "default": "已下载的固定版本", "none": "还没选"}


def _check(path: Path, mode: str, label: str) -> Resolution:
    cap = capability(path)
    if not cap.ok:
        return Resolution(path, mode, f"{label}: {path}", problem=cap.reason, version=cap.version)
    return Resolution(path, mode, f"{label}: {path}（{cap.version or '版本未知'}）", version=cap.version)


def resolve(settings: Settings) -> Resolution:
    if settings.biliup_path:
        p = Path(settings.biliup_path).expanduser()
        if not p.is_file():
            return Resolution(None, "configured", f"config.toml biliup_path={p}", problem="文件不存在")
        return _check(p, "configured", "config.toml biliup_path")
    choice = read_choice(settings)
    managed = bb.managed_path(settings)
    if choice and choice["mode"] == "path":
        p = Path(str(choice.get("path", ""))).expanduser()
        if not p.is_file():
            return Resolution(None, "path", f"你指定的 {p}",
                              problem="文件不存在了：用 mpub biliup use <新路径> 重新指定，或 mpub biliup find 查找")
        return _check(p, "path", "你指定的路径")
    if choice and choice["mode"] == "auto":
        pick = best(settings, discover(settings))
        if pick is None:
            return Resolution(None, "auto", "自动查找", problem="本机没找到兼容的 biliup：运行 mpub biliup find 查看原因，"
                                                            "或 mpub biliup use <路径> / mpub biliup use --download")
        return Resolution(pick.path, "auto", f"自动查找（{pick.source}）: {pick.path}（{pick.version or '版本未知'}）",
                          version=pick.version)
    if choice and choice["mode"] == "download":
        if not managed.is_file():
            return Resolution(None, "download", f"下载的固定版本 {settings.biliup_version}",
                              problem="还没下载：运行 mpub setup")
        return _check(managed, "download", f"下载的固定版本 {settings.biliup_version}")
    if managed.is_file():
        return _check(managed, "default", f"已下载的固定版本 {settings.biliup_version}")
    return Resolution(None, "none", "还没选 biliup 来源",
                      problem="还没选 biliup 来源：mpub biliup find 看本机已有的，"
                              "然后 mpub biliup use <路径或编号> / --auto / --download（或运行 mpub setup 按提示选）")


def needs_choice(settings: Settings) -> bool:
    """mpub setup 是否需要先问用户：没配置、没选过、也没下载过固定版本。"""
    return resolve(settings).mode == "none"


def use(settings: Settings, target: str | None = None, *, mode: str | None = None,
        force: bool = False) -> tuple[bool, str, Resolution | None]:
    """记录用户的选择。target 是路径、目录或 `mpub biliup find` 列表里的编号。返回 (成功, 说明, 解析结果)。"""
    if mode in ("auto", "download"):
        write_choice(settings, mode)
        res = resolve(settings)
        note = "" if not settings.biliup_path else "（注意：config.toml 里的 biliup_path 优先级更高，当前仍用它）"
        if mode == "download" and not bb.managed_path(settings).is_file():
            return True, f"已选择：下载固定版本 {settings.biliup_version}，运行 mpub setup 下载{note}", res
        return res.ok, (f"已选择：{MODE_ZH[mode]}；当前使用 {res.detail}{note}" if res.ok
                        else f"已选择：{MODE_ZH[mode]}；但{res.problem}"), res
    if not target:
        return False, "缺少路径或编号", None
    path: Path | None = None
    if target.strip().isdigit():
        cands = discover(settings)
        n = int(target)
        if not 1 <= n <= len(cands):
            return False, f"编号 {n} 不在列表里（先运行 mpub biliup find）", None
        path = cands[n - 1].path
    else:
        p = Path(target.strip().strip('"').strip("'")).expanduser()
        if p.is_dir():
            hits = [p / n for n in _names() if (p / n).is_file()]
            if not hits:
                return False, f"目录里没有 biliup：{p}", None
            p = hits[0]
        path = p
    if not path.is_file():
        return False, f"文件不存在: {path}", None
    cap = capability(path)
    if not cap.ok and not force:
        return False, f"{path} 不能用：{cap.reason}（确定要用请加 --force）", None
    write_choice(settings, "path", path.resolve())
    res = resolve(settings)
    note = "（注意：config.toml 里的 biliup_path 优先级更高，当前仍用它）" if settings.biliup_path else ""
    warn = "" if cap.ok else f"；警告：{cap.reason}"
    return True, f"已选择：{path}（{cap.version or '版本未知'}）{warn}{note}", res
