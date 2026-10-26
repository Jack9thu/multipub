"""B站适配器测试：全部离线（httpx.MockTransport、假 biliup 输出、本地子进程）。"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from multipub.config import load_settings
from multipub.models import (ErrorCode, Kind, LoginRequired, NeedsAction, PlatformPost, PublishError,
                             Status, ValidationFailed)
from multipub.platforms import bili_biliup as bb
from multipub.platforms import bili_cookies as bc
from multipub.platforms import bili_http as bh
from multipub.platforms.base import JobContext
from multipub.platforms.bilibili import BilibiliAdapter, note_text

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


# ------------------------------------------------------------------ helpers

def biliup_login(*, token: bool = True, expires: int | None = None) -> dict[str, Any]:
    exp = expires if expires is not None else int(time.time()) + 100 * 86400

    def ck(name: str, value: str, http_only: int = 0) -> dict[str, Any]:
        return {"name": name, "value": value, "http_only": http_only, "expires": exp, "secure": 0}

    data: dict[str, Any] = {
        "cookie_info": {"cookies": [ck("SESSDATA", "sess%2Cabc", 1), ck("bili_jct", "csrf123"),
                                    ck("DedeUserID", "10086"), ck("DedeUserID__ckMd5", "md5"), ck("sid", "sid1")],
                        "domains": [".bilibili.com"]},
        "sso": ["https://passport.bilibili.com/api/v2/sso"],
        "platform": "BiliTV",
    }
    if token:
        data["token_info"] = {"mid": 10086, "access_token": "at", "refresh_token": "rt", "expires_in": 15552000}
    return data


def write_json(path: Path, data: dict[str, Any], *, bom: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig" if bom else "utf-8")
    return path


@pytest.fixture
def settings(home: Path):
    s = load_settings(home)
    s.ensure_dirs()
    return s


REAL_PROBE = bb.probe_binary


@pytest.fixture(autouse=True)
def biliup_starts(monkeypatch):
    """测试里的「biliup」都是假文件，跑不了 `--version`：默认当作能启动。
    要测真实试运行的用例用 monkeypatch.setattr(bb, "probe_binary", REAL_PROBE) 换回来。"""
    from multipub.platforms import bili_locate

    bb._PROBE_OK.clear()
    bili_locate._CAP_CACHE.clear()
    monkeypatch.setattr(bb, "probe_binary", lambda path, timeout=bb.PROBE_TIMEOUT: bb.Probe(True, "v1.3.1", 0))
    # 兼容性检查读 `--help` / `upload --help`：假文件同样跑不了，返回 v1.3.1 的参数表
    monkeypatch.setattr(bili_locate, "_help", lambda exe, args: V131_HELP["upload" if "upload" in args else "top"])
    yield
    bb._PROBE_OK.clear()
    bili_locate._CAP_CACHE.clear()


V131_HELP = {
    "top": "Commands:\n  login\n  renew\n  upload\nOptions:\n  -u, --user-cookie <F>\n      --rust-log <L>\n",
    "upload": "\n".join(f"      {f} <X>" for f in (
        "--title", "--desc", "--tid", "--tid-v2", "--tag", "--copyright", "--source", "--cover", "--dtime",
        "--dynamic", "--no-reprint", "--is-only-self", "--up-selection-reply", "--up-close-reply",
        "--up-close-danmu", "--extra-fields", "--line", "--limit", "--submit")),
}


DLL_NOT_FOUND = 3221225781  # 0xC0000135，Windows 上 Python 拿到的是无符号值


def fake_subprocess_run(monkeypatch, returncode: int, stdout: bytes = b"", stderr: bytes = b""
                        ) -> list[tuple[list[str], dict[str, Any]]]:
    """让 probe_binary 里的 subprocess.run 返回给定退出码（模拟 Windows 的加载器错误）。"""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args, **kw):
        calls.append((list(args), kw))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(bb, "probe_binary", REAL_PROBE)
    monkeypatch.setattr(bb.subprocess, "run", run)
    return calls


def make_ctx(settings, action: str = "publish", account: str = "main") -> tuple[JobContext, list[dict]]:
    events: list[dict] = []
    ctx = JobContext(settings=settings, platform="bilibili", account=account, action=action,
                     headless=True, run_dir=settings.runs_dir / "t", emit=events.append)
    return ctx, events


def video_post(media: Path, **kw: Any) -> PlatformPost:
    base = dict(platform="bilibili", account="main", kind=Kind.VIDEO, title="第12期：测试视频",
                body="简介", tags=["科技", "教程"], video=media / "video.mp4", cover=media / "cover.jpg",
                options={"tid": 231, "copyright": 1})
    base.update(kw)
    return PlatformPost(**base)


def note_post(media: Path, **kw: Any) -> PlatformPost:
    base = dict(platform="bilibili", account="main", kind=Kind.NOTE, title="周末徒步",
                body="正文第一行\n第二行", tags=["徒步", "周末"],
                images=[media / "images" / "1.jpg", media / "images" / "2.jpg"], options={})
    base.update(kw)
    return PlatformPost(**base)


def errors_of(issues) -> set[str]:
    return {i.field for i in issues if i.level == "error"}


def warnings_of(issues) -> set[str]:
    return {i.field for i in issues if i.level == "warning"}


def independent_wbi(params: dict[str, Any], img_key: str, sub_key: str) -> str:
    """独立的直白实现（照 WBI 文档逐步写），用来交叉验证。"""
    tab = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28,
           14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21,
           56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
    raw = img_key + sub_key
    mixin = ""
    for i in tab:
        mixin += raw[i]
    mixin = mixin[:32]
    parts = []
    for k in sorted(params):
        v = str(params[k])
        for ch in "!'()*":
            v = v.replace(ch, "")
        parts.append(urllib.parse.quote(k, safe="") + "=" + urllib.parse.quote(v, safe=""))
    return hashlib.md5(("&".join(parts) + mixin).encode()).hexdigest()


# ------------------------------------------------------------------ WBI / APP 签名

def test_wbi_documented_vector():
    assert bh.mixin_key(IMG_KEY, SUB_KEY) == "ea1db124af3c7062474693fa704f4ff8"
    signed = bh.wbi_sign({"foo": "114", "bar": "514", "zab": 1919810}, IMG_KEY, SUB_KEY, wts=1702204169)
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"
    assert signed["wts"] == 1702204169 and signed["foo"] == "114"
    # 编码示例（空格 %20、中文大写百分号编码）
    assert bh.encode_query({"bar": "五一四", "baz": 1919810, "foo": "one one four"}) == \
        "bar=%E4%BA%94%E4%B8%80%E5%9B%9B&baz=1919810&foo=one%20one%20four"


def test_wbi_matches_independent_impl_with_special_chars():
    params = {"keyword": "a b!'()*c 中文", "platform": "web", "csrf": "x/y=z&", "n": 3}
    signed = bh.wbi_sign(params, IMG_KEY, SUB_KEY, wts=1700000000)
    expected = independent_wbi({**params, "wts": 1700000000}, IMG_KEY, SUB_KEY)
    assert signed["w_rid"] == expected
    assert signed["keyword"] == params["keyword"]  # 发送的是原值，只在签名时去掉 !'()*


def test_wbi_js_semantics():
    """与网页端 um() 一致：None 跳过；只有字符串去掉 !'()*；对象按 String(obj) 即 "[object Object]" 签名。"""
    params = {"a": None, "b": True, "obj": {"x": "(1)"}, "s": "x(y)!", "n": 1.0}
    signed = bh.wbi_sign(params, IMG_KEY, SUB_KEY, wts=1700000000)
    assert "a" not in signed and signed["obj"] == {"x": "(1)"}
    expected = independent_wbi({"b": "true", "obj": "[object Object]", "s": "x(y)!", "n": "1",
                                "wts": 1700000000}, IMG_KEY, SUB_KEY)
    assert signed["w_rid"] == expected
    # URL 里对象发 JSON（网页端请求库 JSON.stringify），None 不发
    assert bh.encode_query({"m": {"k": "v"}, "z": None}) == "m=%7B%22k%22%3A%22v%22%7D"


def test_create_dyn_query_shape():
    dyn_req = bh.build_dyn_req(text="t", title="", pics=[], mid=1, upload_id="1_2_3")
    query = bh.create_dyn_query("csrf1", (IMG_KEY, SUB_KEY), dyn_req, wts=1700000000)
    params = dict(urllib.parse.parse_qsl(query))
    assert params["wts"] == "1700000000" and params["w_dyn_req.upload_id"] == "1_2_3"
    assert json.loads(params["w_dyn_req.meta"]) == {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}}
    assert {"dm_img_list", "dm_img_str", "dm_cover_img_str", "dm_img_inter"} <= set(params)
    w_rid = params.pop("w_rid")
    assert w_rid == independent_wbi({**params, "w_dyn_req.meta": "[object Object]"}, IMG_KEY, SUB_KEY)
    assert dyn_req["option"] == {"aigc": 2}  # 没有图片时不带 pic_mode


