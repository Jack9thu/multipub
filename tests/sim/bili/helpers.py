"""B站模拟测试的公共工具：数据目录 / 登录文件 / 清单 / 仿 biliup，带场景参数运行真实 CLI。"""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import time
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .._harness import REPO, make_home, run_log
from .server import login_info

SIM_DIR = Path(__file__).resolve().parent
FAKE_BILIUP = SIM_DIR / "fake_biliup.py"

TOML = """
[timeouts]
publish_video = {video_timeout}
publish_note = 200
login = 150
check = 60

[platforms.bilibili]
{biliup_line}
{extra}
"""


def png(w: int = 64, h: int = 48, color: tuple[int, int, int] = (0, 174, 236)) -> bytes:
    """真正能被浏览器解码的 PNG（线上发布框会校验宽高 >= 10px）。"""
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_biliup(root: Path) -> Path:
    """生成仿 biliup 的启动脚本（python -I 运行 fake_biliup.py，不受 PYTHONPATH 影响）。"""
    root.mkdir(parents=True, exist_ok=True)
    exe = root / "biliup"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_BILIUP}" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    return exe


def home_for(tmp_path: Path, *, biliup: bool | str = True, logged_in: bool = True, video_timeout: int = 200,
             extra: str = "", **account: Any) -> Path:
    """biliup=True：生成仿 biliup 并写进 biliup_path；"managed"：放到 mpub setup 的安装位置。"""
    biliup_line = ""
    if biliup is True:
        exe = make_biliup(tmp_path / "bin 工具")
        biliup_line = f"biliup_path = '{exe}'"
    home = make_home(tmp_path, TOML.format(video_timeout=video_timeout, biliup_line=biliup_line, extra=extra))
    if biliup == "managed":
        make_biliup(home / "tools" / "biliup" / "v1.3.1")
    if logged_in:
        write_account(home, **account)
    return home


def write_account(home: Path, account: str = "main", **kw: Any) -> Path:
    f = home / "accounts" / "bilibili" / f"{account}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(login_info(**kw), ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def write_post(root: Path, kind: str = "video", *, images: int = 3, bili: dict[str, Any] | None = None,
               sizes: list[tuple[int, int]] | None = None, **fields: Any) -> Path:
    """在 root（可含中文、空格）下生成 post.yaml 和媒体文件；sizes：每张图的宽高（默认 64x48 左右）。"""
    import yaml

    root.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"kind": kind, "account": "main"}
    if kind == "video":
        (root / "视频 第1期.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(64 * 1024))
        (root / "封面 1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2048)
        data.update(title="三分钟讲清楚 Git 分支", body="用三分钟讲清楚 Git 分支。\n第二行简介。", tags=["编程", "Git"],
                    video="视频 第1期.mp4", cover="封面 1.jpg")
        platform = {"tid": 231}
    else:
        names = []
        for i in range(images):
            name = f"图 {i + 1}.png"
            w, ht = sizes[i] if sizes and i < len(sizes) else (64 + i, 48 + i)
            (root / name).write_bytes(png(w, ht))
            names.append(name)
        data.update(title="周末徒步路线", body="西山环线 12 公里，\n适合新手。", tags=["徒步"], images=names)
        platform = {}
    platform.update(bili or {})
    data["platforms"] = {"bilibili": platform}
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (root / "post.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return root


def sim_env(log: Path, scenario: dict[str, Any] | None = None, tag: str = "") -> dict[str, str]:
    env = {"MULTIPUB_SIM": "bili", "MULTIPUB_SIM_BILI": json.dumps(scenario or {}, ensure_ascii=False),
           "MULTIPUB_SIM_BILI_LOG": str(log)}
    if tag:
        env["MULTIPUB_SIM_BILI_TAG"] = tag
    return env


def cli_env(home: Path, env: dict[str, str]) -> dict[str, str]:
    e = dict(os.environ)
    e["MULTIPUB_HOME"] = str(home)
    paths = [str(SIM_DIR), str(REPO)] + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else [])
    e["PYTHONPATH"] = os.pathsep.join(paths)
    e.update(env)
    return e


def run(args: list[str], home: Path, log: Path, scenario: dict[str, Any] | None = None,
        timeout: int = 240, display: str | None = None) -> tuple[int, dict[str, Any] | None, str, float]:
    """display：有头浏览器用的 X 显示（见 xvfb()）。"""
    t0 = time.monotonic()
    env = cli_env(home, sim_env(log, scenario))
    if display:
        env["DISPLAY"] = display
    proc = subprocess.run([sys.executable, "-m", "multipub.cli", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=str(REPO))
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        out = None
    return proc.returncode, out, proc.stderr, time.monotonic() - t0


@contextmanager
def xvfb() -> Iterator[str]:
    """有 DISPLAY 就用它；否则起一个 Xvfb 虚拟屏幕（有头模式的测试用），结束时关掉。"""
    if os.environ.get("DISPLAY"):
        yield os.environ["DISPLAY"]
        return
    num = 90 + os.getpid() % 500
    while Path(f"/tmp/.X11-unix/X{num}").exists() or Path(f"/tmp/.X{num}-lock").exists():
        num += 1
    proc = subprocess.Popen(["Xvfb", f":{num}", "-screen", "0", "1440x960x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not Path(f"/tmp/.X11-unix/X{num}").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        yield f":{num}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def start(args: list[str], home: Path, log: Path, scenario: dict[str, Any] | None = None,
          tag: str = "") -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "multipub.cli", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            env=cli_env(home, sim_env(log, scenario, tag)), cwd=str(REPO))


def events(log: Path, kind: str | None = None) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if kind is None or r["type"] == kind]


def page_events(log: Path, evt: str) -> list[dict[str, Any]]:
    return [r for r in events(log, "page") if r.get("evt") == evt]


def history(home: Path) -> list[dict[str, Any]]:
    f = home / "history.jsonl"
    if not f.is_file():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def tagged_pids(tag: str) -> list[int]:
    """环境变量里带 MULTIPUB_SIM_BILI_TAG=<tag> 的进程（worker、浏览器、biliup 都继承它）。"""
    needle = f"MULTIPUB_SIM_BILI_TAG={tag}".encode()
    pids = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if needle in (p / "environ").read_bytes().split(b"\0"):
                pids.append(int(p.name))
        except OSError:
            continue
    return pids


def new_tag() -> str:
    return uuid.uuid4().hex


def describe(code: int, out: Any, err: str, home: Path, log: Path) -> str:
    return (f"exit={code}\nout={json.dumps(out, ensure_ascii=False, indent=1)[:3000]}\n"
            f"stderr(tail)={err[-3000:]}\nsim-log={json.dumps(events(log), ensure_ascii=False)[-4000:]}\n"
            f"{run_log(home)}")


SIGTERM = signal.SIGTERM
