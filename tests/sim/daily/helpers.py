"""日常使用模拟的公共工具：数据目录、媒体文件、带模拟平台运行真实 CLI、找残留进程。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .._harness import REPO, find_chromium, run_log

SIM_DIR = Path(__file__).resolve().parent


def make_home(root: Path, extra_toml: str = "") -> Path:
    """数据目录故意带空格和中文。"""
    home = root / "data dir" / "多 平台"
    home.mkdir(parents=True, exist_ok=True)
    chromium = find_chromium() or ""
    (home / "config.toml").write_text(
        f"gap_seconds = 0\n{extra_toml}\n[browser]\nexecutable_path = '{chromium}'\n", encoding="utf-8")
    return home


def write_media(d: Path, images: int = 3) -> None:
    from PIL import Image

    (d / "images").mkdir(parents=True, exist_ok=True)
    for i in range(1, images + 1):
        Image.new("RGB", (1080, 1440), (200, 100 + i * 20, 50)).save(d / "images" / f"图 {i}.jpg", "JPEG")
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(d / "cover.jpg", "JPEG")
    (d / "视频 成片.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096)


def write_storage_state(home: Path, platform: str, account: str, session: str = "valid") -> Path:
    f = home / "accounts" / platform / f"{account}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"cookies": [
        {"name": "sess", "value": session, "domain": "creator.sim.example", "path": "/", "expires": -1,
         "httpOnly": False, "secure": True, "sameSite": "Lax"}], "origins": []}), encoding="utf-8")
    return f


def env_for(home: Path, log: Path, scenario: dict[str, Any] | None = None, tag: str = "") -> dict[str, str]:
    e = dict(os.environ)
    e.update({
        "MULTIPUB_HOME": str(home), "MULTIPUB_ENABLE_FAKE": "1", "MULTIPUB_SIM": "daily",
        "MULTIPUB_SIM_DAILY": json.dumps(scenario or {}), "MULTIPUB_SIM_DAILY_LOG": str(log),
        "PYTHONPATH": os.pathsep.join([str(SIM_DIR), str(REPO)] + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else [])),
    })
    if tag:
        e["MULTIPUB_SIM_DAILY_TAG"] = tag
    return e


class Sim:
    """一个模拟用户：自己的数据目录、工作目录和模拟平台日志。"""

    def __init__(self, root: Path, extra_toml: str = ""):
        self.root = root
        self.home = make_home(root, extra_toml)
        self.log = root / "sim.jsonl"
        self.cwd = root / "工作 目录"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.tag = uuid.uuid4().hex

    def run(self, args: list[str], *, scenario: dict[str, Any] | None = None, timeout: int = 120,
            cwd: Path | None = None, home: bool = True) -> tuple[int, Any, str]:
        env = env_for(self.home, self.log, scenario, self.tag)
        if not home:
            env.pop("MULTIPUB_HOME")
        proc = subprocess.run([sys.executable, "-m", "multipub.cli", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env,
                              cwd=str(cwd or self.cwd))
        try:
            out = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            out = proc.stdout
        return proc.returncode, out, proc.stderr

    def start(self, args: list[str], *, scenario: dict[str, Any] | None = None) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "multipub.cli", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                env=env_for(self.home, self.log, scenario, self.tag), cwd=str(self.cwd))

    def history(self) -> list[dict[str, Any]]:
        f = self.home / "history.jsonl"
        if not f.is_file():
            return []
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]

    def pending(self) -> list[dict[str, Any]]:
        """发布途中留下的「结果待定」记录（<数据目录>/pending/）。"""
        d = self.home / "pending"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def published(self, platform: str | None = None) -> list[dict[str, Any]]:
        """模拟平台上真正「发出去」的次数（查重是否生效看这个）。"""
        if not self.log.is_file():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if r["type"] == "publish" and (platform is None or r["platform"] == platform)]

    def pids(self) -> list[int]:
        return tagged_pids(self.tag)

    def debug(self, code: int = 0, out: Any = None, err: str = "") -> str:
        return (f"exit={code}\nout={json.dumps(out, ensure_ascii=False)[:3000] if not isinstance(out, str) else out[:3000]}"
                f"\nstderr={err[-3000:]}\nhistory={json.dumps(self.history(), ensure_ascii=False)[-2000:]}\n"
                f"{run_log(self.home) if (self.home / 'runs').is_dir() else ''}")


def tagged_pids(tag: str) -> list[int]:
    """环境变量里带 MULTIPUB_SIM_DAILY_TAG=<tag> 的进程（CLI、worker、driver、浏览器都继承它）。"""
    needle = f"MULTIPUB_SIM_DAILY_TAG={tag}".encode()
    me = os.getpid()
    pids: list[int] = []
    if not Path("/proc").is_dir():  # 没有 /proc 的系统上不检查
        return pids
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == me:
            continue
        try:
            if needle in (p / "environ").read_bytes().split(b"\0"):
                pids.append(int(p.name))
        except OSError:
            continue
    return pids


def wait_for(pred, timeout: float, step: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def read_stderr_until(proc: subprocess.Popen, needle: str, timeout: float) -> str:
    """边读 stderr 边找 needle（读到就返回已读内容）；用后台线程读，避免 readline 卡住。"""
    import queue
    import threading

    q: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stderr is not None
        for line in iter(proc.stderr.readline, ""):
            q.put(line)
        q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    seen = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            break
        seen += line
        if needle in seen:
            break
    return seen


def events(stderr: str, name: str) -> list[dict[str, Any]]:
    rows = []
    for line in stderr.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event") == name:
            rows.append(ev)
    return rows