def test_wbi_keys_from_nav():
    nav = {"wbi_img": {"img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                       "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png"}}
    assert bh.wbi_keys(nav) == (IMG_KEY, SUB_KEY)
    with pytest.raises(PublishError):
        bh.wbi_keys({"wbi_img": {}})


def test_app_sign_documented_vectors():
    # API-collect TV 扫码登录文档里的示例签名（ts=0）
    assert bh.app_sign({"appkey": bh.TV_APPKEY, "local_id": "0", "ts": 0})["sign"] == \
        "e134154ed6add881d28fbdf68653cd9c"
    assert bh.app_sign({"ts": 0, "local_id": "0", "auth_code": "6214464b3025541abf6f654cf7569a01",
                        "appkey": bh.TV_APPKEY})["sign"] == "87de3d0fee7c3f4facd244537238914e"


# ------------------------------------------------------------------ cookie 文件

def test_load_credential_bom_and_fields(tmp_path: Path):
    f = write_json(tmp_path / "中文目录" / "main.json", biliup_login(), bom=True)
    cred = bc.load_credential(f)
    assert cred.csrf == "csrf123" and cred.has_token and cred.mid == 10086
    assert cred.cookie_dict["SESSDATA"] == "sess%2Cabc"
    assert 99 < cred.days_left() <= 100
    # biliup（serde_json）不认 BOM：交给它之前原样去掉（内容不变），没有 BOM 时不动文件
    before = f.read_bytes()
    assert bc.strip_bom(f) and f.read_bytes() == before[3:]
    assert not bc.strip_bom(f) and not bc.strip_bom(tmp_path / "missing.json")
    assert bc.load_credential(f).csrf == "csrf123"


def test_load_credential_errors_and_storage_state_shape(tmp_path: Path):
    with pytest.raises(bc.CredentialError):
        bc.load_credential(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(bc.CredentialError):
        bc.load_credential(tmp_path / "bad.json")
    no_csrf = biliup_login()
    no_csrf["cookie_info"]["cookies"] = [c for c in no_csrf["cookie_info"]["cookies"] if c["name"] != "bili_jct"]
    with pytest.raises(bc.CredentialError, match="bili_jct"):
        bc.load_credential(write_json(tmp_path / "nocsrf.json", no_csrf))
    # Playwright storage_state 形状也能读（但没有 token，不能传视频）
    state = {"cookies": [{"name": "SESSDATA", "value": "s", "domain": ".bilibili.com"},
                         {"name": "bili_jct", "value": "c", "domain": ".bilibili.com"}], "origins": []}
    cred = bc.load_credential(write_json(tmp_path / "state.json", state))
    assert not cred.has_token and cred.csrf == "c"


def test_storage_state_conversion_coerces_ints(tmp_path: Path):
    cred = bc.load_credential(write_json(tmp_path / "a.json", biliup_login()))
    state = bc.to_storage_state(cred, {"buvid3": "B3", "SESSDATA": "ignored"})
    by_name = {c["name"]: c for c in state["cookies"]}
    sess = by_name["SESSDATA"]
    assert sess["httpOnly"] is True and sess["secure"] is False
    assert sess["domain"] == ".bilibili.com" and sess["path"] == "/" and sess["sameSite"] == "Lax"
    assert isinstance(sess["expires"], float) and sess["expires"] > time.time()
    assert by_name["bili_jct"]["httpOnly"] is False
    assert by_name["buvid3"]["value"] == "B3" and by_name["buvid3"]["httpOnly"] is False
    assert by_name["SESSDATA"]["value"] == "sess%2Cabc"  # extra 不覆盖已有 cookie
    assert state["origins"] == []
    assert bc.to_playwright_cookie({"name": "x", "value": "1", "expires": 0, "sameSite": "None",
                                    "secure": 0})["secure"] is True
    assert bc.to_playwright_cookie({"name": "x", "value": "1", "expires": None})["expires"] == -1


def test_login_info_from_tv_poll_and_write(tmp_path: Path):
    data = biliup_login()
    poll = {"is_new": False, "mid": 10086, "access_token": "at", "refresh_token": "rt", "expires_in": 15552000,
            "token_info": data["token_info"], "cookie_info": data["cookie_info"], "sso": data["sso"]}
    info = bc.login_info_from_tv_poll(poll)
    assert set(info) == {"cookie_info", "sso", "token_info", "platform"}
    assert info["platform"] == "BiliTV" and info["token_info"]["mid"] == 10086
    target = tmp_path / "账号" / "main.json"
    bc.write_login_info(target, info)
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # biliup 不认 BOM
    assert json.loads(raw.decode("utf-8")) == info
    assert list(target.parent.iterdir()) == [target]  # 没有残留临时文件
    with pytest.raises(bc.CredentialError):
        bc.login_info_from_tv_poll({"mid": 1})


# ------------------------------------------------------------------ biliup 安装

@pytest.mark.parametrize("system,machine,suffix", [
    ("Windows", "AMD64", "x86_64-windows.zip"),
    ("Windows", "ARM64", "x86_64-windows.zip"),
    ("Linux", "x86_64", "x86_64-linux.tar.xz"),
    ("Linux", "aarch64", "aarch64-linux.tar.xz"),
    ("Linux", "armv7l", "arm-linux.tar.xz"),
    ("Darwin", "x86_64", "x86_64-macos.tar.xz"),
    ("Darwin", "arm64", "aarch64-macos.tar.xz"),
])
def test_asset_selection(system, machine, suffix):
    name = bb.asset_name("v1.3.1", system, machine)
    assert name == f"biliupR-v1.3.1-{suffix}"
    assert name in bb.KNOWN_SHA256
    assert bb.exe_name(system) == ("biliup.exe" if system == "Windows" else "biliup")


def test_asset_selection_unsupported_and_urls():
    with pytest.raises(bb.BiliupError):
        bb.asset_name("v1.3.1", "FreeBSD", "x86_64")
    asset = "biliupR-v1.3.1-x86_64-windows.zip"
    gh = "https://github.com/biliup/biliup/releases/download/v1.3.1/" + asset
    assert bb.download_url("v1.3.1", asset) == gh
    assert bb.download_url("v1.3.1", asset, "https://ghfast.top/") == "https://ghfast.top/" + gh
    assert bb.download_url("v1.3.1", asset, "https://ghfast.top") == "https://ghfast.top/" + gh


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_xz_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _serve(payload: bytes, seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=payload)
    return httpx.MockTransport(handler)


def test_install_tofu_zip_and_doctor(settings, monkeypatch):
    monkeypatch.setattr(bb, "normalize_platform", lambda s=None, m=None: ("windows", "x86_64"))
    settings.biliup_version = "v9.9.9"
    settings.github_mirror = "https://mirror.example/"
    payload = _zip_bytes({"biliupR-v9.9.9-x86_64-windows/THIRD-PARTY-NOTICES.txt": b"x",
                          "biliupR-v9.9.9-x86_64-windows/biliup.exe": b"FAKE-EXE",
                          "biliupR-v9.9.9-x86_64-windows/render-tools/fonts/a.ttf": b"f"})
    seen: list[str] = []
    item = bb.install(settings, transport=_serve(payload, seen), log=lambda m: None)
    assert item["ok"], item
    assert seen == ["https://mirror.example/https://github.com/biliup/biliup/releases/download/v9.9.9/"
                    "biliupR-v9.9.9-x86_64-windows.zip"]
    exe = settings.tools_dir / "biliup" / "v9.9.9" / "biliup.exe"
    assert exe.read_bytes() == b"FAKE-EXE"
    info = bb.read_install_info(settings)
    assert info["archive_sha256"] == hashlib.sha256(payload).hexdigest() and info["verified"] == "tofu"
    assert (exe.parent / "biliupR-v9.9.9-x86_64-windows.zip.sha256").read_text().startswith(info["archive_sha256"])
    assert not list(exe.parent.glob("*.part"))
    assert bb.find_biliup(settings) == exe
    assert bb.doctor_items(settings)[0]["ok"]
    # 已安装：不再下载、不更新
    def boom(request):
        raise AssertionError("不应联网")
    assert bb.install(settings, transport=httpx.MockTransport(boom))["ok"]
    # 二进制被替换：doctor 报警
    exe.write_bytes(b"TAMPERED")
    assert not bb.doctor_items(settings)[0]["ok"]


def test_install_pinned_hash_mismatch_rejected(settings, monkeypatch):
    monkeypatch.setattr(bb, "normalize_platform", lambda s=None, m=None: ("linux", "x86_64"))
    assert settings.biliup_version == "v1.3.1"
    payload = _tar_xz_bytes({"biliupR-v1.3.1-x86_64-linux/biliup": b"NOT-THE-REAL-ONE"})
    item = bb.install(settings, transport=_serve(payload, []), log=lambda m: None)
    assert not item["ok"] and "sha256" in item["detail"]
    assert bb.find_biliup(settings) is None
    assert not list((settings.tools_dir / "biliup" / "v1.3.1").glob("*"))
    assert not bb.doctor_items(settings)[0]["ok"]


def test_install_tar_xz_ignores_member_paths(settings, monkeypatch):
    monkeypatch.setattr(bb, "normalize_platform", lambda s=None, m=None: ("linux", "x86_64"))
    settings.biliup_version = "v0.0.1"
    payload = _tar_xz_bytes({"../../evil/biliup": b"ELF-ish"})
    item = bb.install(settings, transport=_serve(payload, []), log=lambda m: None)
    assert item["ok"], item
    exe = settings.tools_dir / "biliup" / "v0.0.1" / "biliup"
    assert exe.read_bytes() == b"ELF-ish"
    assert not (settings.tools_dir.parent / "evil").exists()


def test_install_network_error_and_biliup_path(settings, monkeypatch):
    monkeypatch.setattr(bb, "normalize_platform", lambda s=None, m=None: ("linux", "x86_64"))
    def fail(request):
        raise httpx.ConnectError("blocked")
    item = bb.install(settings, transport=httpx.MockTransport(fail), log=lambda m: None)
    assert not item["ok"] and "github_mirror" in item["detail"]
    custom = settings.home / "my biliup.exe"
    custom.write_bytes(b"x")
    settings.biliup_path = str(custom)
    assert bb.install(settings)["ok"]
    assert bb.find_biliup(settings) == custom


# ------------------------------------------------------------------ biliup 能否启动（VC++ 运行库）

def test_startup_failure_hint():
    for rc in (DLL_NOT_FOUND, -1073741515):  # 无符号 / 有符号两种写法
        hint = bb.startup_failure_hint(rc)
        assert hint and "VCRUNTIME140.dll" in hint and "https://aka.ms/vs/17/release/vc_redist.x64.exe" in hint
    for rc in (0xC000007B, 0xC0000139):
        hint = bb.startup_failure_hint(rc)
        assert hint and "vc_redist.x64.exe" in hint and f"0x{rc:08X}" in hint
    for rc in (None, 0, 1, 2, 101, -9, 0xC0000005):  # 普通失败、被信号杀、崩溃：不是「起不来」
        assert bb.startup_failure_hint(rc) is None


def test_probe_binary_real_subprocess(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bb, "probe_binary", REAL_PROBE)
    # 用 Python 解释器当「biliup」：`python --version` 输出 Python 3.x.y、退出码 0
    probe = bb.probe_binary(Path(sys.executable))
    assert probe.ok and probe.version and probe.version.startswith("v3.") and probe.returncode == 0
    # 不是可执行文件：OSError，不抛出
    junk = tmp_path / "biliup.exe"
    junk.write_bytes(b"FAKE-EXE")
    junk.chmod(0o755)
    probe = bb.probe_binary(junk)
    assert not probe.ok and "无法运行 biliup" in probe.detail and not probe.timed_out
    probe = bb.probe_binary(tmp_path / "不存在.exe")
    assert not probe.ok and "无法运行 biliup" in probe.detail


@pytest.mark.skipif(sys.platform == "win32", reason="用 sh 脚本模拟")
def test_probe_binary_nonzero_exit_script(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bb, "probe_binary", REAL_PROBE)
    script = tmp_path / "biliup"
    script.write_text("#!/bin/sh\necho \"args: $*\"\necho 'error: unexpected argument' >&2\nexit 2\n")
    script.chmod(0o755)
    probe = bb.probe_binary(script)
    assert not probe.ok and probe.returncode == 2
    assert "退出码 2" in probe.detail and "args: --version" in probe.detail and "unexpected argument" in probe.detail


def test_probe_binary_dll_not_found_and_timeout(tmp_path: Path, monkeypatch):
    exe = tmp_path / "biliup.exe"
    exe.write_bytes(b"x")
    calls = fake_subprocess_run(monkeypatch, DLL_NOT_FOUND)
    probe = bb.probe_binary(exe)
    assert not probe.ok and probe.returncode == DLL_NOT_FOUND and not probe.timed_out
    assert "VCRUNTIME140.dll" in probe.detail and "vc_redist.x64.exe" in probe.detail
    # 只会带 --version 运行（不带参数的 biliup 会启动 Web 服务）；stdin 关掉，不等输入
    args, kw = calls[0]
    assert args == [str(exe), "--version"] and kw["stdin"] is subprocess.DEVNULL and kw["timeout"] > 0

    def hang(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(bb.subprocess, "run", hang)
    probe = bb.probe_binary(exe, timeout=5)
    assert not probe.ok and probe.timed_out and "5 秒" in probe.detail


def test_probe_cached_only_caches_success(tmp_path: Path, monkeypatch):
    exe = tmp_path / "biliup.exe"
    exe.write_bytes(b"x")
    results = [bb.Probe(False, detail="坏"), bb.Probe(True, "v1.3.1", 0)]
    seen: list[Path] = []

    def probe(path, timeout=bb.PROBE_TIMEOUT):
        seen.append(path)
        return results[min(len(seen), len(results)) - 1]

    monkeypatch.setattr(bb, "probe_binary", probe)
    assert not bb.probe_cached(exe).ok  # 失败不缓存
    assert bb.probe_cached(exe).ok and bb.probe_cached(exe).ok
    assert len(seen) == 2
    exe.write_bytes(b"xy")  # 文件换了：重新试
    assert bb.probe_cached(exe).ok and len(seen) == 3


def test_quiet_error_mode_windows(monkeypatch):
    """Windows 上启动 biliup 期间关掉系统的「找不到 DLL」弹框（子进程继承错误模式），结束后恢复。"""
    log: list[tuple[str, int]] = []

    class K32:
        mode = 0x8000

        def GetErrorMode(self):
            return self.mode

        def SetErrorMode(self, mode):
            log.append(("set", mode))
            old, self.mode = self.mode, mode
            return old

    k32 = K32()
    monkeypatch.setattr(bb, "_kernel32", lambda: k32)
    monkeypatch.setattr(bb.sys, "platform", "win32")
    with bb.quiet_error_mode():
        assert k32.mode == 0x8000 | bb.SEM_FAILCRITICALERRORS | bb.SEM_NOGPFAULTERRORBOX
    assert k32.mode == 0x8000 and len(log) == 2
    with pytest.raises(RuntimeError), bb.quiet_error_mode():  # 出异常也恢复
        raise RuntimeError
    assert k32.mode == 0x8000
    monkeypatch.setattr(bb.sys, "platform", "linux")
    with bb.quiet_error_mode():
        pass
    assert len(log) == 4  # 其他系统什么都不做


def test_install_and_doctor_report_missing_vcruntime(settings, monkeypatch):
    """Windows 版 biliup.exe 依赖 VCRUNTIME140.dll：文件和 sha256 都对也可能起不来，doctor / setup 要报出来。"""
    monkeypatch.setattr(bb, "normalize_platform", lambda s=None, m=None: ("windows", "x86_64"))
    settings.biliup_version = "v9.9.9"
    payload = _zip_bytes({"biliupR-v9.9.9-x86_64-windows/biliup.exe": b"FAKE-EXE"})
    calls = fake_subprocess_run(monkeypatch, DLL_NOT_FOUND)
    item = bb.install(settings, transport=_serve(payload, []), log=lambda m: None)
    assert not item["ok"]
    assert "VCRUNTIME140.dll" in item["detail"] and "https://aka.ms/vs/17/release/vc_redist.x64.exe" in item["detail"]
    exe = settings.tools_dir / "biliup" / "v9.9.9" / "biliup.exe"
    assert exe.is_file() and "不用重新下载" in item["detail"]  # 文件保留
    doctor = bb.doctor_items(settings)[0]
    assert not doctor["ok"] and "VCRUNTIME140.dll" in doctor["detail"] and str(exe) in doctor["detail"]
    assert all(args == [str(exe), "--version"] for args, _ in calls) and len(calls) == 2
    # 装好运行库之后：doctor 通过，setup 不重新下载
    fake_subprocess_run(monkeypatch, 0, stdout=b"biliup-cli 9.9.9\n")

    def boom(request):
        raise AssertionError("不应联网")

    assert bb.doctor_items(settings)[0]["ok"]
    assert bb.install(settings, transport=httpx.MockTransport(boom))["ok"]
    # biliup_path 指向的也一样
    custom = settings.home / "biliup.exe"
    custom.write_bytes(b"x")
    settings.biliup_path = str(custom)
    assert "v9.9.9" in bb.doctor_items(settings)[0]["detail"]
    fake_subprocess_run(monkeypatch, -1073741515)
    doctor = bb.doctor_items(settings)[0]
    assert not doctor["ok"] and "VCRUNTIME140.dll" in doctor["detail"] and "biliup_path" in doctor["detail"]
    item = bb.install(settings)
    assert not item["ok"] and "VCRUNTIME140.dll" in item["detail"] and "重新下载" not in item["detail"]


async def test_renew_reports_startup_failure(tmp_path: Path, monkeypatch):
    async def run(args, *, cwd, timeout, on_line=None, heartbeat=30):
        return DLL_NOT_FOUND, []

    monkeypatch.setattr(bb, "run_streaming", run)
    ok, tail = await bb.renew(tmp_path / "biliup.exe", tmp_path / "main.json", cwd=tmp_path)
    assert not ok and "VCRUNTIME140.dll" in tail


# ------------------------------------------------------------------ biliup 参数与输出

def test_build_upload_args_full(media: Path, tmp_path: Path):
    cover = media / "封面 图.jpg"
    cover.write_bytes((media / "cover.jpg").read_bytes())
    when = (datetime.now() + timedelta(hours=3)).replace(second=0, microsecond=0)
    post = video_post(media, body="", cover=cover, schedule=when, ai_generated=True, title="-以横线开头",
                      options={"tid": 231, "tid_v2": 2001, "copyright": 2, "source": "https://example.com/a",
                               "dynamic": "新视频！", "no_reprint": True, "close_reply": True, "line": "BDA2",
                               "limit": 5, "submit": "web", "extra_fields": {"foo": 1}})
    exe, cookie = Path("/opt/biliup"), tmp_path / "账号.json"
    args = bb.build_upload_args(exe, cookie, post)
    assert args[:6] == [str(exe), "--rust-log", "info", "-u", str(cookie), "upload"]
    assert args[-2:] == ["--", str(post.video)]
    assert "--title=-以横线开头" in args
    assert "--desc=-以横线开头" in args  # 简介为空时用标题
    assert "--tid=231" in args and "--tid-v2=2001" in args
    assert "--tag=科技,教程" in args
    assert "--copyright=2" in args and "--source=https://example.com/a" in args
    assert f"--cover={cover}" in args
    assert f"--dtime={int(when.timestamp())}" in args
    assert "--dynamic=新视频！" in args and "--no-reprint=1" in args and "--up-close-reply" in args
    assert "--line=bda2" in args and "--limit=5" in args and "--submit=web" in args
    extra = next(a for a in args if a.startswith("--extra-fields="))
    assert json.loads(extra.split("=", 1)[1]) == {"foo": 1, "creation_statement": {"id": 1}}


def test_build_upload_args_minimal(media: Path):
    post = video_post(media, cover=None)
    args = bb.build_upload_args(Path("biliup"), Path("c.json"), post)
    assert "--copyright=1" in args
    assert not any(a.startswith(("--source", "--cover", "--dtime", "--extra-fields", "--dynamic")) for a in args)


def test_build_upload_args_flags_need_real_true(media: Path):
    """"false" / "no" / 1 这类值不能打开 仅自己可见 / 关闭评论（validate 也会报错）。"""
    opts = {"tid": 231, "is_only_self": "false", "close_reply": "no", "selection_reply": "false",
            "no_reprint": 1, "close_danmu": False}
    args = bb.build_upload_args(Path("biliup"), Path("c.json"), video_post(media, options=opts))
    assert not [a for a in args if a.startswith(("--is-only-self", "--up-", "--no-reprint"))]
    issues = BilibiliAdapter.validate(video_post(media, options=opts))
    assert {"platforms.bilibili.is_only_self", "platforms.bilibili.close_reply", "platforms.bilibili.selection_reply",
            "platforms.bilibili.no_reprint"} <= errors_of(issues)
    assert "platforms.bilibili.close_danmu" not in errors_of(issues)
    on = {"tid": 231, "is_only_self": True, "close_danmu": True, "close_reply": None}
    args = bb.build_upload_args(Path("biliup"), Path("c.json"), video_post(media, options=on))
    assert "--is-only-self=1" in args and "--up-close-danmu" in args and "--up-close-reply" not in args
    assert not errors_of(BilibiliAdapter.validate(video_post(media, options=on)))


SUCCESS_LINES = [
    "\x1b[2m2026-10-20 12:00:00\x1b[0m \x1b[32m INFO\x1b[0m biliup_cli::uploader: user: 测试UP",
    "2026-10-20 12:00:01  INFO biliup_cli::uploader: number of concurrent futures: 3",
    "2026-10-20 12:03:00  INFO biliup_cli::uploader: Upload completed: video.mp4 => cost 179.00s, 1.20 MB/s.",
    "2026-10-20 12:03:00  INFO biliup_cli::uploader: All files uploaded successfully, checkpoint removed",
    '2026-10-20 12:03:01  INFO biliup::uploader::bilibili: ResponseData { code: 0, data: Some(Object '
    '{"aid": Number(113456789012345), "bvid": String("BV1Ab4y1C7xY")}), message: "0", ttl: Some(1) }',
    "2026-10-20 12:03:01  INFO biliup::uploader::bilibili: APP接口投稿成功",
]


def test_parse_upload_output_success():
    o = bb.parse_upload_output(SUCCESS_LINES, 0)
    assert o.submitted and o.uploaded and not o.login_error
    assert o.bvid == "BV1Ab4y1C7xY" and o.aid == "113456789012345" and o.response_code == 0
    assert bb.video_url(o.bvid, o.aid) == "https://www.bilibili.com/video/BV1Ab4y1C7xY"
    assert bb.video_url(None, "123") == "https://www.bilibili.com/video/av123"
    assert all("\x1b" not in line for line in o.tail)


def test_parse_upload_output_rejection_escaped():
    lines = SUCCESS_LINES[:4] + [
        'Error: 未知错误', '╰─▶ Custom("ResponseData { code: 21070, data: None, '
        'message: \\"请勿重复提交\\", ttl: Some(1) }")']
    o = bb.parse_upload_output(lines, 1)
    assert not o.submitted and o.uploaded
    assert o.response_code == 21070 and o.response_message == "请勿重复提交"
    assert o.bvid is None


def test_parse_upload_output_login_error():
    o = bb.parse_upload_output(["Error: credential validation failed with Bilibili code -101"], 1)
    assert o.login_error and not o.uploaded and o.response_code is None


TRICKY = Path("/data/第一次投稿成功 账号未登录.mp4")  # 文件名里带「投稿成功」和登录错误字样


def tricky_upload_lines(video: Path = TRICKY) -> list[str]:
    name = json.dumps(video.name, ensure_ascii=False)
    return [
        "2026-10-20 12:00:00  INFO biliup_cli::uploader: user: 测试UP",
        f'2026-10-20 12:00:02  INFO biliup::uploader::line: pre_upload: {{"name":{name},"size":1024}}',
        f"2026-10-20 12:03:00  INFO biliup_cli::uploader: Upload completed: {video.name} => cost 1.00s, 1.00 MB/s.",
        "2026-10-20 12:03:00  INFO biliup_cli::uploader: All files uploaded successfully, checkpoint removed",
        f"2026-10-20 12:03:00  WARN biliup_cli::uploader: 未搜索到匹配的视频文件：{video.name}",
    ]


REJECT_21070 = ('2026-10-20 12:03:01  INFO biliup::uploader::bilibili: ResponseData { code: 21070, data: None, '
                'message: "请勿重复提交", ttl: Some(1) }')


def test_parse_upload_output_file_name_is_not_a_marker():
    echo = bb.echo_strings(TRICKY)
    # 上传完了，投稿被明确拒绝：不能因为文件名里有「投稿成功」就当成功
    o = bb.parse_upload_output(tricky_upload_lines() + [REJECT_21070, "Error: 未知错误"], 1, echo=echo)
    assert not o.submitted and o.uploaded and o.response_code == 21070 and not o.login_error
    # 上传后网络出错、没有投稿响应
    o = bb.parse_upload_output(tricky_upload_lines() + ["Error: connection reset"], 1, echo=echo)
    assert not o.submitted and o.uploaded and o.response_code is None and not o.login_error
    # 还没开始传就失败（错误报告里带路径）
    o = bb.parse_upload_output(["Error: 未知错误", f"╰─▶ file {TRICKY}", "    No such file or directory"], 1,
                               echo=echo)
    assert not o.submitted and not o.uploaded and not o.login_error
    # 不传 echo 时，错误报告里的路径会被误认成登录错误：这正是要传 echo 的原因
    assert bb.parse_upload_output([f"╰─▶ file {TRICKY}"], 1).login_error


def test_parse_upload_output_success_marker_rules():
    ok = "2026-10-20 12:03:01  INFO biliup::uploader::bilibili: Web 接口投稿成功"
    # 没有可解析的投稿响应时才看 biliup 自己的「接口投稿成功」，且 target 必须是投稿模块
    assert bb.parse_upload_output(SUCCESS_LINES[:4] + [ok], 0).submitted
    fake = "2026-10-20 12:03:01  INFO biliup_cli::uploader: APP接口投稿成功"
    assert not bb.parse_upload_output(SUCCESS_LINES[:4] + [fake], 1).submitted
    assert not bb.parse_upload_output(["APP接口投稿成功", "投稿成功"], 1).submitted
    # code 非 0 的响应优先于任何「投稿成功」字样
    assert not bb.parse_upload_output(SUCCESS_LINES[:4] + [REJECT_21070, ok], 1).submitted


def test_parse_upload_output_unknown_time():
    """Linux/macOS 上 biliup 取不到本地时区，时间戳是 <unknown time>。"""
    lines = [line.replace("2026-10-20 12:03:00", "<unknown time>").replace("2026-10-20 12:03:01", "<unknown time>")
             for line in SUCCESS_LINES[2:]]
    o = bb.parse_upload_output(lines, 0)
    assert o.uploaded and o.submitted and o.bvid == "BV1Ab4y1C7xY"
    o = bb.parse_upload_output(lines[:2] + ["Error: connection reset"], 1)
    assert o.uploaded and not o.submitted


def test_echo_strings():
    echo = bb.echo_strings(Path("/a/b/投稿 成功.mp4"), None, "标题", "ab")
    assert "/a/b/投稿 成功.mp4" in echo and "投稿 成功.mp4" in echo and "标题" not in echo  # 太短的不抹
    assert echo == sorted(echo, key=len, reverse=True)
    win = bb.echo_strings(Path("C:/视频/a\"b.mp4"))
    assert any("\\\"" in s for s in win)  # JSON 转义形式（pre_upload 里的 "name"）


async def test_run_streaming_real_subprocess(tmp_path: Path):
    code = "import sys; print('第一行'); print('\\x1b[32mINFO\\x1b[0m ok'); sys.stderr.write('err\\n'); sys.exit(3)"
    rc, lines = await bb.run_streaming([sys.executable, "-c", code], cwd=tmp_path / "运行目录", timeout=30)
    assert rc == 3
    assert lines[:2] == ["第一行", "INFO ok"] and "err" in lines


async def test_run_streaming_timeout_kills(tmp_path: Path):
    beats: list[Any] = []
    code = "import time; print('start', flush=True); time.sleep(60)"
    t0 = time.monotonic()
    rc, lines = await bb.run_streaming([sys.executable, "-c", code], cwd=tmp_path, timeout=1.5,
                                       on_line=beats.append, heartbeat=0.5)
    assert rc is None and lines == ["start"]
    assert None in beats  # 心跳
    assert time.monotonic() - t0 < 15


# ------------------------------------------------------------------ 视频发布（模拟 biliup）

@pytest.fixture
def video_env(settings, media: Path, fake_bili):
    fake = settings.home / "biliup-fake.exe"
    fake.write_bytes(b"")
    settings.biliup_path = str(fake)
    # worker 会从磁盘重新读配置
    (settings.home / "config.toml").write_text(f"[platforms.bilibili]\nbiliup_path = '{fake}'\n", encoding="utf-8")
    write_json(settings.account_file("bilibili", "main"), biliup_login())
    return settings


def fake_run(lines: list[str], rc: int | None, calls: list[Any], timeouts: list[float] | None = None):
    async def run(args, *, cwd, timeout, on_line=None, heartbeat=30):
        calls.append(args)
        if timeouts is not None:
            timeouts.append(timeout)
        for line in lines:
            if on_line:
                on_line(line)
        return rc, list(lines)
    return run


async def test_publish_video_success(video_env, media: Path, monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, calls))
    ctx, events = make_ctx(video_env)
    result = await BilibiliAdapter(ctx).publish(video_post(media))
    assert result.status is Status.SUBMITTED
    assert result.url == "https://www.bilibili.com/video/BV1Ab4y1C7xY" and result.item_id == "BV1Ab4y1C7xY"
    assert result.submit_started and calls[0][0] == video_env.biliup_path
    names = [e["event"] for e in events]
    assert "submit_started" in names


async def test_publish_video_scheduled(video_env, media: Path, monkeypatch):
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, []))
    ctx, _ = make_ctx(video_env)
    post = video_post(media, schedule=datetime.now() + timedelta(days=1))
    result = await BilibiliAdapter(ctx).publish(post)
    assert result.status is Status.SCHEDULED and result.url.endswith("BV1Ab4y1C7xY")


@pytest.mark.parametrize("lines,rc,code,started", [
    (["Error: file not found"], 1, ErrorCode.PLATFORM_ERROR, False),
    (SUCCESS_LINES[:4] + ["Error: connection reset"], 1, ErrorCode.UNKNOWN_OUTCOME, True),
    (SUCCESS_LINES[:4] + ['ResponseData { code: 21012, data: None, message: "标签不能为空", ttl: Some(1) }'], 1,
     ErrorCode.PLATFORM_ERROR, True),
    (SUCCESS_LINES[:4] + ['ResponseData { code: -504, data: None, message: "服务调用超时", ttl: Some(1) }'], 1,
     ErrorCode.UNKNOWN_OUTCOME, True),
    (["uploading..."], None, ErrorCode.TIMEOUT, False),
    (SUCCESS_LINES[:4], None, ErrorCode.UNKNOWN_OUTCOME, True),
    (["Error: credential validation failed with Bilibili code -101"], 1, ErrorCode.LOGIN_REQUIRED, False),
    (SUCCESS_LINES[:4] + [REJECT_21070], 0, ErrorCode.PLATFORM_ERROR, True),  # 响应 code 优先于退出码
])
async def test_publish_video_failures(video_env, media: Path, monkeypatch, lines, rc, code, started):
    """视频传完之前就失败 / 超时：确定没投稿，不标记 submit_started（查重不拦、可直接重发）；传完之后才标记。"""
    monkeypatch.setattr(bb, "run_streaming", fake_run(lines, rc, []))
    ctx, _ = make_ctx(video_env)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx).publish(video_post(media))
    assert info.value.code is code
    assert ctx.submit_started is started


