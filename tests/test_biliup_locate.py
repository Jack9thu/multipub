"""biliup 来源选择：用户给路径 / 本机自动查找 / 下载固定版本。用 shell 脚本冒充 biliup（不联网）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from multipub.cli import main
from multipub.config import load_settings
from multipub.platforms import bili_biliup as bb
from multipub.platforms import bili_locate as bl

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="用 shell 脚本冒充 biliup")

V131_TOP = """Usage: biliup [OPTIONS] <COMMAND>
Commands:
  login     登录B站并保存登录信息
  renew     手动验证并刷新登录信息
  upload    上传视频
Options:
  -p, --proxy <PROXY>
  -u, --user-cookie <USER_COOKIE>
      --rust-log <RUST_LOG>
"""
V131_UPLOAD = """Options:
      --title <TITLE>
      --desc <DESC>
      --tid <TID>
      --tid-v2 <TID_V2>
      --tag <TAG>
      --copyright <COPYRIGHT>
      --source <SOURCE>
      --cover <COVER>
      --dtime <DTIME>
      --dynamic <DYNAMIC>
      --line <LINE>
      --submit <SUBMIT>
"""
OLD_TOP = "Commands:\n  login\n  upload\nOptions:\n  -u, --user-cookie <USER_COOKIE>\n"
OLD_UPLOAD = "      --title <TITLE>\n      --desc <DESC>\n      --tid <TID>\n      --tag <TAG>\n      --copyright <C>\n"


def fake_biliup(path: Path, version: str, top: str, upload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n  --version) echo "biliup-cli {version}";;\n'
        f"  --help) cat <<'X'\n{top}X\n;;\n"
        f"  upload) cat <<'X'\n{upload}X\n;;\n"
        '  *) echo "unexpected $*" >&2; exit 9;;\nesac\n', encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def env(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "userhome"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    pathdir = tmp_path / "path bin"
    monkeypatch.setenv("PATH", f"{pathdir}:/usr/bin:/bin")
    monkeypatch.chdir(tmp_path)
    bl._CAP_CACHE.clear()
    return {"home": home, "userhome": fake_home, "pathdir": pathdir, "tmp": tmp_path}


def test_discover_sources_and_compatibility(env):
    new = fake_biliup(env["pathdir"] / "biliup", "1.3.1", V131_TOP, V131_UPLOAD)
    sau = fake_biliup(env["userhome"] / ".social-auto-upload/tools/biliup/linux-x86_64/biliup", "1.2.0",
                      V131_TOP, V131_UPLOAD)
    old = fake_biliup(env["userhome"] / "Downloads/biliupR-v0.1.9/biliup", "0.1.9", OLD_TOP, OLD_UPLOAD)
    (new.parent / "cookies.json").write_text("{}", encoding="utf-8")
    settings = load_settings()
    cands = {c.path: c for c in bl.discover(settings)}
    assert set(cands) == {new, sau, old}
    assert cands[new].compatible and cands[new].version == "v1.3.1" and cands[new].source == "PATH"
    assert cands[new].cookies == [str(new.parent / "cookies.json")]
    assert cands[sau].compatible and cands[sau].source == "social-auto-upload"
    # 只缺可选参数（--dtime 等）仍算兼容：用到这些选项时发布前再拦（见 test_unsupported_flags_and_rust_log）
    assert cands[old].compatible and cands[old].version == "v0.1.9"
    # 自动挑选：固定版本优先
    assert bl.best(settings, list(cands.values())).path == new


def test_incompatible_reason_and_force(env):
    really_old = fake_biliup(env["tmp"] / "x/biliup", "0.0.1", OLD_TOP, "      --title <T>\n")
    settings = load_settings()
    ok, msg, _ = bl.use(settings, str(really_old))
    assert not ok and "--desc" in msg and "--force" in msg
    ok, msg, res = bl.use(settings, str(really_old), force=True)
    assert ok and "警告" in msg
    assert bl.read_choice(settings)["mode"] == "path"
    assert not res.ok and res.problem  # 不兼容的仍然报告问题，发布前会拦住


def test_selection_order(env, monkeypatch):
    settings = load_settings()
    assert bl.resolve(settings).mode == "none" and bl.needs_choice(settings)
    managed = fake_biliup(bb.managed_path(settings), "1.3.1", V131_TOP, V131_UPLOAD)
    assert bl.resolve(settings).mode == "default" and bl.resolve(settings).path == managed
    mine = fake_biliup(env["tmp"] / "我的 工具/biliup", "1.3.1", V131_TOP, V131_UPLOAD)
    assert bl.use(settings, str(mine.parent))[0]  # 给目录也行
    assert bl.resolve(settings).path == mine.resolve() and bb.find_biliup(settings) == mine.resolve()
    # config.toml 的 biliup_path 优先
    settings.biliup_path = str(managed)
    assert bl.resolve(settings).mode == "configured"
    settings.biliup_path = ""
    mine.unlink()
    res = bl.resolve(settings)
    assert not res.ok and "不存在" in res.problem and bb.find_biliup(settings) is None


def test_unsupported_flags_and_rust_log(env):
    old = fake_biliup(env["tmp"] / "old/biliup", "0.9.0", OLD_TOP, OLD_UPLOAD)
    args = [str(old), "-u", "c.json", "upload", "--title=t", "--desc=d", "--tid=1", "--tag=a",
            "--copyright=1", "--dtime=1", "--line=bda2", "--", "v.mp4"]
    assert bl.unsupported_flags(old, args) == ["--dtime", "--line"]
    assert not bl.supports_rust_log(old)
    new = fake_biliup(env["tmp"] / "new/biliup", "1.3.1", V131_TOP, V131_UPLOAD)
    assert bl.supports_rust_log(new) and bl.unsupported_flags(new, args) == []


def test_cli_setup_needs_choice_then_choose(env, capsys, monkeypatch):
    fake_biliup(env["pathdir"] / "biliup", "1.3.1", V131_TOP, V131_UPLOAD)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["setup", "--skip-browser", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    step = [s for s in out["steps"] if s["name"] == "bilibili.biliup"][0]
    assert step["needs_choice"] and step["candidates"][0]["compatible"]
    assert not bb.managed_path(load_settings()).exists()  # 没有擅自下载

    assert main(["setup", "--skip-browser", "--biliup", "auto", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [s for s in out["steps"] if s["name"] == "bilibili.biliup"][0]["ok"]

    assert main(["biliup", "find", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["in_use"]["mode"] == "auto" and out["candidates"][0]["index"] == 1
    assert main(["biliup", "use", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["in_use"]["mode"] == "path"
    assert main(["biliup", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["choice"]["mode"] == "path"
    assert main(["biliup", "use", "9", "--json"]) == 2


def test_build_upload_args_without_rust_log(tmp_path):
    from datetime import datetime

    from multipub.models import Kind, PlatformPost

    post = PlatformPost(platform="bilibili", account="a", kind=Kind.VIDEO, title="t", body="d", tags=["x"],
                        video=tmp_path / "v.mp4", options={"tid": 1})
    args = bb.build_upload_args(Path("biliup"), Path("c.json"), post, rust_log=False)
    assert "--rust-log" not in args and args[1:4] == ["-u", "c.json", "upload"]
    assert datetime  # 保持导入简单
