"""模拟测试公共工具：找 Chromium、准备数据目录、运行真实 CLI。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
CHROMIUM_CANDIDATES = (
    os.environ.get("MULTIPUB_SIM_CHROMIUM", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
)


def find_chromium() -> str | None:
    for c in CHROMIUM_CANDIDATES:
        if c and Path(c).is_file():
            return c
    return None


requires_chromium = pytest.mark.skipif(find_chromium() is None,
                                       reason="模拟测试需要 Chromium（设置 MULTIPUB_SIM_CHROMIUM）")


def make_home(tmp_path: Path, extra_toml: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    chromium = find_chromium() or ""
    (home / "config.toml").write_text(
        f"gap_seconds = 0\n{extra_toml}\n[browser]\nexecutable_path = '{chromium}'\n", encoding="utf-8")
    return home


def run_cli(args: list[str], *, home: Path, sim_dir: Path | None = None, timeout: int = 300,
            env: dict[str, str] | None = None) -> tuple[int, dict[str, Any] | None, str]:
    """在子进程里跑 python -m multipub.cli；sim_dir 里的 sitecustomize.py 会在 worker 里生效。"""
    e = dict(os.environ)
    e["MULTIPUB_HOME"] = str(home)
    paths = [str(sim_dir)] if sim_dir else []
    paths.append(str(REPO))
    if e.get("PYTHONPATH"):
        paths.append(e["PYTHONPATH"])
    e["PYTHONPATH"] = os.pathsep.join(paths)
    e.update(env or {})
    proc = subprocess.run([sys.executable, "-m", "multipub.cli", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=e, cwd=str(REPO))
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        out = None
    return proc.returncode, out, proc.stderr


def run_log(home: Path) -> str:
    """最近一次运行的 worker 日志（stderr），断言失败时打印出来便于排查。"""
    logs = sorted((home / "runs").rglob("*.log"), key=lambda p: p.stat().st_mtime)
    return "\n".join(f"--- {p}\n{p.read_text(encoding='utf-8', errors='replace')[-4000:]}" for p in logs[-3:])