async def test_publish_video_marks_submit_when_upload_completes(video_env, media: Path, monkeypatch):
    """v1.3.1：看到「Upload completed」那一刻（投稿请求之前）就发出 submit_started 事件，被中断时主进程据此记成结果未知。"""
    seen: list[tuple[str, bool]] = []
    ctx, events = make_ctx(video_env)

    async def run(args, *, cwd, timeout, on_line=None, heartbeat=30):
        for line in SUCCESS_LINES:
            on_line(line)
            seen.append((line, ctx.submit_started))
        return 0, list(SUCCESS_LINES)

    monkeypatch.setattr(bb, "run_streaming", run)
    result = await BilibiliAdapter(ctx).publish(video_post(media))
    assert result.status is Status.SUBMITTED and result.submit_started
    started = [line for line, flag in seen if flag]
    assert started[0] == SUCCESS_LINES[2] and "Upload completed" in started[0]
    assert [e["event"] for e in events].count("submit_started") == 1

    # 输出格式没核对过的版本（biliup_path 指向别的版本）：启动 biliup 之前就标记，保守处理
    monkeypatch.setattr(bb, "probe_binary", lambda path, timeout=bb.PROBE_TIMEOUT: bb.Probe(True, "v1.2.0", 0))
    bb._PROBE_OK.clear()
    monkeypatch.setattr(bb, "run_streaming", fake_run(["Error: boom"], 1, []))
    ctx2, _ = make_ctx(video_env)
    with pytest.raises(PublishError):
        await BilibiliAdapter(ctx2).publish(video_post(media))
    assert ctx2.submit_started


