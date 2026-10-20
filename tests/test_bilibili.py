"""B站适配器测试：全部离线（httpx.MockTransport、假 biliup 输出、本地子进程）。"""

from __future__ import annotations

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


# ------------------------------------------------------------------ biliup 参数与输出

def test_build_upload_args_full(media: Path, tmp_path: Path):
    cover = media / "封面 图.jpg"
    cover.write_bytes((media / "cover.jpg").read_bytes())
    when = (datetime.now() + timedelta(hours=3)).replace(second=0, microsecond=0)
    post = video_post(media, body="", cover=cover, schedule=when, ai_generated=True, title="-以横线开头",
                      options={"tid": 231, "tid_v2": 2001, "copyright": 2, "source": "https://example.com/a",
                               "dynamic": "新视频！", "no_reprint": 1, "close_reply": True, "line": "BDA2",
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
def video_env(settings, media: Path):
    fake = settings.home / "biliup-fake.exe"
    fake.write_bytes(b"")
    settings.biliup_path = str(fake)
    # worker 会从磁盘重新读配置
    (settings.home / "config.toml").write_text(f"[platforms.bilibili]\nbiliup_path = '{fake}'\n", encoding="utf-8")
    write_json(settings.account_file("bilibili", "main"), biliup_login())
    return settings


def fake_run(lines: list[str], rc: int | None, calls: list[Any]):
    async def run(args, *, cwd, timeout, on_line=None, heartbeat=30):
        calls.append(args)
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


@pytest.mark.parametrize("lines,rc,code", [
    (["Error: file not found"], 1, ErrorCode.PLATFORM_ERROR),
    (SUCCESS_LINES[:4] + ["Error: connection reset"], 1, ErrorCode.UNKNOWN_OUTCOME),
    (SUCCESS_LINES[:4] + ['ResponseData { code: 21012, data: None, message: "标签不能为空", ttl: Some(1) }'], 1,
     ErrorCode.PLATFORM_ERROR),
    (["uploading..."], None, ErrorCode.TIMEOUT),
    (SUCCESS_LINES[:4], None, ErrorCode.UNKNOWN_OUTCOME),
    (["Error: credential validation failed with Bilibili code -101"], 1, ErrorCode.LOGIN_REQUIRED),
])
async def test_publish_video_failures(video_env, media: Path, monkeypatch, lines, rc, code):
    monkeypatch.setattr(bb, "run_streaming", fake_run(lines, rc, []))
    ctx, _ = make_ctx(video_env)
    with pytest.raises(PublishError) as info:
        await BilibiliAdapter(ctx).publish(video_post(media))
    assert info.value.code is code
    assert ctx.submit_started


async def test_publish_video_through_worker(video_env, media: Path, monkeypatch):
    from multipub.worker import run_job

    monkeypatch.setattr(bb, "run_streaming", fake_run(["Error: boom"], 1, []))
    job = {"action": "publish", "platform": "bilibili", "account": "main", "home": str(video_env.home),
           "headless": True, "run_dir": str(video_env.runs_dir / "w"), "post": video_post(media).to_dict()}
    result = await run_job(job, emit=lambda e: None)
    assert result.status is Status.FAILED and result.error_code == "platform_error"
    assert result.submit_started and not result.retryable
    assert "--force" in result.message


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
    assert "platforms.bilibili.tid" in errors_of(BilibiliAdapter.validate(video_post(media, options={"tid": "abc"})))
    assert not errors_of(BilibiliAdapter.validate(video_post(media, options={"tid": "231"})))
    cover = media / "cover.webp"
    cover.write_bytes(b"x")
    assert "cover" in errors_of(BilibiliAdapter.validate(video_post(media, cover=cover)))
    assert "ai_generated" in warnings_of(BilibiliAdapter.validate(video_post(media, ai_generated=True)))


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
    big = media / "images" / "big.png"
    big.write_bytes(b"\0" * (20 * 1024 * 1024 + 1))
    assert "images" in errors_of(BilibiliAdapter.validate(note_post(media, images=[big])))


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
            "bad=[x for x in ('httpx','patchright','segno') if x in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ------------------------------------------------------------------ HTTP 接口（MockTransport）

def nav_payload(login: bool = True) -> dict[str, Any]:
    data = {"isLogin": login, "wbi_img": {"img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                                          "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png"}}
    if login:
        data.update({"mid": 10086, "uname": "测试UP"})
    return {"code": 0 if login else -101, "message": "0", "data": data}


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
                "image_height": 1440, "img_size": 123.4}})
        if path == "/x/dynamic/feed/create/submit_check":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path == "/x/dynamic/feed/create/dyn":
            return self.create_response or httpx.Response(200, json={"code": 0, "data": {
                "dyn_id": 1, "dyn_id_str": "1234567890123456789", "dyn_type": 2}})
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
    assert params["platform"] == "web" and params["csrf"] == "csrf123"
    w_rid = params.pop("w_rid")
    assert w_rid == independent_wbi(params, IMG_KEY, SUB_KEY)
    req = json.loads(create.content)["dyn_req"]
    assert req["scene"] == 2 and req["content"]["title"] == "周末徒步"
    assert req["content"]["contents"] == [{"raw_text": "正文第一行\n第二行\n#徒步# #周末#", "type": 1, "biz_id": ""}]
    assert [p["img_src"] for p in req["pics"]] == ["http://i0.hdslb.com/bfs/new_dyn/1.jpg",
                                                   "http://i0.hdslb.com/bfs/new_dyn/2.jpg"]
    assert req["pics"][0] == {"img_src": "http://i0.hdslb.com/bfs/new_dyn/1.jpg", "img_width": 1080,
                              "img_height": 1440, "img_size": 123.4}
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
