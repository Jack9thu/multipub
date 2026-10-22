"""小红书模拟测试的公共工具：准备数据目录 / 账号 / 清单，带场景参数运行真实 CLI。"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .._harness import REPO, make_home, run_log
from .server import TINY_PNG

SIM_DIR = Path(__file__).resolve().parent

TOML = """
[timeouts]
publish_video = 300
publish_note = 300
login = 150
check = 60

[platforms.xiaohongshu]
upload_timeout = 40
publish_timeout = {publish_timeout}
"""


def home_for(tmp_path: Path, *, publish_timeout: int = 25, logged_in: bool | str = True) -> Path:
    home = make_home(tmp_path, TOML.format(publish_timeout=publish_timeout))
    if logged_in:
        write_account(home, "valid" if logged_in is True else str(logged_in))
    return home


def write_account(home: Path, session: str = "valid", account: str = "main") -> Path:
    f = home / "accounts" / "xiaohongshu" / f"{account}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    state = {"cookies": [
        {"name": "web_session", "value": session, "domain": ".xiaohongshu.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"},
        {"name": "a1", "value": "sim", "domain": ".xiaohongshu.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ], "origins": []}
    f.write_text(json.dumps(state), encoding="utf-8")
    return f


def write_post(root: Path, kind: str = "video", *, images: int = 3, **fields: Any) -> Path:
    """在 root（可含中文、空格）下生成 post.yaml 和媒体文件。"""
    import yaml

    root.mkdir(parents=True, exist_ok=True)
    png = base64.b64decode(TINY_PNG)
    data: dict[str, Any] = {"kind": kind, "account": "main", "platforms": {"xiaohongshu": {}}}
    if kind == "video":
        (root / "视频 第1期.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096)
        data.update(title="三分钟讲清楚 Git", body="用三分钟讲清楚 Git 分支。\n第二行。", tags=["编程", "Git"],
                    video="视频 第1期.mp4")
    else:
        names = []
        for i in range(images):
            name = f"图 {i + 1}.png"
            (root / name).write_bytes(png)
            names.append(name)
        data.update(title="周末徒步路线", body="西山环线 12 公里。", tags=["徒步"], images=names)
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    if isinstance(data.get("cover"), str) and not (root / data["cover"]).exists():
        (root / data["cover"]).write_bytes(png)
    (root / "post.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return root


def sim_env(log: Path, scenario: dict[str, Any] | None = None, tag: str = "") -> dict[str, str]:
    env = {"MULTIPUB_SIM": "xhs", "MULTIPUB_SIM_XHS": json.dumps(scenario or {}, ensure_ascii=False),
           "MULTIPUB_SIM_XHS_LOG": str(log)}
    if tag:
        env["MULTIPUB_SIM_XHS_TAG"] = tag
    return env


def cli_env(home: Path, env: dict[str, str]) -> dict[str, str]:
    e = dict(os.environ)
    e["MULTIPUB_HOME"] = str(home)
    paths = [str(SIM_DIR), str(REPO)] + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else [])
    e["PYTHONPATH"] = os.pathsep.join(paths)
    e.update(env)
    return e


def run(args: list[str], home: Path, log: Path, scenario: dict[str, Any] | None = None,
        timeout: int = 180) -> tuple[int, dict[str, Any] | None, str, float]:
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "multipub.cli", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=cli_env(home, sim_env(log, scenario)), cwd=str(REPO))
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        out = None
    return proc.returncode, out, proc.stderr, time.monotonic() - t0


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
    """环境变量里带 MULTIPUB_SIM_XHS_TAG=<tag> 的进程（worker、driver、浏览器都继承它）。"""
    needle = f"MULTIPUB_SIM_XHS_TAG={tag}".encode()
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
            f"stderr(tail)={err[-3000:]}\nsim-log={json.dumps(events(log), ensure_ascii=False)[:3000]}\n"
            f"{run_log(home)}")


def result_of(out: dict[str, Any] | None) -> dict[str, Any]:
    assert out and out.get("results"), out
    return out["results"][-1]


SIGTERM = signal.SIGTERM