def test_marks_submit_phase():
    assert bb.marks_submit_phase(SUCCESS_LINES[2]) and bb.marks_submit_phase(SUCCESS_LINES[3])
    assert bb.marks_submit_phase(SUCCESS_LINES[4]) and bb.marks_submit_phase(SUCCESS_LINES[5])
    assert not bb.marks_submit_phase(SUCCESS_LINES[0]) and not bb.marks_submit_phase(SUCCESS_LINES[1])
    # 回显的文件名 / 别的模块 / 非 INFO 都不算
    assert not bb.marks_submit_phase('<unknown time>  INFO biliup::uploader::line::upos: pre_upload: '
                                     '{"name":"Upload completed: x.mp4"}')
    assert not bb.marks_submit_phase("Upload completed: video.mp4 => cost 1s")
    assert not bb.marks_submit_phase("<unknown time>  WARN biliup_cli::uploader: Upload completed: x")
    assert not bb.marks_submit_phase('<unknown time>  INFO biliup_cli::uploader: ResponseData { code: 0 }')


# biliup v1.3.1 真实输出（断网时上传：登录阶段的网络错误），error-stack 报告 + backtrace
REAL_NETWORK_REPORT = """Error: \x1b[1mUnknown Error\x1b[22m
├╴at \x1b[3mcrates/biliup-cli/src/uploader.rs:403:31\x1b[23m
│
├─▶ \x1b[1merror sending request\x1b[22m
│   ╰╴at \x1b[3mcrates/biliup-cli/src/uploader.rs:403:31\x1b[23m
│
├─▶ \x1b[1mtcp connect error\x1b[22m
│   ╰╴at \x1b[3mcrates/biliup-cli/src/uploader.rs:403:31\x1b[23m
│
╰─▶ \x1b[1mNetwork is unreachable (os error 101)\x1b[22m
    ├╴at \x1b[3mcrates/biliup-cli/src/uploader.rs:403:31\x1b[23m
    ╰╴backtrace (1)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

backtrace no. 1
   0: <unknown>
   7: __libc_start_call_main
             at ./csu/../sysdeps/nptl/libc_start_call_main.h:58:16
   8: __libc_start_main_impl
             at ./csu/../csu/libc-start.c:360:3
   9: <unknown>""".encode().decode("unicode_escape").encode("latin-1").decode("utf-8").splitlines()


def test_error_text_summarises_error_stack_report():
    o = bb.parse_upload_output(REAL_NETWORK_REPORT, 1)
    assert o.error_text == "error sending request → tcp connect error → Network is unreachable (os error 101)"
    assert "backtrace" in o.tail_text  # 末尾 12 行全是 backtrace：以前给人看的就是这些
    lock = ["Error: 另一个使用该账号 (1) 的上传进程正在等待限流恢复，请稍后重试。",
            "如果确认当前没有其他上传进程在运行，可能是上次异常退出残留的锁文件，", "可手动删除以下文件后重试：",
            "  /tmp/biliup.lock", "├╴at crates/biliup-cli/src/uploader.rs:523:24", "╰╴backtrace (1)"]
    assert bb.parse_upload_output(lock, 1).error_text.endswith("可手动删除以下文件后重试： /tmp/biliup.lock")
    head_only = ["Error: open cookies file: c.json", "├╴at x.rs:1:1", "│", "╰─▶ No such file or directory (os error 2)"]
    assert bb.parse_upload_output(head_only, 1).error_text == \
        "open cookies file: c.json → No such file or directory (os error 2)"
    assert bb.parse_upload_output(["just text", "more"], 1).error_text == "just text | more"


async def test_publish_video_file_name_with_success_words(video_env, media: Path, monkeypatch):
    """文件名里有「投稿成功」：被拒 / 出错时不能报成 submitted（否则查重会挡住重发）。"""
    video = media / TRICKY.name
    video.write_bytes((media / "video.mp4").read_bytes())
    for tail, rc, code in (([REJECT_21070, "Error: 未知错误"], 1, ErrorCode.PLATFORM_ERROR),
                           (["Error: connection reset"], 1, ErrorCode.UNKNOWN_OUTCOME)):
        monkeypatch.setattr(bb, "run_streaming", fake_run(tricky_upload_lines(video) + tail, rc, []))
        ctx, _ = make_ctx(video_env)
        with pytest.raises(PublishError) as info:
            await BilibiliAdapter(ctx).publish(video_post(media, video=video, title="投稿成功复盘"))
        assert info.value.code is code
    # 上传前就失败：错误报告里回显的路径不能被当成登录错误
    lines = ["Error: 未知错误", f"╰─▶ file {video}", "    No such file or directory (os error 2)"]
    monkeypatch.setattr(bb, "run_streaming", fake_run(lines, 1, []))
    ctx, _ = make_ctx(video_env)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx).publish(video_post(media, video=video))
    assert info.value.code is ErrorCode.PLATFORM_ERROR and "尚未投稿" in str(info.value)


async def test_publish_video_deadline_accounts_for_elapsed(video_env, media: Path, monkeypatch):
    import multipub.platforms.bilibili as mod

    timeouts: list[float] = []
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, [], timeouts))
    ctx, _ = make_ctx(video_env)
    ctx.timeout = 1000
    adapter = BilibiliAdapter(ctx)
    real_token = adapter._token

    async def slow_token(cred, *, timeout=30):  # 预检用掉 0.3 秒
        await asyncio.sleep(0.3)
        return await real_token(cred, timeout=timeout)

    adapter._token = slow_token
    await adapter.publish(video_post(media))
    assert 1000 - mod.VIDEO_TIMEOUT_MARGIN - 5 < timeouts[0] <= 1000 - mod.VIDEO_TIMEOUT_MARGIN - 0.3
    # 预检卡住：有自己的上限，不会吃掉 biliup 的时间
    monkeypatch.setattr(mod, "TOKEN_PRECHECK_TIMEOUT", 0.05)

    async def hang(cred, *, timeout=30):
        await asyncio.sleep(30)

    adapter2 = BilibiliAdapter(make_ctx(video_env)[0])
    adapter2.ctx.timeout = 1000
    adapter2._token = hang
    result = await adapter2.publish(video_post(media))
    assert result.status is Status.SUBMITTED and timeouts[1] > 900
    # 剩余时间不够：不启动 biliup、不标记 submit_started
    ctx3, _ = make_ctx(video_env)
    ctx3.timeout = mod.VIDEO_TIMEOUT_MARGIN + mod.VIDEO_MIN_BUDGET - 1
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx3).publish(video_post(media))
    assert info.value.code is ErrorCode.TIMEOUT and not ctx3.submit_started and len(timeouts) == 2


async def test_publish_video_token_precheck(video_env, media: Path, monkeypatch, fake_bili):
    calls: list[Any] = []
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, calls))
    fake_bili.token_ok = False
    ctx, _ = make_ctx(video_env)
    with pytest.raises(LoginRequired, match="令牌已失效"):
        await BilibiliAdapter(ctx).publish(video_post(media))
    assert not ctx.submit_started and not calls  # 还没启动 biliup
    oauth = fake_bili.by_path("/x/passport-login/oauth2/info")[-1]
    assert oauth.url.params["appkey"] == bh.TV_APPKEY

    # 预检网络不通：不拦，交给 biliup
    def offline(request):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(bh, "make_client",
                        functools.partial(bh.make_client.func, transport=httpx.MockTransport(offline)))
    ctx2, events = make_ctx(video_env)
    result = await BilibiliAdapter(ctx2).publish(video_post(media))
    assert result.status is Status.SUBMITTED and calls
    assert any("交给 biliup" in e.get("message", "") for e in events)


async def test_publish_video_through_worker(video_env, media: Path, monkeypatch):
    from multipub.worker import run_job

    monkeypatch.setattr(bb, "run_streaming", fake_run(["Error: boom"], 1, []))
    job = {"action": "publish", "platform": "bilibili", "account": "main", "home": str(video_env.home),
           "headless": True, "run_dir": str(video_env.runs_dir / "w"), "post": video_post(media).to_dict()}
    result = await run_job(job, emit=lambda e: None)
    assert result.status is Status.FAILED and result.error_code == "platform_error"
    assert not result.submit_started and not result.retryable  # 没传完就失败：确定没投稿
    assert "尚未投稿" in result.message and "--force" not in result.message


async def test_publish_video_preflight_blocks_before_submit(video_env, media: Path, monkeypatch, fake_bili):
    """biliup 起不来（缺 VC++ 运行库）：标记 submit_started 之前就报错，不启动上传、不算已投稿。"""
    calls: list[Any] = []
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, calls))
    probes = fake_subprocess_run(monkeypatch, DLL_NOT_FOUND)
    ctx, events = make_ctx(video_env)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx).publish(video_post(media))
    exc = info.value
    assert exc.code is ErrorCode.INTERNAL and not exc.retryable
    assert "VCRUNTIME140.dll" in str(exc) and "vc_redist.x64.exe" in str(exc) and "尚未投稿" in str(exc)
    assert not ctx.submit_started and not calls and "submit_started" not in [e["event"] for e in events]
    assert probes[0][0] == [video_env.biliup_path, "--version"]
    assert not fake_bili.by_path("/x/passport-login/oauth2/info")  # 也没去做令牌预检

    # --version 卡住（杀毒软件扫描等）：timeout、可重试、没开始投稿
    def hang(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(bb.subprocess, "run", hang)
    ctx2, _ = make_ctx(video_env)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx2).publish(video_post(media))
    assert info.value.code is ErrorCode.TIMEOUT and info.value.retryable and not ctx2.submit_started and not calls

    # 能启动：同一进程里第二次发布不再试运行
    ok_probes = fake_subprocess_run(monkeypatch, 0, stdout=b"biliup-cli 1.3.1\n")
    for _ in range(2):
        ctx3, _ = make_ctx(video_env)
        assert (await BilibiliAdapter(ctx3).publish(video_post(media))).status is Status.SUBMITTED
    assert len(calls) == 2 and len(ok_probes) == 1


async def test_publish_video_preflight_through_worker(video_env, media: Path, monkeypatch):
    from multipub.worker import run_job

    calls: list[Any] = []
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, calls))
    fake_subprocess_run(monkeypatch, DLL_NOT_FOUND)
    job = {"action": "publish", "platform": "bilibili", "account": "main", "home": str(video_env.home),
           "headless": True, "run_dir": str(video_env.runs_dir / "w"), "post": video_post(media).to_dict()}
    result = await run_job(job, emit=lambda e: None)
    assert result.status is Status.FAILED and result.error_code == "internal"
    assert not result.submit_started and not calls
    assert "VCRUNTIME140.dll" in result.message and "https://aka.ms/vs/17/release/vc_redist.x64.exe" in result.message


@pytest.mark.parametrize("rc", [DLL_NOT_FOUND, -1073741515])
async def test_publish_video_startup_failure_exit_code(video_env, media: Path, monkeypatch, rc):
    """试运行通过但真正上传时 biliup 起不来（退出码 0xC0000135、没有输出）：给出运行库提示和「尚未投稿」。"""
    monkeypatch.setattr(bb, "run_streaming", fake_run([], rc, []))
    ctx, _ = make_ctx(video_env)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx).publish(video_post(media))
    exc = info.value
    assert exc.code is ErrorCode.INTERNAL and "VCRUNTIME140.dll" in str(exc) and "尚未投稿" in str(exc)
    assert not ctx.submit_started  # 进程根本没起来，确定没投稿


async def test_publish_video_requires_login_and_biliup(settings, media: Path):
    ctx, _ = make_ctx(settings)
    with pytest.raises(LoginRequired, match="mpub login bilibili --account main"):
        await BilibiliAdapter(ctx).publish(video_post(media))
    write_json(settings.account_file("bilibili", "main"), biliup_login(token=False))
    with pytest.raises(LoginRequired, match="token_info"):
        await BilibiliAdapter(ctx).publish(video_post(media))
    write_json(settings.account_file("bilibili", "main"), biliup_login())
    with pytest.raises(PublishError, match="mpub setup"):
        await BilibiliAdapter(ctx).publish(video_post(media))
    assert not ctx.submit_started


async def test_publish_revalidates(video_env, media: Path):
    ctx, _ = make_ctx(video_env)
    with pytest.raises(ValidationFailed):
        await BilibiliAdapter(ctx).publish(video_post(media, options={}))


