"""知乎模拟测试的公共工具：数据目录 / 登录文件 / 清单，带场景参数运行真实 CLI。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from .._harness import REPO, make_home, run_log
from .images import jpeg, png, webp
from .server import D_C0, SIM_Z_C0, XSRF

SIM_DIR = Path(__file__).resolve().parent
SIGTERM = signal.SIGTERM
SIM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/141.0.0.0 Safari/537.36")

TOML = """
[timeouts]
publish_article = {publish_timeout}
publish_note = {publish_timeout}
login = {login_timeout}
check = 60

[platforms.zhihu]
write_interval = {write_interval}
image_timeout = {image_timeout}
{extra}
"""


def home_for(root: Path, *, logged_in: bool = True, publish_timeout: int = 200, login_timeout: int = 150,
             write_interval: float = 1, image_timeout: float = 20, extra: str = "", ua: bool = True,
             **account: Any) -> Path:
    home = make_home(root, TOML.format(publish_timeout=publish_timeout, login_timeout=login_timeout,
                                       write_interval=write_interval, image_timeout=image_timeout, extra=extra))
    if logged_in:
        write_account(home, **account)
        if ua:
            ua_file = home / "accounts" / "zhihu" / "main.ua.txt"
            ua_file.write_text(SIM_UA, encoding="utf-8")
    return home


def storage_state(*, z_c0: str = SIM_Z_C0, z_c0_days: float = 30, xsrf: bool = True) -> dict[str, Any]:
    """patchright storage_state 的样子：z_c0 / _xsrf / d_c0 在 .zhihu.com，另有几个 host-only cookie。"""
    now = time.time()

    def ck(name: str, value: str, domain: str = ".zhihu.com", days: float = 365, http_only: bool = False):
        return {"name": name, "value": value, "domain": domain, "path": "/", "expires": now + days * 86400,
                "httpOnly": http_only, "secure": True, "sameSite": "Lax"}

    cookies = [ck("z_c0", z_c0, days=z_c0_days, http_only=True), ck("d_c0", D_C0),
               ck("_zap", "sim-zap"), ck("BEC", "sim-bec", domain="www.zhihu.com", days=-1),
               ck("tst", "r", domain="www.zhihu.com")]
    cookies[3]["expires"] = -1  # 会话 cookie
    if xsrf:
        cookies.append(ck("_xsrf", XSRF))
    return {"cookies": cookies, "origins": [{"origin": "https://www.zhihu.com", "localStorage": []}]}


def write_account(home: Path, account: str = "main", *, bom: bool = False, fmt: str = "state", **kw: Any) -> Path:
    """fmt：state（patchright storage_state）/ list（浏览器插件导出的 cookie 列表）/ dict（手写的 {name: value}）。"""
    f = home / "accounts" / "zhihu" / f"{account}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    state = storage_state(**kw)
    data: Any = state
    if fmt == "list":  # EditThisCookie 之类：expirationDate、hostOnly、sameSite 写法与 patchright 不同
        data = [{"domain": c["domain"], "expirationDate": c["expires"], "hostOnly": not c["domain"].startswith("."),
                 "httpOnly": c["httpOnly"], "name": c["name"], "path": "/", "sameSite": "no_restriction",
                 "secure": True, "session": c["expires"] < 0, "value": c["value"]} for c in state["cookies"]]
    elif fmt == "dict":
        data = {c["name"]: c["value"] for c in state["cookies"]}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    f.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return f


ARTICLE_MD = """---
tags: [草稿]
---
# 周末徒步：西山环线

西山环线 12 公里，**适合新手**。
第二行：带好水。

![山顶](<图片/山顶 全景.png>)

## 路线

| 段 | 公里 |
|---|---|
| 上山 | 5 |

