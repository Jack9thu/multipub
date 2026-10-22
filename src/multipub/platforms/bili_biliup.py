"""biliup 命令行（B站视频上传引擎）的安装、调用与输出解析。

- 版本固定为 settings.biliup_version（默认 v1.3.1），装在 <tools_dir>/biliup/<版本>/；
  settings.biliup_path 指定时直接用它，不下载。
- 只有 `mpub setup` 会下载；已装好的版本永不自动更新。
- 下载后计算压缩包 sha256：内置了 v1.3.1 各平台压缩包的校验值，不一致直接拒绝；
  其他版本首次下载时记录（trust-on-first-use），写在二进制旁边的 install.json / *.sha256。
- 命令行参数按 biliup v1.3.1 源码（crates/biliup-cli/src/cli.rs、
  crates/biliup/src/uploader/bilibili.rs 的 Studio）核对过；全局参数必须写在子命令前面。
- Windows 版 biliup.exe 静态导入 VCRUNTIME140.dll（压缩包里没带），缺 VC++ 运行库时进程根本起不来。
  安装后、doctor、每次上传视频前都用 `biliup --version`（probe_binary）试启动一次。

本模块只依赖标准库（httpx 在下载时才导入），主进程可以导入。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import Settings

GITHUB_DOWNLOAD = "https://github.com/biliup/biliup/releases/download/{version}/{asset}"

# 2026-10-20 两次独立下载 github.com/biliup/biliup/releases/download/v1.3.1/ 核对过
KNOWN_SHA256: dict[str, str] = {
    "biliupR-v1.3.1-x86_64-windows.zip": "b177488823204e32db1fdb2a81c6bfcc4ffc58b2d47b14831ead4f83adc6401f",
    "biliupR-v1.3.1-x86_64-linux.tar.xz": "e55cdefc66606fb326c738be45e5740b575c21e9ad9ed7c12f83897c8dd683d3",
    "biliupR-v1.3.1-x86_64-linux-musl.tar.xz": "5ca2146a97061a478eb537569dea7fb84dc8bbcba77c32da6fa7d46409d8ae45",
    "biliupR-v1.3.1-aarch64-linux.tar.xz": "b2526e8d7ea2d4689e77226b3fcbbff9c3506a6aa7e5d00baeddd87977ce72c3",
    "biliupR-v1.3.1-arm-linux.tar.xz": "b027127a71f9d03c57a6543ffc8f521b5d78239a39fa90e4e0e84a852d7194d9",
    "biliupR-v1.3.1-x86_64-macos.tar.xz": "bd46266cdf8b7fdd47a4c9a933f7e39c66edc1f898acb99de8fa66fc1457163d",
    "biliupR-v1.3.1-aarch64-macos.tar.xz": "443b5e07f4b6eead71a6fad5a0b103e6959e23a10baafe343b5a40f36a97d594",
}

# (系统, 架构) -> 发布包后缀。Windows ARM 没有原生包，用 x86_64 版（系统自带转译）
ASSET_SUFFIXES: dict[tuple[str, str], str] = {
    ("windows", "x86_64"): "x86_64-windows.zip",
    ("windows", "aarch64"): "x86_64-windows.zip",
    ("linux", "x86_64"): "x86_64-linux.tar.xz",
    ("linux", "aarch64"): "aarch64-linux.tar.xz",
    ("linux", "arm"): "arm-linux.tar.xz",
    ("macos", "x86_64"): "x86_64-macos.tar.xz",
    ("macos", "aarch64"): "aarch64-macos.tar.xz",
}

SUBMIT_CHOICES = ("app", "web", "b-cut-android")
# 清单里的开关（必须是 true/false）-> biliup 参数
VIDEO_FLAGS: dict[str, str] = {
    "no_reprint": "--no-reprint=1",
    "is_only_self": "--is-only-self=1",
    "selection_reply": "--up-selection-reply",
    "close_reply": "--up-close-reply",
    "close_danmu": "--up-close-danmu",
}
# biliup Studio 注明「仅提交接口为app时可用」
APP_ONLY_FLAGS = ("selection_reply", "close_reply", "close_danmu")
LINE_CHOICES = ("bldsa", "cnbldsa", "andsa", "atdsa", "bda2", "cnbd", "anbd", "atbd",
                "tx", "cntx", "antx", "attx", "txa", "alia", "estx", "akbd")


class BiliupError(RuntimeError):
    pass


# ------------------------------------------------------------------ 平台与路径

def normalize_platform(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    s = (system or _platform.system()).strip().lower()
    s = {"darwin": "macos", "win32": "windows", "cygwin": "windows"}.get(s, s)
    m = (machine or _platform.machine()).strip().lower()
    m = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64",
         "armv7l": "arm", "armv6l": "arm", "armhf": "arm"}.get(m, m)
    return s, m


def asset_name(version: str, system: str | None = None, machine: str | None = None) -> str:
    key = normalize_platform(system, machine)
    try:
        suffix = ASSET_SUFFIXES[key]
    except KeyError as exc:
        raise BiliupError(f"biliup 没有 {key[0]}-{key[1]} 的预编译包，请自行安装后在 config.toml 里设置 "
                          "[platforms.bilibili] biliup_path") from exc
    return f"biliupR-{version}-{suffix}"


def download_url(version: str, asset: str, mirror: str = "") -> str:
    url = GITHUB_DOWNLOAD.format(version=version, asset=asset)
    if mirror:
        url = mirror if mirror.endswith("/") else mirror + "/"
        url += GITHUB_DOWNLOAD.format(version=version, asset=asset)
    return url


def exe_name(system: str | None = None) -> str:
    return "biliup.exe" if normalize_platform(system)[0] == "windows" else "biliup"


def install_dir(settings: Settings) -> Path:
    return settings.tools_dir / "biliup" / settings.biliup_version


def managed_path(settings: Settings) -> Path:
    return install_dir(settings) / exe_name()


def find_biliup(settings: Settings) -> Path | None:
    if settings.biliup_path:
        p = Path(settings.biliup_path).expanduser()
        return p if p.is_file() else None
    p = managed_path(settings)
    return p if p.is_file() else None


def read_install_info(settings: Settings) -> dict[str, Any] | None:
    f = install_dir(settings) / "install.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 能否启动（--version 试运行）

VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VCRUNTIME_HINT = f"缺少 Microsoft Visual C++ 运行库（VCRUNTIME140.dll），请安装 {VCREDIST_URL}"
# Windows 加载器失败时的进程退出码（NTSTATUS）。Python 给的是无符号值，也兼容有符号写法
STATUS_DLL_NOT_FOUND = 0xC0000135  # 3221225781 / -1073741515
_LOADER_FAILURES = {
    0xC000007B: "STATUS_INVALID_IMAGE_FORMAT",  # 常见于找到了 32 位的 DLL
    0xC0000138: "STATUS_ORDINAL_NOT_FOUND",
    0xC0000139: "STATUS_ENTRYPOINT_NOT_FOUND",  # 运行库太旧
}
PROBE_TIMEOUT = 30.0  # 正常不到 1 秒；首次运行可能被杀毒软件扫描
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002


def startup_failure_hint(returncode: int | None) -> str | None:
    """退出码表示进程根本没启动起来（Windows 加载 DLL 失败）时，返回原因和处理办法；否则 None。"""
    if returncode is None:
        return None
    code = returncode & 0xFFFFFFFF
    if code == STATUS_DLL_NOT_FOUND:
        return f"biliup 无法启动：{VCRUNTIME_HINT}"
    if code in _LOADER_FAILURES:
        return (f"biliup 无法启动（0x{code:08X} {_LOADER_FAILURES[code]}）：Microsoft Visual C++ 运行库"
                f"可能损坏或版本太旧，请重新安装 {VCREDIST_URL}")
    return None


def _kernel32() -> Any:
    import ctypes

    return ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]


@contextmanager
def quiet_error_mode() -> Iterator[None]:
    """Windows：子进程继承调用方的错误模式。默认缺 DLL 时系统弹出错误框、一直等人点掉（无人值守就卡住）；
    启动 biliup 期间关掉这类弹框，让它直接以 0xC0000135 退出，由我们给出提示。其他系统什么都不做。
    错误模式是进程级的：只在上面加标志位、结束时恢复原值（worker 进程本来就整体打开了这两个标志）。"""
    k32: Any = None
    old = 0
    if sys.platform == "win32":
        try:
            k32 = _kernel32()
            old = int(k32.GetErrorMode())
            k32.SetErrorMode(old | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
        except (OSError, AttributeError, ValueError):
            k32 = None
    try:
        yield
    finally:
        if k32 is not None:
            k32.SetErrorMode(old)


@dataclass
class Probe:
    """`biliup --version` 试运行的结果。"""

    ok: bool
    version: str | None = None
    returncode: int | None = None
    timed_out: bool = False
    detail: str = ""  # 不 ok 时：原因和处理办法


def probe_binary(path: Path, timeout: float = PROBE_TIMEOUT) -> Probe:
    """运行 `biliup --version`（纯本地、不联网）确认它能启动。

    注意不带参数的 biliup 会启动 Web 服务，绝不能那样调用。"""
    try:
        with quiet_error_mode():
            proc = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=timeout, env=biliup_env(), **_no_window_kwargs())
    except subprocess.TimeoutExpired:
        return Probe(False, timed_out=True,
                     detail=f"biliup --version {timeout:g} 秒没有结束（可能正被杀毒软件扫描或拦截），稍后重试")
    except OSError as exc:
        return Probe(False, detail=f"无法运行 biliup（{path}）：{exc}")
    hint = startup_failure_hint(proc.returncode)
    if hint:
        return Probe(False, returncode=proc.returncode, detail=hint)
    stdout = strip_ansi(proc.stdout.decode("utf-8", "replace"))
    if proc.returncode != 0:
        text = stdout + "\n" + strip_ansi(proc.stderr.decode("utf-8", "replace"))
        tail = " | ".join(x.strip() for x in text.splitlines() if x.strip())[-300:]
        return Probe(False, returncode=proc.returncode,
                     detail=f"biliup --version 失败（退出码 {proc.returncode}）" + (f"：{tail}" if tail else ""))
    m = re.search(r"(\d+\.\d+\.\d+\S*)", stdout)
    return Probe(True, version=f"v{m.group(1)}" if m else None, returncode=0)


_PROBE_OK: dict[tuple[str, int, int], Probe] = {}


def probe_cached(path: Path, timeout: float = PROBE_TIMEOUT) -> Probe:
    """同一进程里同一个文件（路径、大小、修改时间都没变）试运行成功过就不再试；失败的每次都重试。"""
    try:
        st = path.stat()
    except OSError:
        return probe_binary(path, timeout)
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _PROBE_OK.get(key)
    if hit is not None:
        return hit
    probe = probe_binary(path, timeout)
    if probe.ok:
        _PROBE_OK[key] = probe
    return probe


def _no_window_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


# ------------------------------------------------------------------ doctor / setup

def doctor_items(settings: Settings) -> list[dict[str, Any]]:
    if settings.biliup_path:
        p = Path(settings.biliup_path).expanduser()
        if not p.is_file():
            return [{"name": "biliup", "ok": False, "detail": f"biliup_path 指向的文件不存在: {p}"}]
        probe = probe_binary(p)
        if not probe.ok:
            return [{"name": "biliup", "ok": False, "detail": f"{p}（biliup_path）：{probe.detail}"}]
        return [{"name": "biliup", "ok": True,
                 "detail": f"{p}（biliup_path，版本 {probe.version or '未知'}，不受 mpub setup 管理）"}]
    exe = managed_path(settings)
    if not exe.is_file():
        detail = f"未安装 {settings.biliup_version}：运行 mpub setup（国内网络可设置 github_mirror）"
        on_path = shutil.which("biliup") or shutil.which("biliupR")
        if on_path:
            detail += (f"；也可以直接用已安装的 {on_path}：在 config.toml 的 [platforms.bilibili] 写 "
                       f"biliup_path（参数按 {settings.biliup_version} 核对过，版本差太多可能不兼容）")
        return [{"name": "biliup", "ok": False, "detail": detail}]
    info = read_install_info(settings) or {}
    detail = f"{settings.biliup_version} {exe}"
    if info.get("archive_sha256"):
        detail += f"；压缩包 sha256={info['archive_sha256'][:16]}…（{info.get('verified', 'tofu')}）"
    expected = info.get("binary_sha256")
    if expected and sha256_file(exe) != expected:
        return [{"name": "biliup", "ok": False,
                 "detail": f"{exe} 与安装时记录的 sha256 不一致（被替换或损坏）：删除该目录后重新 mpub setup"}]
    probe = probe_binary(exe)  # 文件没问题不等于能运行（例如缺 VC++ 运行库）
    if not probe.ok:
        return [{"name": "biliup", "ok": False, "detail": f"{exe}：{probe.detail}"}]
    return [{"name": "biliup", "ok": True, "detail": detail}]


def _installed_item(dest: Path, detail: str, *, managed: bool = True) -> dict[str, Any]:
    """安装结果：再试运行一次 `--version`，起不来就算失败（文件留着，装好运行库后不用重新下载）。"""
    probe = probe_binary(dest)
    if not probe.ok:
        keep = "（文件已保留，处理后运行 mpub doctor 确认，不用重新下载）" if managed else ""
        return {"name": "biliup", "ok": False, "detail": f"{detail}；但{probe.detail}{keep}"}
    return {"name": "biliup", "ok": True, "detail": detail}


def install(settings: Settings, *, force: bool = False, transport: Any = None,
            log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """下载并安装固定版本的 biliup。已安装则什么都不做（不自动更新）。"""
    say = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    if settings.biliup_path:
        p = Path(settings.biliup_path).expanduser()
        if not p.is_file():
            return {"name": "biliup", "ok": False, "detail": f"使用 biliup_path={p}（文件不存在）"}
        return _installed_item(p, f"使用 biliup_path={p}", managed=False)
    dest = managed_path(settings)
    if dest.is_file() and not force:
        return _installed_item(dest, f"已安装 {settings.biliup_version}: {dest}")

    version = settings.biliup_version
    try:
        asset = asset_name(version)
    except BiliupError as exc:
        return {"name": "biliup", "ok": False, "detail": str(exc)}
    url = download_url(version, asset, settings.github_mirror)
    folder = install_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / (asset + ".part")
    say(f"下载 biliup {version}: {url}")
    try:
        digest = _download(url, archive, transport=transport)
    except Exception as exc:  # noqa: BLE001 - 网络错误种类很多，统一转成提示
        archive.unlink(missing_ok=True)
        hint = "" if settings.github_mirror else "；国内网络可在 config.toml 设置 github_mirror"
        return {"name": "biliup", "ok": False, "detail": f"下载失败: {type(exc).__name__}: {exc}{hint}"}

    expected = KNOWN_SHA256.get(asset)
    if expected and digest != expected:
        archive.unlink(missing_ok=True)
        return {"name": "biliup", "ok": False,
                "detail": f"{asset} 的 sha256 不匹配（期望 {expected}，实际 {digest}），已删除，未安装"}
    try:
        _extract_binary(archive, dest, is_zip=asset.endswith(".zip"))
    except (BiliupError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        archive.unlink(missing_ok=True)
        return {"name": "biliup", "ok": False, "detail": f"解压失败: {exc}"}
    archive.unlink(missing_ok=True)

    verified = "pinned" if expected else "tofu"
    info = {
        "version": version, "asset": asset, "url": url, "archive_sha256": digest,
        "binary_sha256": sha256_file(dest), "verified": verified,
        "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (folder / "install.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / f"{asset}.sha256").write_text(f"{digest}  {asset}\n", encoding="utf-8")
    note = "与内置校验值一致" if expected else "首次下载，已记录（trust-on-first-use，请自行核对）"
    say(f"biliup 压缩包 sha256={digest}（{note}）")
    return _installed_item(dest, f"已安装 {version}: {dest}；sha256={digest}（{note}）")


DOWNLOAD_DEADLINE = 30 * 60  # 整个下载最多 30 分钟


def _download(url: str, dest: Path, *, transport: Any = None) -> str:
    import httpx

    h = hashlib.sha256()
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.Client(follow_redirects=True, timeout=timeout, transport=transport,
                      headers={"User-Agent": "multipub"}) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"超过 {DOWNLOAD_DEADLINE // 60} 分钟未下载完")
                    fh.write(chunk)
                    h.update(chunk)
    return h.hexdigest()


def _extract_binary(archive: Path, dest: Path, *, is_zip: bool) -> None:
    """只解出 biliup 可执行文件本身（按文件名匹配，不信任压缩包里的路径）。"""
    names = {"biliup", "biliup.exe"}
    tmp = dest.with_name(dest.name + ".tmp")
    if is_zip:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.infolist() if not m.is_dir() and Path(m.filename).name.lower() in names]
            if not members:
                raise BiliupError("压缩包里没有 biliup 可执行文件")
            member = min(members, key=lambda m: len(m.filename))
            with zf.open(member) as src, tmp.open("wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
    else:
        with tarfile.open(archive, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile() and Path(m.name).name in names]
            if not members:
                raise BiliupError("压缩包里没有 biliup 可执行文件")
            member = min(members, key=lambda m: len(m.name))
            src = tf.extractfile(member)
            if src is None:
                raise BiliupError("无法读取压缩包里的 biliup")
            with src, tmp.open("wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
    if sys.platform != "win32":
        tmp.chmod(0o755)
    os.replace(tmp, dest)


# ------------------------------------------------------------------ 上传参数

def creation_statement_for(options: dict[str, Any], ai_generated: bool) -> int | None:
    """创作声明 id（来自 OmniPost 的用法，未在真实账号验证）：-1 无，1 AI，2 虚构演绎，3 营销，4 个人观点。"""
    value = options.get("creation_statement")
    if value not in (None, ""):
        return int(value)
    return 1 if ai_generated else None


def build_upload_args(exe: Path, cookie_file: Path, post: Any) -> list[str]:
    """拼 `biliup -u <file> upload ...`。字符串一律用 --opt=value，避免值以 - 开头被误解析。"""
    o = post.options
    title = post.title.strip()
    desc = post.body.strip() or title  # B站简介不能为空：没写就用标题
    copyright_ = int(o.get("copyright", 1))
    args = [str(exe), "--rust-log", "info", "-u", str(cookie_file), "upload",
            f"--title={title}", f"--desc={desc}", f"--tid={int(o['tid'])}",
            f"--tag={','.join(post.tags)}", f"--copyright={copyright_}"]
    if copyright_ == 2:
        args.append(f"--source={str(o.get('source', '')).strip()}")
    if o.get("tid_v2") not in (None, ""):
        args.append(f"--tid-v2={int(o['tid_v2'])}")
    if post.cover is not None:
        args.append(f"--cover={post.cover}")
    if post.schedule is not None:
        args.append(f"--dtime={int(post.schedule.timestamp())}")  # 本地时间 -> epoch
    if str(o.get("dynamic") or "").strip():
        args.append(f"--dynamic={str(o['dynamic']).strip()}")
    # 开关只认真正的 true（validate 已拒绝 "false" 之类的字符串，这里再保险一次）
    for key, flag in VIDEO_FLAGS.items():
        if o.get(key) is True:
            args.append(flag)
    extra = dict(o.get("extra_fields") or {})
    statement = creation_statement_for(o, post.ai_generated)
    if statement is not None and "creation_statement" not in extra:
        extra["creation_statement"] = {"id": statement}
    if extra:
        args.append("--extra-fields=" + json.dumps(extra, ensure_ascii=False, separators=(",", ":")))
    if o.get("line"):
        args.append(f"--line={str(o['line']).lower()}")
    if o.get("limit"):
        args.append(f"--limit={int(o['limit'])}")
    if o.get("submit"):
        args.append(f"--submit={str(o['submit']).lower()}")
    args += ["--", str(post.video)]
    return args


# ------------------------------------------------------------------ 输出解析

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
_AID_RE = re.compile(r'"aid"\s*:\s*(?:Number\()?(\d+)')
# info 日志里是 message: "…"；错误报告里整段被 Debug 转义成 message: \"…\"
_RESP_RE = re.compile(r'ResponseData \{ code: (-?\d+),.*?message: \\?"(.*?)\\?", ttl')
# tracing 默认格式（NO_COLOR）："2026-10-20 12:03:01  INFO biliup::uploader::bilibili: 消息"。
# Linux/macOS 上多线程进程取不到本地时区，时间戳会是 "<unknown time>"。
# 行首只允许时间戳，所以消息里回显的文件名（pre_upload: {"name":…}、Upload completed: <文件>）
# 伪造不了级别和 target，也伪造不了下面按 target 锚定的标记
_LOG_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[T ][\d:.]+\S*|<unknown time>)?\s*"
                     r"(TRACE|DEBUG|INFO|WARN|ERROR)\s+(biliup[\w:]*): (.*)$")
UPLOADED_MARKERS = ("Upload completed:", "All files uploaded successfully")
# 投稿请求所在模块（crates/biliup/src/uploader/bilibili.rs）：投稿响应和「接口投稿成功」只认它
SUBMIT_TARGET = "biliup::uploader::bilibili"
# biliup 自己的投稿成功日志（APP接口投稿成功 / Web 接口投稿成功 / BCUT接口投稿成功），整条消息完全匹配
_SUBMIT_OK_RE = re.compile(r"(?:APP|Web|BCUT)\s*接口投稿成功")
LOGIN_ERROR_MARKERS = ("credential validation failed", "credential refresh failed",
                       "open cookies file", "账号未登录", "未知平台", "missing field `token_info`")
_ECHO_MIN_LEN = 4  # 太短的回显串不做替换，免得把标记本身抹掉


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def echo_strings(*items: Path | str | None) -> list[str]:
    """biliup 可能在输出里回显的用户内容（文件名/路径、标题；原样和 JSON 转义两种），
    匹配登录错误 / 投稿响应前先抹掉，免得其中的字样被当成状态。"""
    out: set[str] = set()
    for item in items:
        if item is None:
            continue
        forms = (str(item), item.as_posix(), item.name) if isinstance(item, Path) else (str(item),)
        for s in forms:
            out.add(s)
            out.add(json.dumps(s, ensure_ascii=False)[1:-1])
    return sorted((s for s in out if len(s) >= _ECHO_MIN_LEN), key=len, reverse=True)


def _scrub(text: str, echo: list[str] | tuple[str, ...]) -> str:
    for s in echo:
        text = text.replace(s, "<file>")
    return text


@dataclass
class UploadOutcome:
    returncode: int | None  # None = 超时被杀
    bvid: str | None = None
    aid: str | None = None
    response_code: int | None = None  # 最后一次投稿响应的 code
    response_message: str = ""
    uploaded: bool = False  # 视频分片已全部传完（之后就是投稿请求）
    submitted: bool = False  # 投稿响应 code 0（没解析到响应时看 biliup 自己的「接口投稿成功」日志）
    login_error: bool = False
    tail: list[str] = field(default_factory=list)

    @property
    def tail_text(self) -> str:
        return " | ".join(line for line in self.tail if line.strip())[-600:]


def parse_upload_output(lines: list[str], returncode: int | None, *, echo: list[str] | tuple[str, ...] = ()
                        ) -> UploadOutcome:
    """解析 biliup 输出。echo：会被回显的文件名/路径/标题（echo_strings），避免其中的字样被当成状态。

    - 投稿结果以投稿响应 ResponseData 为准（最后一条；code 非 0 时任何「投稿成功」字样都不算），
      只认投稿模块（SUBMIT_TARGET）的 INFO 日志和非 tracing 的错误报告行。
    - 上传完成 / 投稿成功标记必须是 biliup 自己的 tracing 日志：消息以固定前缀开头 / 整条完全匹配，
      投稿成功还要求 target 是投稿模块。只有没解析到投稿响应时才看投稿成功标记。
    - 登录错误只看非 INFO 的行（错误报告、WARN/ERROR），并先抹掉回显的内容。
    """
    out = UploadOutcome(returncode=returncode)
    ok_marker = False
    resp_lines: list[str] = []
    for raw in lines:
        line = strip_ansi(raw)
        log = _LOG_RE.match(line)
        if log:
            level, target, msg = log.group(1), log.group(2), log.group(3).strip()
            submit_mod = target == SUBMIT_TARGET
            if level == "INFO" and msg.startswith(UPLOADED_MARKERS):
                out.uploaded = True
            if level == "INFO" and submit_mod and _SUBMIT_OK_RE.fullmatch(msg):
                ok_marker = True
            resp_src = msg if (level == "INFO" and submit_mod and msg.startswith("ResponseData {")) else ""
            login_src = "" if level in ("INFO", "DEBUG", "TRACE") else _scrub(msg, echo)
        else:  # 错误报告：抹掉回显内容后再判断；投稿响应的 message 仍取原文（可能引用了标题）
            login_src = _scrub(line, echo)
            resp_src = line if _RESP_RE.search(login_src) else ""
        if login_src and any(m in login_src for m in LOGIN_ERROR_MARKERS):
            out.login_error = True
        resp = _RESP_RE.search(resp_src) if resp_src else None
        if resp:
            out.response_code = int(resp.group(1))
            out.response_message = resp.group(2)
            resp_lines.append(resp_src)
            if out.response_code == 0:
                bv = BV_RE.search(resp_src)
                aid = _AID_RE.search(resp_src)
                out.bvid = bv.group(0) if bv else out.bvid
                out.aid = aid.group(1) if aid else out.aid
    if out.response_code is not None:
        out.submitted = out.response_code == 0
    else:
        out.submitted = ok_marker
    if out.submitted and not out.bvid:
        # 兜底：只在投稿响应行里找 BV 号（视频文件名里也可能有 BV 字样）
        for src in reversed(resp_lines):
            bv = BV_RE.search(src) if "bvid" in src else None
            if bv:
                out.bvid = bv.group(0)
                break
    out.tail = [strip_ansi(x) for x in lines[-12:]]
    return out


def video_url(bvid: str | None, aid: str | None) -> str | None:
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}"
    if aid:
        return f"https://www.bilibili.com/video/av{aid}"
    return None


# ------------------------------------------------------------------ 运行

LineHandler = Callable[[str | None], None]  # None 表示心跳（一段时间没有新输出）
STREAM_LIMIT = 1024 * 1024  # 单行输出上限（字节）


def biliup_env() -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"  # tracing 日志不要 ANSI 颜色
    return env


async def run_streaming(args: list[str], *, cwd: Path, timeout: float,
                        on_line: LineHandler | None = None, heartbeat: float = 30) -> tuple[int | None, list[str]]:
    """运行命令，逐行收集 stdout+stderr。超时杀进程并返回 (None, 已有输出)。"""
    cwd.mkdir(parents=True, exist_ok=True)
    with quiet_error_mode():  # 无人值守：缺 DLL / 崩溃时不弹系统错误框
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=biliup_env(), limit=STREAM_LIMIT, **_no_window_kwargs())
    assert proc.stdout is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    lines: list[str] = []
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None, lines
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=min(remaining, heartbeat))
            except asyncio.TimeoutError:
                if on_line:
                    on_line(None)
                continue
            except ValueError:  # 单行超过 STREAM_LIMIT：StreamReader 已丢弃这段，记一笔继续读
                lines.append("[multipub] 一行输出过长，已省略")
                continue
            if not raw:
                break
            line = strip_ansi(raw.decode("utf-8", errors="replace")).rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=max(1.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            return None, lines
        return rc, lines
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass


async def renew(exe: Path, cookie_file: Path, *, cwd: Path, timeout: float = 90) -> tuple[bool, str]:
    """biliup renew：用 refresh_token 刷新整份登录文件（会重写文件）。"""
    rc, lines = await run_streaming([str(exe), "--rust-log", "info", "-u", str(cookie_file), "renew"],
                                    cwd=cwd, timeout=timeout)
    tail = " | ".join(x for x in lines[-5:] if x.strip())
    return rc == 0, startup_failure_hint(rc) or tail


def run_interactive_login(settings: Settings, account: str) -> int:
    """在当前终端里运行 `biliup -u <文件> login`（需要真实终端，二维码图片写在账号目录）。"""
    exe = find_biliup(settings)
    if exe is None:
        print("未找到 biliup：先运行 mpub setup", file=sys.stderr)
        return 1
    cookie_file = settings.account_file("bilibili", account)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("biliup 交互式登录需要在真实终端（如 Windows Terminal）里运行；"
              f"也可以直接执行: \"{exe}\" -u \"{cookie_file}\" login", file=sys.stderr)
        return 2
    probe = probe_binary(exe)  # 缺 VC++ 运行库时先说清楚，而不是让系统弹一个找不到 DLL 的对话框
    if not probe.ok and not probe.timed_out:
        print(probe.detail, file=sys.stderr)
        return 1
    print(f"即将运行 biliup 登录，推荐选择「扫码登录」。终端二维码显示不全时打开: "
          f"{cookie_file.parent / 'qrcode.png'}", file=sys.stderr, flush=True)
    started = time.time()
    proc = subprocess.run([str(exe), "-u", str(cookie_file), "login"], cwd=str(cookie_file.parent))
    if proc.returncode == 0 and cookie_file.is_file() and cookie_file.stat().st_mtime >= started - 1:
        print(f"登录信息已保存: {cookie_file}", file=sys.stderr)
    hint = startup_failure_hint(proc.returncode)
    if hint:
        print(hint, file=sys.stderr)
    return proc.returncode