# ------------------------------------------------------------------ validate

def test_validate_video_rules(media: Path):
    ok = BilibiliAdapter.validate(video_post(media))
    assert not errors_of(ok)
    bad = video_post(media, title="长" * 81, tags=["t"] * 13 + ["超" * 21], body="",
                     options={"copyright": 2, "line": "nope", "submit": "x", "creation_statement": 9,
                              "unknown_key": 1})
    issues = BilibiliAdapter.validate(bad)
    assert {"title", "tags", "platforms.bilibili.tid", "platforms.bilibili.source", "platforms.bilibili.line",
            "platforms.bilibili.submit", "platforms.bilibili.creation_statement"} <= errors_of(issues)
    assert {"body", "platforms.bilibili.unknown_key"} <= warnings_of(issues)
    assert "tags" in errors_of(BilibiliAdapter.validate(video_post(media, tags=[])))
    assert "tags" in errors_of(BilibiliAdapter.validate(video_post(media, tags=["a,b"])))
    assert "platforms.bilibili.tid" in errors_of(BilibiliAdapter.validate(video_post(media, options={"tid": "abc"})))
    assert not errors_of(BilibiliAdapter.validate(video_post(media, options={"tid": "231"})))
    cover = media / "cover.webp"
    cover.write_bytes(b"x")
    assert "cover" in errors_of(BilibiliAdapter.validate(video_post(media, cover=cover)))
    assert "ai_generated" in warnings_of(BilibiliAdapter.validate(video_post(media, ai_generated=True)))
    # ai_generated 但显式设了非 AI 的创作声明：提醒不会声明 AI
    for value in (-1, 2, "4"):
        issues = BilibiliAdapter.validate(video_post(media, ai_generated=True,
                                                     options={"tid": 231, "creation_statement": value}))
        assert "platforms.bilibili.creation_statement" in warnings_of(issues) and not errors_of(issues)
    assert not warnings_of(BilibiliAdapter.validate(
        video_post(media, ai_generated=True, options={"tid": 231, "creation_statement": 1})))
    # 精选评论 / 关闭评论 / 关闭弹幕只在 app 接口生效
    web = BilibiliAdapter.validate(video_post(media, options={"tid": 231, "submit": "web", "close_reply": True}))
    assert "platforms.bilibili.submit" in warnings_of(web) and not errors_of(web)
    for opts in ({"tid": 231, "submit": "app", "close_reply": True}, {"tid": 231, "close_danmu": True},
                 {"tid": 231, "submit": "b-cut-android", "is_only_self": True}):
        assert "platforms.bilibili.submit" not in warnings_of(BilibiliAdapter.validate(video_post(media, options=opts)))
    assert "platforms.bilibili.submit" in warnings_of(BilibiliAdapter.validate(
        video_post(media, options={"tid": 231, "submit": "b-cut-android", "selection_reply": True})))


@pytest.mark.parametrize("delta,level", [
    (timedelta(hours=1), "error"), (timedelta(days=16), "error"),
    (timedelta(hours=2, minutes=10), "warning"), (timedelta(days=3), None),
])
def test_validate_video_schedule(media: Path, delta, level):
    issues = [i for i in BilibiliAdapter.validate(video_post(media, schedule=datetime.now() + delta))
              if i.field == "schedule"]
    assert (issues[0].level if issues else None) == level


def test_validate_note_rules(media: Path):
    assert not errors_of(BilibiliAdapter.validate(note_post(media)))
    many = [media / "images" / "1.jpg"] * 10
    gif = media / "images" / "a.webp"
    gif.write_bytes(b"x")
    issues = BilibiliAdapter.validate(note_post(media, title="标" * 21, body="字" * 995, images=many + [gif],
                                                cover=media / "cover.jpg",
                                                options={"mode": "api", "pic_mode": 3, "tid": 1}))
    assert {"title", "body", "images", "platforms.bilibili.mode", "platforms.bilibili.pic_mode"} <= errors_of(issues)
    assert {"cover", "platforms.bilibili.tid"} <= warnings_of(issues)
    # 正文 995 字本身不超，但加上 #徒步# #周末# 后超过 1000
    assert "body" not in errors_of(BilibiliAdapter.validate(note_post(media, body="字" * 995, tags=[])))
    assert "platforms.bilibili.confirm_rules" in errors_of(
        BilibiliAdapter.validate(note_post(media, options={"confirm_rules": "yes"})))
    assert not errors_of(BilibiliAdapter.validate(note_post(media, options={"confirm_rules": True})))
    big = media / "images" / "big.png"
    big.write_bytes(b"\0" * (20 * 1024 * 1024 + 1))
    assert "images" in errors_of(BilibiliAdapter.validate(note_post(media, images=[big])))
    # 与发布框的本地校验一致：floor(大小/1MB) >= 上限就拒绝；GIF 上限 8MB
    big.write_bytes(b"\0" * (20 * 1024 * 1024))
    assert "images" in errors_of(BilibiliAdapter.validate(note_post(media, images=[big])))
    anim = media / "images" / "anim.gif"
    anim.write_bytes(b"\0" * (8 * 1024 * 1024))
    assert "images" in errors_of(BilibiliAdapter.validate(note_post(media, images=[anim])))
    anim.write_bytes(b"\0" * (8 * 1024 * 1024 - 1))
    assert "images" not in errors_of(BilibiliAdapter.validate(note_post(media, images=[anim])))


@pytest.mark.parametrize("delta,level", [
    (timedelta(minutes=3), "error"), (timedelta(days=8), "error"),
    (timedelta(minutes=8), "warning"), (timedelta(hours=5), None),
])
def test_validate_note_schedule(media: Path, delta, level):
    issues = [i for i in BilibiliAdapter.validate(note_post(media, schedule=datetime.now() + delta))
              if i.field == "schedule"]
    assert (issues[0].level if issues else None) == level


def test_note_text_tags():
    p = PlatformPost(platform="bilibili", account="a", kind=Kind.NOTE, body="去爬山 #徒步#", tags=["徒步", "周末"])
    assert note_text(p) == "去爬山 #徒步#\n#周末#"
    p.options["tags_in_text"] = False
    assert note_text(p) == "去爬山 #徒步#"
    assert note_text(PlatformPost(platform="bilibili", account="a", kind=Kind.NOTE, tags=["a"])) == "#a#"