![远程图](https://img.example.com/view.webp)

### 装备

- 登山鞋
- 水 2L

![再次引用同一张](图片/%E5%B1%B1%E9%A1%B6%20%E5%85%A8%E6%99%AF.png)

公式 $E=mc^2$ 与脚注[^1]。

[^1]: 参考 https://example.com/route
"""


SMALL_MD = "# 小文章\n\n第一段，**重点**。\n\n![山顶](<图片/山顶 全景.png>)\n"


def write_post(root: Path, kind: str = "article", *, images: int = 2, zhihu: dict[str, Any] | None = None,
               body_md: str | None = None, **fields: Any) -> Path:
    """在 root（可含中文、空格）下生成 post.yaml、正文和图片。"""
    root.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"kind": kind, "account": "main"}
    if kind == "article":
        content = root / "正文 稿"
        (content / "图片").mkdir(parents=True, exist_ok=True)
        (content / "图片" / "山顶 全景.png").write_bytes(png(120, 80, (10, 120, 200)))
        (content / "文章.md").write_text(body_md if body_md is not None else ARTICLE_MD, encoding="utf-8")
        (root / "封面 图.jpg").write_bytes(jpeg(160, 90))
        data.update(title="周末徒步：西山环线 12 公里", body_file="正文 稿/文章.md", cover="封面 图.jpg",
                    tags=["徒步", "Python"])
        names = []
        for i in range(images):
            name = f"附图 {i + 1}.png"
            (root / name).write_bytes(png(70 + i, 50 + i, (i * 40 % 255, 90, 160)))
            names.append(name)
        if names:
            data["images"] = names
    else:
        names = []
        for i in range(images):
            name = f"照片 {i + 1}.jpg" if i % 3 else f"照片 {i + 1}.webp"
            (root / name).write_bytes(jpeg(80 + i, 60 + i, (i * 25 % 255, 100, 50)) if i % 3
                                      else webp(80 + i, 60 + i, (i * 25 % 255, 100, 50)))
            names.append(name)
        data.update(title="周末徒步", body="西山环线 12 公里，\n适合新手。\n\n带好水。", tags=["徒步"], images=names)
    data["platforms"] = {"zhihu": dict(zhihu or {})}
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (root / "post.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return root / "post.yaml"


def sim_env(log: Path, scenario: dict[str, Any] | None = None, tag: str = "") -> dict[str, str]:
    return {"MULTIPUB_SIM": "zhihu", "MULTIPUB_SIM_ZHIHU": json.dumps(scenario or {}, ensure_ascii=False),
            "MULTIPUB_SIM_ZHIHU_LOG": str(log), "MULTIPUB_SIM_ZHIHU_STATE": str(log.with_suffix(".state.json")),
            "MULTIPUB_SIM_ZHIHU_TAG": tag or uuid.uuid4().hex}


def cli_env(home: Path, env: dict[str, str]) -> dict[str, str]:
    e = dict(os.environ)
    e["MULTIPUB_HOME"] = str(home)
    paths = [str(SIM_DIR), str(REPO)] + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else [])
    e["PYTHONPATH"] = os.pathsep.join(paths)
    e.update(env)
    return e


def run(args: list[str], home: Path, log: Path, scenario: dict[str, Any] | None = None, timeout: int = 240,
        display: str | None = None) -> tuple[int, dict[str, Any] | None, str, float]:
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


def start(args: list[str], home: Path, log: Path, scenario: dict[str, Any] | None = None,
          tag: str = "") -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "multipub.cli", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            env=cli_env(home, sim_env(log, scenario, tag)), cwd=str(REPO))


def events(log: Path, kind: str | None = None, **match: Any) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if (kind is None or r["type"] == kind)
            and all(r.get(k) == v for k, v in match.items())]


def requests(log: Path, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
    return [r for r in events(log, "req") if (method is None or r["method"] == method)
            and (path is None or r["path"] == path or (path.endswith("*") and r["path"].startswith(path[:-1])))]


def history(home: Path) -> list[dict[str, Any]]:
    f = home / "history.jsonl"
    if not f.is_file():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def tagged_pids(tag: str) -> list[int]:
    """环境变量里带 MULTIPUB_SIM_ZHIHU_TAG=<tag> 的进程（CLI、worker、浏览器都继承它）。"""
    needle = f"MULTIPUB_SIM_ZHIHU_TAG={tag}".encode()
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


def describe(code: int, out: Any, err: str, home: Path, log: Path) -> str:
    return (f"exit={code}\nout={json.dumps(out, ensure_ascii=False, indent=1)[:3000]}\n"
            f"stderr(tail)={err[-3000:]}\nsim-log={json.dumps(events(log), ensure_ascii=False)[-4000:]}\n"
            f"{run_log(home)}")


def new_tag() -> str:
    return uuid.uuid4().hex


@contextmanager
def xvfb() -> Iterator[str]:
    """有 DISPLAY 就用它；否则起一个 Xvfb 虚拟屏幕（有头登录的测试用），结束时关掉。"""
    if os.environ.get("DISPLAY"):
        yield os.environ["DISPLAY"]
        return
    num = 600 + os.getpid() % 300
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


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or shutil.which("Xvfb"))