def test_adapter_module_import_is_cheap():
    code = ("import sys, multipub.platforms.bilibili as m; m.BilibiliAdapter.validate; "
            "bad=[x for x in ('httpx','patchright','segno') if x in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ------------------------------------------------------------------ HTTP 接口（MockTransport）

def nav_payload(login: bool = True) -> dict[str, Any]:
    data = {"isLogin": login, "wbi_img": {"img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                                          "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png"}}
    if login:
        data.update({"mid": 10086, "uname": "测试UP"})
    return {"code": 0 if login else -101, "message": "0", "data": data}


def season_item(season_id: int, title: str, *section_ids: int) -> dict[str, Any]:
    return {"season": {"id": season_id, "title": title},
            "sections": {"sections": [{"id": s, "title": "正片" if i == 0 else f"第{i + 1}节"}
                                      for i, s in enumerate(section_ids)]}}


class FakeBili:
    """按路径路由的假 B站接口；记录请求，便于断言。"""

    def __init__(self, ctx: JobContext | None = None):
        self.ctx = ctx
        self.requests: list[httpx.Request] = []
        self.marks: dict[str, bool] = {}
        self.nav_login = True
        self.token_ok = True
        self.create_response: httpx.Response | None = None
        self.upload_response: httpx.Response | None = None
        self.poll_codes: list[int] = []
        self.seasons: list[dict[str, Any]] = [season_item(5001, "科技小课堂", 6001),
                                              season_item(5002, "读书笔记", 6002)]
        self.archive_views: list[dict[str, Any]] = []  # 依次返回，用完后返回成功
        self.episode_codes: list[int] = []
        self.section_aids: dict[int, list[int]] = {}  # 小节 id → 稿件 aid（episodes/add 成功时加入）
        self.add_takes_effect = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.ctx is not None:
            self.marks[path] = self.ctx.submit_started
        if path == "/x/web-interface/nav":
            return httpx.Response(200, json=nav_payload(self.nav_login))
        if path == "/x/passport-login/oauth2/info":
            if not self.token_ok:
                return httpx.Response(200, json={"code": -101, "message": "账号未登录", "data": None})
            return httpx.Response(200, json={"code": 0, "data": {"mid": 10086, "access_token": "at",
                                                                 "expires_in": 100, "refresh": True}})
        if path == "/x/frontend/finger/spi":
            return httpx.Response(200, json={"code": 0, "data": {"b_3": "B3-uuid", "b_4": "B4-uuid"}})
        if path == "/x/dynamic/feed/draw/upload_bfs":
            if self.upload_response is not None:
                return self.upload_response
            n = sum(1 for r in self.requests if r.url.path == path)
            return httpx.Response(200, json={"code": 0, "data": {
                "image_url": f"http://i0.hdslb.com/bfs/new_dyn/{n}.jpg", "image_width": 1080,
                "image_height": 1440, "img_size": 123.4, "ai_gen_pic": 0}})
        if path == "/x/dynamic/feed/create/submit_check":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path == "/x/dynamic/feed/create/dyn":
            return self.create_response or httpx.Response(200, json={"code": 0, "data": {
                "dyn_id": 1, "dyn_id_str": "1234567890123456789", "dyn_type": 2}})
        if path == "/x2/creative/web/seasons":
            return httpx.Response(200, json={"code": 0, "data": {"seasons": self.seasons, "total": len(self.seasons)}})
        if path == "/x/vupre/web/archive/view":
            if self.archive_views:
                return httpx.Response(200, json=self.archive_views.pop(0))
            return httpx.Response(200, json={"code": 0, "data": {
                "archive": {"aid": 113456789012345, "state": -30}, "videos": [{"cid": 4305787}]}})
        if path == "/x2/creative/web/season/section/episodes/add":
            code = self.episode_codes.pop(0) if self.episode_codes else 0
            if not code and self.add_takes_effect:
                body = json.loads(request.content)
                self.section_aids.setdefault(body["sectionId"], []).extend(e["aid"] for e in body["episodes"])
            return httpx.Response(200, json={"code": code, "message": "0" if not code else "err", "data": None})
        if path == "/x2/creative/web/season/section":
            aids = self.section_aids.get(int(request.url.params["id"]), [])
            return httpx.Response(200, json={"code": 0, "data": {"episodes": [{"aid": a} for a in aids]}})
        if path.endswith("/qrcode/auth_code"):
            n = sum(1 for r in self.requests if r.url.path == path)
            return httpx.Response(200, json={"code": 0, "data": {
                "url": f"https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/auth?auth_code=c{n}",
                "auth_code": f"c{n}"}})
        if path.endswith("/qrcode/poll"):
            code = self.poll_codes.pop(0) if self.poll_codes else 0
            if code:
                return httpx.Response(200, json={"code": code, "message": "wait", "data": None})
            d = biliup_login()
            return httpx.Response(200, json={"code": 0, "data": {
                "mid": 10086, "access_token": "at", "refresh_token": "rt", "expires_in": 15552000,
                "token_info": d["token_info"], "cookie_info": d["cookie_info"], "sso": d["sso"]}})
        return httpx.Response(404, json={"code": -404})

    def by_path(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture
def fake_bili(monkeypatch):
    fake = FakeBili()
    monkeypatch.setattr(bh, "make_client", functools.partial(bh.make_client, transport=httpx.MockTransport(fake)))
    return fake


@pytest.fixture
def note_env(settings, fake_bili):
    write_json(settings.account_file("bilibili", "main"), biliup_login(), bom=True)
    ctx, events = make_ctx(settings)
    fake_bili.ctx = ctx
    return ctx, events, fake_bili


async def test_http_note_flow(note_env, media: Path):
    ctx, events, fake = note_env
    when = datetime.now() + timedelta(hours=3)
    post = note_post(media, options={"mode": "http", "pic_mode": 1}, ai_generated=True, schedule=when)
    result = await BilibiliAdapter(ctx).publish(post)
    assert result.status is Status.SCHEDULED
    assert result.url == "https://www.bilibili.com/opus/1234567890123456789"
    assert result.item_id == "1234567890123456789" and result.submit_started

    uploads = fake.by_path("/x/dynamic/feed/draw/upload_bfs")
    assert len(uploads) == 2
    body = uploads[0].content
    for part in (b'name="file_up"; filename="1.jpg"', b'name="biz"\r\n\r\ndyn', b'name="category"\r\n\r\ndaily',
                 b'name="csrf"\r\n\r\ncsrf123'):
        assert part in body
    assert "SESSDATA=sess%2Cabc" in uploads[0].headers["cookie"]
    assert "buvid3=B3-uuid" in uploads[0].headers["cookie"]  # spi 补上的

    create = fake.by_path("/x/dynamic/feed/create/dyn")[0]
    params = dict(create.url.params)
    req = json.loads(create.content)["dyn_req"]
    assert params["platform"] == "web" and params["csrf"] == "csrf123"
    assert json.loads(params["x-bili-device-req-json"]) == {"platform": "web", "device": "pc", "spmid": "333.1365",
                                                            "mobi_app": "web_cn"}
    # 网页端 WBI 中间件的 bodyPayload：body 里的 upload_id / meta 拷进 URL（meta 发 JSON）
    assert params["w_dyn_req.upload_id"] == req["upload_id"]
    assert json.loads(params["w_dyn_req.meta"]) == req["meta"]
    w_rid = params.pop("w_rid")
    # 浏览器签名时对对象直接 encodeURIComponent(obj)，即 "[object Object]"
    assert w_rid == independent_wbi({**params, "w_dyn_req.meta": "[object Object]"}, IMG_KEY, SUB_KEY)
    assert req["scene"] == 2 and req["content"]["title"] == "周末徒步"
    assert req["content"]["contents"] == [{"raw_text": "正文第一行\n第二行\n#徒步# #周末#", "type": 1, "biz_id": ""}]
    assert [p["img_src"] for p in req["pics"]] == ["http://i0.hdslb.com/bfs/new_dyn/1.jpg",
                                                   "http://i0.hdslb.com/bfs/new_dyn/2.jpg"]
    # img_size 与网页端一致：本地文件大小 / 1024（KB），ai_gen_pic 原样带上
    size_kb = (media / "images" / "1.jpg").stat().st_size / 1024
    assert req["pics"][0] == {"img_src": "http://i0.hdslb.com/bfs/new_dyn/1.jpg", "img_width": 1080,
                              "img_height": 1440, "img_size": size_kb, "ai_gen_pic": 0}
    check_req = fake.by_path("/x/dynamic/feed/create/submit_check")[0]
    assert json.loads(check_req.url.params["x-bili-device-req-json"]) == {"platform": "web", "device": "pc",
                                                                          "spmid": "333.1365"}
    assert req["option"] == {"aigc": 1, "pic_mode": 1, "timer_pub_time": int(when.timestamp())}
    assert req["upload_id"].startswith("10086_")
    assert req["meta"] == {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}}
    check = json.loads(fake.by_path("/x/dynamic/feed/create/submit_check")[0].content)
    assert check["create_option"] == req["option"]
    # 只有 create/dyn 在 submit_started 之后
    assert fake.marks["/x/dynamic/feed/draw/upload_bfs"] is False
    assert fake.marks["/x/dynamic/feed/create/submit_check"] is False
    assert fake.marks["/x/dynamic/feed/create/dyn"] is True
    assert [e["event"] for e in events].count("submit_started") == 1


async def test_http_note_immediate_without_ai(note_env, media: Path):
    ctx, _, fake = note_env
    result = await BilibiliAdapter(ctx).publish(note_post(media, options={"mode": "http"}))
    assert result.status is Status.PUBLISHED
    req = json.loads(fake.by_path("/x/dynamic/feed/create/dyn")[0].content)["dyn_req"]
    assert req["option"] == {"aigc": 2, "pic_mode": 0}


@pytest.mark.parametrize("response,exc,code,started", [
    (httpx.Response(412, text="blocked"), NeedsAction, ErrorCode.RISK_CONTROL, True),
    (httpx.Response(200, json={"code": -352, "message": "风控校验失败"}), NeedsAction, ErrorCode.RISK_CONTROL, True),
    (httpx.Response(200, json={"code": 4126021, "message": "未绑定手机"}), PublishError, ErrorCode.PLATFORM_ERROR,
     True),
    (httpx.Response(200, json={"code": -101, "message": "账号未登录"}), LoginRequired, ErrorCode.LOGIN_REQUIRED,
     True),
    (httpx.Response(502, text="<html>bad gateway</html>"), PublishError, ErrorCode.UNKNOWN_OUTCOME, True),
    (httpx.Response(200, json={"code": -504, "message": "服务调用超时"}), PublishError, ErrorCode.UNKNOWN_OUTCOME,
     True),
    (httpx.Response(200, json=[1, 2]), PublishError, ErrorCode.UNKNOWN_OUTCOME, True),
])
async def test_http_note_create_errors(note_env, media: Path, response, exc, code, started):
    ctx, _, fake = note_env
    fake.create_response = response
    with pytest.raises(exc) as info:
        await BilibiliAdapter(ctx).publish(note_post(media, options={"mode": "http"}))
    assert info.value.code is code and ctx.submit_started is started
    if code is ErrorCode.PLATFORM_ERROR:
        assert "绑定手机" in str(info.value)


async def test_http_note_upload_412_before_submit(note_env, media: Path):
    ctx, _, fake = note_env
    fake.upload_response = httpx.Response(412)
    with pytest.raises(NeedsAction) as info:
        await BilibiliAdapter(ctx).publish(note_post(media, options={"mode": "http"}))
    assert info.value.code is ErrorCode.RISK_CONTROL and "浏览器模式" in str(info.value)
    assert not ctx.submit_started
    assert not fake.by_path("/x/dynamic/feed/create/dyn")


async def test_http_note_network_error_after_submit(settings, media: Path, monkeypatch):
    write_json(settings.account_file("bilibili", "main"), biliup_login())
    fake = FakeBili()

    def handler(request):
        if request.url.path == "/x/dynamic/feed/create/dyn":
            raise httpx.ReadTimeout("slow")
        return fake(request)

    monkeypatch.setattr(bh, "make_client", functools.partial(bh.make_client, transport=httpx.MockTransport(handler)))
    ctx, _ = make_ctx(settings)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx).publish(note_post(media, options={"mode": "http"}))
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME and ctx.submit_started


async def test_note_login_expired_renews_via_biliup(note_env, media: Path, monkeypatch):
    ctx, _, fake = note_env
    fake.nav_login = False
    fake_exe = ctx.settings.home / "biliup-fake"
    fake_exe.write_bytes(b"")
    ctx.settings.biliup_path = str(fake_exe)
    renewed: list[Path] = []

    async def renew(exe, cookie_file, *, cwd, timeout=90):
        renewed.append(cookie_file)
        fake.nav_login = True
        return True, "登录凭据刷新成功"

    monkeypatch.setattr(bb, "renew", renew)
    result = await BilibiliAdapter(ctx).publish(note_post(media, options={"mode": "http"}))
    assert result.status is Status.PUBLISHED and renewed == [ctx.account_file]


async def test_note_login_expired_without_biliup(note_env, media: Path):
    ctx, _, fake = note_env
    fake.nav_login = False
    with pytest.raises(LoginRequired, match="mpub login bilibili"):
        await BilibiliAdapter(ctx).publish(note_post(media, options={"mode": "http"}))
    assert not ctx.submit_started


# ------------------------------------------------------------------ check / login

async def test_check(settings, fake_bili):
    ctx, _ = make_ctx(settings, action="check")
    with pytest.raises(LoginRequired, match="mpub login bilibili --account main"):
        await BilibiliAdapter(ctx).check()
    write_json(settings.account_file("bilibili", "main"), biliup_login(), bom=True)
    result = await BilibiliAdapter(ctx).check()
    assert result.status is Status.OK and "测试UP" in result.message and "天后过期" in result.message
    assert "令牌有效" in result.message and "自动续期" in result.message
    nav_req = fake_bili.by_path("/x/web-interface/nav")[-1]
    assert "SESSDATA=sess%2Cabc" in nav_req.headers["cookie"]
    assert not fake_bili.by_path("/x/frontend/finger/spi")
    # 与 biliup validate_tokens 相同的只读校验：TV appkey + APP 签名
    oauth = dict(fake_bili.by_path("/x/passport-login/oauth2/info")[-1].url.params)
    assert oauth["appkey"] == bh.TV_APPKEY and oauth["access_key"] == "at" and oauth["actionKey"] == "appkey"
    sign = oauth.pop("sign")
    assert bh.app_sign(oauth)["sign"] == sign
    fake_bili.token_ok = False
    with pytest.raises(LoginRequired, match="令牌已失效"):
        await BilibiliAdapter(ctx).check()
    fake_bili.token_ok = True
    fake_bili.nav_login = False
    with pytest.raises(LoginRequired):
        await BilibiliAdapter(ctx).check()
    android = biliup_login()
    android["platform"] = "Weird"
    write_json(settings.account_file("bilibili", "main"), android)
    fake_bili.nav_login = True
    with pytest.raises(LoginRequired, match="platform"):
        await BilibiliAdapter(ctx).check()
    write_json(settings.account_file("bilibili", "main"), biliup_login(token=False))
    with pytest.raises(LoginRequired, match="token_info"):
        await BilibiliAdapter(ctx).check()


async def test_check_risk_control(settings, monkeypatch):
    write_json(settings.account_file("bilibili", "main"), biliup_login())
    monkeypatch.setattr(bh, "make_client", functools.partial(
        bh.make_client, transport=httpx.MockTransport(lambda r: httpx.Response(412))))
    ctx, _ = make_ctx(settings, action="check")
    with pytest.raises(NeedsAction) as info:
        await BilibiliAdapter(ctx).check()
    assert info.value.code is ErrorCode.RISK_CONTROL


async def test_tv_qr_login(settings, fake_bili, monkeypatch):
    import multipub.platforms.bilibili as mod

    monkeypatch.setattr(mod, "LOGIN_POLL_SECONDS", 0)
    fake_bili.poll_codes = [86039, 86090, 86038, 86039, 0]
    ctx, events = make_ctx(settings, action="login")
    result = await BilibiliAdapter(ctx).login()
    assert result.status is Status.OK and "测试UP" in result.message
    qrs = [e for e in events if e["event"] == "qrcode"]
    assert len(qrs) == 2 and "TV" in qrs[0]["note"] and "过期" in qrs[1]["note"]
    assert not Path(qrs[0]["path"]).exists()  # 登录成功后删除二维码
    saved = json.loads(ctx.account_file.read_text(encoding="utf-8"))
    assert saved["platform"] == "BiliTV" and saved["token_info"]["refresh_token"] == "rt"
    polls = fake_bili.by_path("/x/passport-tv-login/qrcode/poll")
    form = urllib.parse.parse_qs(polls[0].content.decode())
    assert form["appkey"] == [bh.TV_APPKEY] and form["auth_code"] == ["c1"] and len(form["sign"][0]) == 32
    assert urllib.parse.parse_qs(polls[-1].content.decode())["auth_code"] == ["c2"]
    assert bc.load_credential(ctx.account_file).has_token
    # 已登录时再次 login 不重新扫码
    events.clear()
    again = await BilibiliAdapter(ctx).login()
    assert again.status is Status.OK and "无需重新扫码" in again.message
    assert not [e for e in events if e["event"] == "qrcode"]
    # 网页 cookie 有效但 biliup 令牌失效：重新扫码（避免「请登录」/「已登录」死循环）
    fake_bili.token_ok = False
    events.clear()
    fake_bili.poll_codes = [0]
    third = await BilibiliAdapter(ctx).login()
    assert third.status is Status.OK and "登录成功" in third.message
    assert [e for e in events if e["event"] == "qrcode"]


async def test_tv_qr_login_failure_code(settings, fake_bili, monkeypatch):
    import multipub.platforms.bilibili as mod

    monkeypatch.setattr(mod, "LOGIN_POLL_SECONDS", 0)
    fake_bili.poll_codes = [-3]
    ctx, _ = make_ctx(settings, action="login")
    with pytest.raises(PublishError, match="-3"):
        await BilibiliAdapter(ctx).login()
    assert not ctx.account_file.exists()


def test_interactive_login_requires_tty(settings, monkeypatch, capsys):
    fake = settings.home / "biliup-fake"
    fake.write_bytes(b"")
    settings.biliup_path = str(fake)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert BilibiliAdapter.run_interactive_login(settings, "main") == 2
    assert "login" in capsys.readouterr().err


def test_interactive_login_reports_missing_vcruntime(settings, monkeypatch, capsys):
    fake = settings.home / "biliup-fake.exe"
    fake.write_bytes(b"")
    settings.biliup_path = str(fake)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    calls = fake_subprocess_run(monkeypatch, DLL_NOT_FOUND)
    assert BilibiliAdapter.run_interactive_login(settings, "main") == 1
    assert "VCRUNTIME140.dll" in capsys.readouterr().err
    assert [args for args, _ in calls] == [[str(fake), "--version"]]  # 没有启动 login


# ------------------------------------------------------------------ 浏览器模式的纯逻辑部分

class _FakeResp:
    def __init__(self, url: str, status: int, data: Any):
        self.url, self.status, self._data = url, status, data

    async def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


async def test_browser_net_tracker_and_result_mapping(settings):
    from multipub.platforms.bili_note import BrowserNotePublisher

    ctx, _ = make_ctx(settings)
    pub = BrowserNotePublisher(ctx, account="main")
    api = "https://api.bilibili.com"
    await pub.net.on_response(_FakeResp(f"{api}/x/dynamic/feed/draw/upload_bfs", 200, {"code": 0}))
    await pub.net.on_response(_FakeResp(f"{api}/x/dynamic/feed/draw/upload_bfs", 200, {"code": 1, "message": "x"}))
    await pub.net.on_response(_FakeResp(f"{api}/x/web-interface/nav", 200, {"code": 0}))
    assert pub.net.uploaded == 1 and pub.net.upload_errors == ["1: x"]
    await pub.net.on_response(_FakeResp(f"{api}/x/dynamic/feed/create/dyn?w_rid=1", 412, ValueError("html")))
    assert pub.net.created_status == 412 and pub.net.created["code"] is None
    with pytest.raises(NeedsAction):
        pub._parse_created(pub.net.created)
    assert pub._parse_created({"code": 0, "data": {"dyn_id_str": "99"}})["dyn_id_str"] == "99"
    pub.net.created_status = 200
    with pytest.raises(PublishError) as info:
        pub._parse_created({"code": 0, "data": {}})
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME
    with pytest.raises(PublishError, match="绑定手机"):
        pub._parse_created({"code": 4126021, "message": "x"})
    with pytest.raises(PublishError) as info:
        pub._parse_created({"code": 12345, "message": "内容违规"})
    assert info.value.code is ErrorCode.PLATFORM_ERROR
    with pytest.raises(PublishError) as info:
        pub._parse_created({"code": -504, "message": "服务调用超时"})
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME


class _FakeLocator:
    def __init__(self, page: "_FakePage", key: str):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    def locator(self, sel: str, has_text: str | None = None):
        return _FakeLocator(self.page, f"{self.key} {sel}" + (f"[{has_text}]" if has_text else ""))

    def get_by_text(self, text: str, exact: bool = False):
        return _FakeLocator(self.page, f"{self.key} text={text}")

    async def is_visible(self) -> bool:
        return self.page.is_visible(self.key)

    async def count(self) -> int:
        return int(self.page.is_visible(self.key))

    async def click(self, timeout: float | None = None) -> None:
        self.page.clicks.append(self.key)
        self.page.on_click(self.key)

    async def get_attribute(self, name: str) -> str:
        return ""


class _FakeContext:
    def __init__(self, cookies: list[dict]):
        self._cookies = cookies

    async def cookies(self, urls=None):
        return list(self._cookies)


class _FakePage:
    """只模拟 bili_note 用到的那点 Page API：可见元素集合、点击回调、cookie。"""

    def __init__(self, visible: set[str] | None = None, cookies: list[dict] | None = None):
        self.visible = set(visible or ())
        self.clicks: list[str] = []
        self.context = _FakeContext(cookies or [])
        self.url = "https://t.bilibili.com/"
        self.on_click = lambda key: None
        self.ticks = 0
        self.on_tick = lambda page: None
        self.eval_result: Any = {"ok": True}
        self.evaluated: list[dict] = []

    def locator(self, sel: str, has_text: str | None = None):
        if sel == ".bili-dyn-specification-popup":  # 每次轮询都会查一次弹窗：借此推进"时间"
            self.ticks += 1
            self.on_tick(self)
        return _FakeLocator(self, sel + (f"[{has_text}]" if has_text else ""))

    def is_visible(self, key: str) -> bool:
        return key in self.visible

    async def goto(self, url: str, **kw) -> None:
        self.url = url

    async def evaluate(self, script: str, args: Any = None, isolated_context: bool = True) -> Any:
        self.evaluated.append(args)
        return self.eval_result


SPEC_BTN = ".bili-dyn-specification-popup .bili-dyn-specification-popup__btn[确认并发送]"


async def test_wait_publisher_stops_early(settings, monkeypatch):
    """等发布框时：跳到登录页 / 页头登录按钮持续出现 / 整页风控都立即停下，不干等 30 秒；登录按钮一闪而过不算。"""
    from multipub.platforms import bili_note as bn

    monkeypatch.setattr(bn, "LOGIN_ENTRY_GRACE", 0.5)
    ctx, _ = make_ctx(settings)
    pub = bn.BrowserNotePublisher(ctx, account="main")

    page = _FakePage()
    page.url = "https://passport.bilibili.com/login?gourl=x"
    with pytest.raises(LoginRequired, match="登录页"):
        await pub._wait_publisher(page)

    page = _FakePage(visible={".header-login-entry"})
    t0 = time.monotonic()
    with pytest.raises(LoginRequired):
        await pub._wait_publisher(page)
    assert time.monotonic() - t0 < 5

    page = _FakePage(visible={"text=安全风控策略"})
    with pytest.raises(NeedsAction) as info:
        await pub._wait_publisher(page)
    assert info.value.code is ErrorCode.RISK_CONTROL

    # 登录按钮出现一下就变成发布框：正常继续
    page = _FakePage(visible={".header-login-entry"})

    async def later():
        await asyncio.sleep(0.2)
        page.visible = {".bili-dyn-publishing"}

    task = asyncio.create_task(later())
    await pub._wait_publisher(page)
    await task

    monkeypatch.setattr(bn, "PUBLISHER_WAIT", 0.5)
    with pytest.raises(PublishError, match="改版"):
        await pub._wait_publisher(_FakePage())


@pytest.fixture
def fast_note(monkeypatch):
    from multipub.platforms import bili_note as bn

    monkeypatch.setattr(bn, "POLL_SECONDS", 0)
    monkeypatch.setattr(bn, "SPEC_CANCEL_GRACE", 0)
    return bn


async def test_spec_popup_confirmed_with_consent(settings, fast_note):
    ctx, events = make_ctx(settings)
    pub = fast_note.BrowserNotePublisher(ctx, account="main", spec_consent=True)
    page = _FakePage(visible={".bili-dyn-specification-popup", SPEC_BTN})

    def clicked(key):
        assert key == SPEC_BTN
        page.visible.clear()
        pub.net.check_sent = pub.net.create_sent = True
        pub.net.created = {"code": 0, "data": {"dyn_id_str": "777"}}

    page.on_click = clicked
    data = await pub._wait_created(page)
    assert data["dyn_id_str"] == "777" and page.clicks == [SPEC_BTN]
    assert any("confirm_dynamic_rules" in e.get("message", "") for e in events)


async def test_spec_popup_human_click_is_remembered(settings, fast_note):
    ctx, events = make_ctx(settings)
    ctx.headless = False
    marker = settings.accounts_dir / "bilibili" / "main.dy_spec_agreed"
    pub = fast_note.BrowserNotePublisher(ctx, account="main", spec_marker=marker)
    page = _FakePage(visible={".bili-dyn-specification-popup", SPEC_BTN})

    def tick(p):
        if p.ticks == 3:  # 用户在第 3 次轮询前点了确认
            p.visible.clear()
            pub.net.check_sent = pub.net.create_sent = True
            pub.net.created = {"code": 0, "data": {"dyn_id_str": "888"}}

    page.on_tick = tick
    data = await pub._wait_created(page)
    assert data["dyn_id_str"] == "888" and not page.clicks  # 没有代点
    assert marker.is_file()
    assert any("确认并发送" in e.get("message", "") for e in events)


async def test_spec_popup_cancelled_means_not_sent(settings, fast_note):
    ctx, _ = make_ctx(settings)
    ctx.headless = False
    pub = fast_note.BrowserNotePublisher(ctx, account="main")
    page = _FakePage(visible={".bili-dyn-specification-popup"})
    page.on_tick = lambda p: p.visible.clear() if p.ticks >= 2 else None
    with pytest.raises(PublishError, match="没有发出") as info:
        await pub._wait_created(page)
    assert info.value.code is ErrorCode.PLATFORM_ERROR


async def test_spec_needs_consent_in_headless_before_submit(settings, fast_note):
    ctx, _ = make_ctx(settings)
    pub = fast_note.BrowserNotePublisher(ctx, account="main")
    page = _FakePage(visible={".bili-dyn-publishing"})
    with pytest.raises(NeedsAction, match="confirm_dynamic_rules") as info:
        await pub._run(page, title="t", text="x", images=[], timer_pub_time=None, aigc=False, pic_mode=None)
    assert not ctx.submit_started and info.value.code is ErrorCode.CONSENT_REQUIRED
    # 已同意（cookie 为 1）就不拦
    agreed = _FakePage(visible={".bili-dyn-publishing"}, cookies=[{"name": "dy_spec_agreed", "value": "1"}])
    assert await pub._spec_agreed(agreed)
    assert not await pub._spec_agreed(page)


class _FakeReq:
    def __init__(self, url: str, body: Any, method: str = "POST"):
        self.url, self.method = url, method
        self._body = body

    @property
    def post_data(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body if isinstance(self._body, (str, type(None))) else json.dumps(self._body)


CREATE_URL = "https://api.bilibili.com/x/dynamic/feed/create/dyn?platform=web&w_rid=1"
LAUNCHER = ".bili-dyn-publishing__action.launcher"


def browser_pub(bn, settings, *, consent: bool = False, headless: bool = True):
    """BrowserNotePublisher + 假页面：填写/传图步骤换成空操作，只走发布按钮之后的流程。"""
    ctx, events = make_ctx(settings)
    ctx.headless = headless
    pub = bn.BrowserNotePublisher(ctx, account="main", spec_consent=consent)

    async def noop(*a, **kw):
        return None

    pub._fill_title = pub._fill_text = pub._upload_images = noop
    cookies = [{"name": "dy_spec_agreed", "value": "1"}] if consent else []
    page = _FakePage(visible={".bili-dyn-publishing", LAUNCHER}, cookies=cookies)
    return ctx, events, pub, page


def send_create(pub, option: dict | None, dyn_id: str = "555") -> None:
    pub.net.on_request(_FakeReq(CREATE_URL, {"dyn_req": {"scene": 2, "option": option}} if option is not None else "x"))
    pub.net.created = {"code": 0, "data": {"dyn_id_str": dyn_id}}


async def test_spec_popup_still_shown_with_consent_through_run(settings, fast_note):
    """已同意并带了 dy_spec_agreed，B站仍弹规范框：代为点「确认并发送」，而不是干等 60 秒报结果未知。"""
    ctx, events, pub, page = browser_pub(fast_note, settings, consent=True)

    def clicked(key):
        if key == LAUNCHER:
            page.visible |= {".bili-dyn-specification-popup", SPEC_BTN}
        elif key == SPEC_BTN:
            page.visible -= {".bili-dyn-specification-popup", SPEC_BTN}
            send_create(pub, {"aigc": 2})

    page.on_click = clicked
    data = await pub._run(page, title="", text="", images=[], timer_pub_time=None, aigc=False, pic_mode=None)
    assert data["dyn_id_str"] == "555" and page.clicks == [LAUNCHER, SPEC_BTN]
    assert ctx.submit_started and not pub.warnings and pub.scheduled_at is None


async def test_unexpected_spec_popup_without_consent_headless(settings, fast_note):
    """没同意、无头，却在点发布后弹了规范框（如定时）：没人能点，明确报 consent_required、动态没发出。"""
    ctx, _, pub, page = browser_pub(fast_note, settings)
    page.eval_result = {"ok": True, "timing": True}
    page.visible.add(".bili-dyn-publishing__datetime")
    page.on_click = lambda key: page.visible.add(".bili-dyn-specification-popup")
    with pytest.raises(NeedsAction) as info:
        await pub._run(page, title="", text="", images=[], timer_pub_time=int(time.time()) + 3600, aigc=False,
                       pic_mode=None)
    assert info.value.code is ErrorCode.CONSENT_REQUIRED and "尚未发出" in str(info.value)


async def test_browser_aigc_not_settable_fails_before_submit(settings, fast_note):
    for eval_result in ({"ok": False, "reason": "no-vm"}, {"ok": True, "aigc": False}, None):
        ctx, _, pub, page = browser_pub(fast_note, settings, consent=True)
        page.eval_result = eval_result
        with pytest.raises(ValidationFailed, match="AI 创作声明") as info:
            await pub._run(page, title="", text="", images=[], timer_pub_time=None, aigc=True, pic_mode=None)
        assert not ctx.submit_started and LAUNCHER not in page.clicks
        assert "mode: http" in str(info.value) and "ai_generated: false" in str(info.value)


async def test_browser_options_verified_against_sent_request(settings, fast_note):
    when = int(time.time()) + 3600
    page_cases = [
        # (请求里的 option, 期望 scheduled_at, 期望提醒里的字样)
        ({"aigc": 1, "pic_mode": 1, "timer_pub_time": when}, when, []),
        ({"aigc": 2, "pic_mode": 1, "timer_pub_time": when}, when, ["没有声明 AI"]),
        ({"aigc": 1, "pic_mode": 0}, None, ["立即发布", "图片排版"]),
        (None, when, []),  # 读不到请求体：按发布框状态，只记日志
    ]
    for option, scheduled, expect in page_cases:
        ctx, events, pub, page = browser_pub(fast_note, settings, consent=True)
        page.eval_result = {"ok": True, "timing": True, "aigc": True, "pic_mode": True}
        page.visible.add(".bili-dyn-publishing__datetime")
        page.on_click = lambda key, pub=pub, option=option: send_create(pub, option) if key == LAUNCHER else None
        data = await pub._run(page, title="", text="", images=[], timer_pub_time=when, aigc=True, pic_mode=1)
        assert data["dyn_id_str"] == "555" and page.evaluated[-1] == {"timer_ms": when * 1000, "aigc": True,
                                                                     "pic_mode": 1}
        assert pub.scheduled_at == scheduled
        assert len(pub.warnings) == len(expect) and all(any(w in x for x in pub.warnings) for w in expect)
        if option is None:
            assert any("无法核对" in e.get("message", "") for e in events)
    # 发布框没有图片排版选项：照发，但写进提醒
    ctx, _, pub, page = browser_pub(fast_note, settings, consent=True)
    page.eval_result = {"ok": True}
    page.on_click = lambda key: send_create(pub, {"aigc": 2}) if key == LAUNCHER else None
    await pub._run(page, title="", text="", images=[], timer_pub_time=None, aigc=False, pic_mode=1)
    assert any("pic_mode 未生效" in w for w in pub.warnings)


def test_net_tracker_reads_sent_option(settings):
    from multipub.platforms.bili_note import _NetTracker

    net = _NetTracker()
    net.on_request(_FakeReq(CREATE_URL, {"dyn_req": {"option": {"aigc": 1, "timer_pub_time": 1700000000}}}))
    assert net.create_sent and net.sent_option == {"aigc": 1, "timer_pub_time": 1700000000}
    for body in (None, "not json", [1], {"dyn_req": {"option": "x"}}, ValueError("binary")):
        net = _NetTracker()
        net.on_request(_FakeReq(CREATE_URL, body))
        assert net.create_sent and net.sent_option is None
    net = _NetTracker()
    net.on_request(_FakeReq(CREATE_URL, {"dyn_req": {}}, method="GET"))
    assert not net.create_sent


async def test_captcha_and_login_popup_after_click(settings, fast_note):
    bn = fast_note
    # 无头，发布请求还没发出：needs_action / captcha
    ctx, _, pub, page = browser_pub(bn, settings, consent=True)
    page.visible.add(".geetest_panel")
    with pytest.raises(NeedsAction) as info:
        await pub._wait_created(page)
    assert info.value.code is ErrorCode.CAPTCHA and "没有发出" in str(info.value)
    # 无头，发布请求已发出、还没有结果：结果未知（查重会拦住重发）
    pub.net.create_sent = True
    with pytest.raises(PublishError) as info:
        await pub._wait_created(page)
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME and not isinstance(info.value, NeedsAction)
    # 有头：等人过验证，过了就拿到结果
    ctx, events, pub, page = browser_pub(bn, settings, consent=True, headless=False)
    page.visible.add(".geetest_panel")

    def solved(p):
        if p.ticks == 3:
            p.visible.discard(".geetest_panel")
            send_create(pub, {"aigc": 2}, dyn_id="666")

    page.on_tick = solved
    assert (await pub._wait_created(page))["dyn_id_str"] == "666"
    assert any("人机验证" in e.get("message", "") for e in events)
    # .bili-mini-mask 是迷你登录框，不是验证码：没发出请求时报 login_required
    ctx, _, pub, page = browser_pub(bn, settings, consent=True)
    page.visible.add(".bili-mini-mask")
    with pytest.raises(LoginRequired, match="没有发出"):
        await pub._wait_created(page)
    # submit_check 返回 -101：同样是登录失效
    ctx, _, pub, page = browser_pub(bn, settings, consent=True)
    pub.net.check_sent, pub.net.check = True, {"code": -101, "message": "账号未登录"}
    with pytest.raises(LoginRequired):
        await pub._wait_created(page)


async def test_note_browser_result_uses_what_was_sent(note_env, media: Path, monkeypatch):
    """定时没生效（请求里没有定时）→ published + 提醒；提醒都进结果消息。"""
    from multipub.platforms import bili_note as bn

    ctx, _, _ = note_env
    state: dict[str, Any] = {}

    class StubPublisher:
        def __init__(self, ctx, *, account, spec_consent=False, spec_marker=None, publish_wait=60):
            self.warnings = state.get("warnings", [])
            self.scheduled_at = state.get("scheduled_at")

        async def publish(self, **kw):
            return {"dyn_id_str": "43"}

    monkeypatch.setattr(bn, "BrowserNotePublisher", StubPublisher)
    when = datetime.now() + timedelta(hours=3)
    state.update(warnings=["发布请求里没有定时时间，动态已经立即发布"], scheduled_at=None)
    result = await BilibiliAdapter(ctx).publish(note_post(media, schedule=when))
    assert result.status is Status.PUBLISHED and "注意：发布请求里没有定时时间" in result.message
    actual = int((when + timedelta(minutes=7)).timestamp())
    state.update(warnings=["x 不一致"], scheduled_at=actual)
    result = await BilibiliAdapter(ctx).publish(note_post(media, schedule=when))
    assert result.status is Status.SCHEDULED and f"{datetime.fromtimestamp(actual):%H:%M}" in result.message
    assert "注意：x 不一致" in result.message


async def test_http_note_rechecks_schedule_before_submit(note_env, media: Path):
    ctx, _, fake = note_env
    async with bh.make_client({"SESSDATA": "s"}) as client:
        with pytest.raises(ValidationFailed, match="不足 5 分钟"):
            await bh.publish_note_http(client, csrf="c", account="main", mid=1, keys=(IMG_KEY, SUB_KEY), title="",
                                       text="x", images=[media / "images" / "1.jpg"], aigc=False, pic_mode=0,
                                       timer_pub_time=int(time.time()) + 200, mark_submit=ctx.mark_submit_started,
                                       log=ctx.log)
    assert not ctx.submit_started and not fake.by_path("/x/dynamic/feed/create/dyn")


def test_with_spec_cookie():
    from multipub.platforms.bili_note import with_spec_cookie

    state = {"cookies": [{"name": "SESSDATA", "value": "s"}, {"name": "dy_spec_agreed", "value": "0"}], "origins": []}
    out = with_spec_cookie(state)
    spec = [c for c in out["cookies"] if c["name"] == "dy_spec_agreed"]
    assert len(spec) == 1 and spec[0]["value"] == "1" and spec[0]["domain"] == ".bilibili.com"
    assert spec[0]["expires"] > time.time() + 300 * 86400 and out["origins"] == []
    assert state["cookies"][1]["value"] == "0"  # 不改原对象


async def test_note_consent_wiring(note_env, media: Path, monkeypatch):
    """浏览器模式：配置 / 清单 / 记录文件 三种同意来源都会传给发布器。"""
    from multipub.platforms import bili_note as bn

    ctx, _, _ = note_env
    seen: list[tuple[bool, Path]] = []
    waits: list[float] = []

    class StubPublisher:
        def __init__(self, ctx, *, account, spec_consent=False, spec_marker=None, publish_wait=60):
            seen.append((spec_consent, spec_marker))
            waits.append(publish_wait)

        async def publish(self, **kw):
            assert any(c["name"] == "buvid3" for c in kw["storage_state"]["cookies"])
            return {"dyn_id_str": "42"}

    monkeypatch.setattr(bn, "BrowserNotePublisher", StubPublisher)
    adapter = BilibiliAdapter(ctx)
    marker = ctx.account_file.with_name("main.dy_spec_agreed")
    result = await adapter.publish(note_post(media))
    assert result.status is Status.PUBLISHED and result.url.endswith("/opus/42")
    assert seen[-1] == (False, marker)
    await adapter.publish(note_post(media, options={"confirm_rules": True}))
    assert seen[-1][0] is True
    ctx.settings.raw = {"platforms": {"bilibili": {"confirm_dynamic_rules": True}}}
    await adapter.publish(note_post(media))
    assert seen[-1][0] is True
    await adapter.publish(note_post(media, options={"confirm_rules": False}))  # 清单明确 false 优先
    assert seen[-1][0] is False
    ctx.settings.raw = {}
    marker.write_text("x", encoding="utf-8")
    await adapter.publish(note_post(media))
    assert seen[-1][0] is True
    # 点发布后等结果的秒数：默认 60，可在 config.toml 调（至少 10 秒）
    ctx.settings.raw = {"platforms": {"bilibili": {"publish_wait": 120}}}
    await adapter.publish(note_post(media))
    ctx.settings.raw = {"platforms": {"bilibili": {"publish_wait": 1}}}
    await adapter.publish(note_post(media))
    assert waits[0] == 60 and waits[-2:] == [120, 10]


def test_registry_and_doctor(settings):
    from multipub.platforms import load_adapter

    cls = load_adapter("bilibili")
    assert cls is BilibiliAdapter and cls.kinds == {Kind.VIDEO, Kind.NOTE} and not cls.interactive_login
    items = cls.doctor(settings)
    assert items[0]["name"] == "biliup" and not items[0]["ok"] and "mpub setup" in items[0]["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行注入脚本")
def test_vue_options_script_with_stub_dom():
    from multipub.platforms.bili_note import _VUE_OPTIONS_JS

    harness = """
const fn = %s;
function mk(visible, list) {
  const pub = {tools: {timingSetting: {visible, active: false, data: {id: 5, datetime: 0, next: false}}},
               creationOption: {value: 0, list}, picModeOption: {value: 0}, $children: []};
  return {pub, child: {$parent: pub, $children: []}};
}
const out = [];
let {pub, child} = mk(true, [{value: 0}, {value: 1}]);
globalThis.document = {querySelector: () => ({__vue__: child})};
out.push(fn({timer_ms: 1700000000000, aigc: true, pic_mode: 1}), pub.tools.timingSetting.active,
         pub.tools.timingSetting.data.datetime, pub.creationOption.value, pub.picModeOption.value);
({pub, child} = mk(false, [{value: 0}]));
globalThis.document = {querySelector: () => ({__vue__: {$children: [pub]}})};
out.push(fn({timer_ms: 1, aigc: false, pic_mode: null}), fn({timer_ms: null, aigc: true, pic_mode: null}));
globalThis.document = {querySelector: () => null};
out.push(fn({}));
console.log(JSON.stringify(out));
""" % _VUE_OPTIONS_JS.strip()
    proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out[0] == {"ok": True, "timing": True, "aigc": True, "pic_mode": True}
    assert out[1:5] == [True, 1700000000000, 1, 1]
    assert out[5] == {"ok": False, "reason": "timing-hidden"}
    assert out[6] == {"ok": True, "aigc": False}  # 没有 AI 声明选项时不勾选
    assert out[7] == {"ok": False, "reason": "no-root"}


# ------------------------------------------------------------------ 视频：投稿后加入合集

def test_validate_video_collection(media: Path):
    ok = video_post(media, options={"tid": 231, "collection": "科技小课堂"})
    assert not errors_of(BilibiliAdapter.validate(ok)) and not warnings_of(BilibiliAdapter.validate(ok))
    for bad in ("  ", 123, True):
        issues = BilibiliAdapter.validate(video_post(media, options={"tid": 231, "collection": bad}))
        assert "platforms.bilibili.collection" in errors_of(issues)
    # 图文（动态）没有合集：按未知参数告警
    assert "platforms.bilibili.collection" in warnings_of(
        BilibiliAdapter.validate(note_post(media, options={"collection": "科技小课堂"})))


async def test_publish_video_joins_collection(video_env, media: Path, monkeypatch, fake_bili):
    """上传前按名称查合集；投稿成功后（审核中）用创作中心接口取 cid，立即加入小节。"""
    calls: list[Any] = []
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, calls))
    ctx, events = make_ctx(video_env)
    fake_bili.ctx = ctx
    post = video_post(media, options={"tid": 231, "collection": " 科技小课堂 "})
    result = await BilibiliAdapter(ctx).publish(post)
    assert result.status is Status.SUBMITTED and result.message.endswith("；已加入合集「科技小课堂」")
    assert not any("season" in a for a in calls[0])  # 不在投稿参数里带 season_id
    assert fake_bili.marks["/x2/creative/web/seasons"] is False  # 上传前查
    view = fake_bili.by_path("/x/vupre/web/archive/view")[0]
    assert view.url.params["aid"] == "113456789012345" and fake_bili.marks["/x/vupre/web/archive/view"]
    add = fake_bili.by_path("/x2/creative/web/season/section/episodes/add")[0]
    assert add.url.params["csrf"] == "csrf123" and "SESSDATA=sess%2Cabc" in add.headers["cookie"]
    assert json.loads(add.content) == {"sectionId": 6001, "csrf": "csrf123", "episodes": [
        {"aid": 113456789012345, "cid": 4305787, "title": "第12期：测试视频"}]}
    assert any(e.get("message") == "已加入合集「科技小课堂」" for e in events)
    check = fake_bili.by_path("/x2/creative/web/season/section")[-1]
    assert check.url.params["id"] == "6001"


async def test_publish_video_collection_add_ok_but_missing(video_env, media: Path, monkeypatch, fake_bili):
    """接口返回 OK 但小节列表里没有：以列表为准，报加入失败。"""
    from multipub.platforms import bili_season

    monkeypatch.setattr(bili_season, "RETRY_DELAY", 0)
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, []))
    fake_bili.add_takes_effect = False
    ctx, _ = make_ctx(video_env)
    result = await BilibiliAdapter(ctx).publish(video_post(media, options={"tid": 231, "collection": "科技小课堂"}))
    assert result.status is Status.SUBMITTED
    assert "加入合集「科技小课堂」失败（接口返回成功，但合集的稿件列表里还没有这条稿件）" in result.message
    assert len(fake_bili.by_path("/x2/creative/web/season/section")) == bili_season.RETRY_TIMES


async def test_publish_video_collection_retries_until_archive_visible(video_env, media: Path, monkeypatch,
                                                                      fake_bili):
    from multipub.platforms import bili_season

    monkeypatch.setattr(bili_season, "RETRY_DELAY", 0)
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, []))
    fake_bili.archive_views = [{"code": -404, "message": "啥都木有"}, {"code": 0, "data": {"archive": {}, "videos": []}}]
    fake_bili.episode_codes = [20111]  # 太频繁：重试
    ctx, _ = make_ctx(video_env)
    result = await BilibiliAdapter(ctx).publish(video_post(media, options={"tid": 231, "collection": "科技小课堂"}))
    assert result.message.endswith("已加入合集「科技小课堂」")
    assert len(fake_bili.by_path("/x/vupre/web/archive/view")) == 3
    assert len(fake_bili.by_path("/x2/creative/web/season/section/episodes/add")) == 2


async def test_publish_video_collection_not_found_still_publishes(video_env, media: Path, monkeypatch, fake_bili):
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, []))
    ctx, events = make_ctx(video_env)
    result = await BilibiliAdapter(ctx).publish(video_post(media, options={"tid": 231, "collection": "科技"}))
    assert result.status is Status.SUBMITTED and "合集" not in result.message
    assert any("没有找到合集「科技」" in e.get("message", "") and "科技小课堂、读书笔记" in e["message"] for e in events)
    assert not fake_bili.by_path("/x2/creative/web/season/section/episodes/add")


async def test_publish_video_collection_join_failure_keeps_result(video_env, media: Path, monkeypatch, fake_bili):
    """稿件已投出：加入合集失败只在消息里提示补加办法，结果仍是投稿成功。"""
    monkeypatch.setattr(bb, "run_streaming", fake_run(SUCCESS_LINES, 0, []))
    fake_bili.episode_codes = [20092]
    fake_bili.seasons = [season_item(1, "科技小课堂", 11, 12)]
    ctx, events = make_ctx(video_env)
    result = await BilibiliAdapter(ctx).publish(video_post(media, options={"tid": 231, "collection": "科技小课堂"}))
    assert result.status is Status.SUBMITTED and result.url.endswith("BV1Ab4y1C7xY")
    assert "加入合集「科技小课堂」失败" in result.message
    assert "biliup season add 11 --vid BV1Ab4y1C7xY" in result.message
    assert any("2 个小节，加入第一个「正片」" in e.get("message", "") for e in events)
