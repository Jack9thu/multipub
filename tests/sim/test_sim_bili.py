"""B站端到端模拟：真实 CLI（mpub publish / login / check）+ 仿 biliup + 真 Chromium + 本地仿真页面 / 接口。

仿真见 tests/sim/bili/（server.py 的场景参数、pages/t.html 动态发布页、fake_biliup.py）；真实网络一律被拦截：
浏览器经 context.route，httpx 经 MockTransport，biliup 是本地脚本。
每个测试里互不相关的场景并发跑（各自独立的数据目录），整体耗时约为最慢场景的耗时。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from ._harness import requires_chromium
from .bili import helpers as h
from .bili.server import BVID, DYN_ID

WORKERS = 6
CONSENT = "confirm_dynamic_rules = true"
OPUS_URL = f"https://www.bilibili.com/opus/{DYN_ID}"
VIDEO_URL = f"https://www.bilibili.com/video/{BVID}"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="仿 biliup 是 sh 启动脚本")


@dataclass
class Case:
    name: str
    kind: str = "video"
    scenario: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    bili: dict[str, Any] = field(default_factory=dict)
    images: int = 3
    toml: str = ""
    biliup: bool | str = True
    logged_in: bool = True
    runs: list[list[str]] | None = None  # 默认一次 publish <post> --yes --json；"{post}" 会被替换
    sizes: list[tuple[int, int]] | None = None  # 图片宽高
    display: str | None = None  # 有头模式用的 X 显示
    bom: bool = False  # 登录文件开头加 UTF-8 BOM（PowerShell 5 / 记事本另存的）


@dataclass
class Run:
    case: Case
    outs: list[tuple[int, dict[str, Any] | None, str, float]]
    home: Path
    log: Path
    post: Path

    @property
    def code(self) -> int:
        return self.outs[0][0]

    @property
    def secs(self) -> float:
        return self.outs[0][3]

    def result_of(self, i: int = 0) -> dict[str, Any]:
        out = self.outs[i][1] or {}
        rows = out.get("results") or ([out["result"]] if out.get("result") else [])
        assert rows, self.debug()
        return rows[-1]

    @property
    def result(self) -> dict[str, Any]:
        return self.result_of(0)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return h.events(self.log, kind)

    def page(self, evt: str) -> list[dict[str, Any]]:
        return h.page_events(self.log, evt)

    @property
    def creates(self) -> list[dict[str, Any]]:
        return self.events("create")

    @property
    def clicks(self) -> int:
        return len(self.page("publish_clicked"))

    @property
    def uploads(self) -> list[dict[str, Any]]:
        return [e for e in self.events("biliup") if e.get("cmd") == "upload"]

    def debug(self) -> str:
        parts = [f"[{self.case.name}]"]
        for code, out, err, secs in self.outs:
            parts.append(f"exit={code} {secs:.1f}s out={json.dumps(out, ensure_ascii=False)[:1500]}\n"
                         f"stderr={err[-1500:]}")
        parts.append(f"sim-log={json.dumps(h.events(self.log), ensure_ascii=False)[-3000:]}")
        parts.append(h.run_log(self.home)[-3000:])
        return "\n".join(parts)


def _run_case(tmp_path: Path, case: Case) -> Run:
    root = tmp_path / case.name
    home = h.home_for(root, biliup=case.biliup, logged_in=case.logged_in, extra=case.toml)
    if case.bom:
        account = home / "accounts" / "bilibili" / "main.json"
        account.write_bytes(b"\xef\xbb\xbf" + account.read_bytes())
    post = h.write_post(root / "我的 帖子" / "第1期", case.kind, images=case.images, bili=case.bili, sizes=case.sizes,
                        **case.fields)
    log = root / "sim.jsonl"
    outs = []
    for args in case.runs or [["publish", "{post}", "--yes", "--json"]]:
        args = [a.replace("{post}", str(post / "post.yaml")) for a in args]
        outs.append(h.run(args, home, log, case.scenario, timeout=240, display=case.display))
    return Run(case, outs, home, log, post)


def run_all(tmp_path: Path, cases: list[Case]) -> dict[str, Run]:
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        runs = list(ex.map(lambda c: _run_case(tmp_path, c), cases))
    return {r.case.name: r for r in runs}


def check_all(runs: dict[str, Run], checks: dict[str, Callable[[Run], None]]) -> None:
    """逐个场景断言，收集所有失败一起报（一次跑完能看到全部问题）。"""
    assert set(checks) == set(runs), set(runs) ^ set(checks)
    failures = []
    for name, check in checks.items():
        run = runs[name]
        try:
            check(run)
        except AssertionError as exc:
            failures.append(f"{name}: {exc}\n{run.debug()}")
    assert not failures, "\n\n".join(failures)


def schedule_at(hours: float) -> str:
    return (datetime.now() + timedelta(hours=hours)).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def epoch(when: str) -> int:
    return int(datetime.strptime(when, "%Y-%m-%d %H:%M").timestamp())


def P(*extra: str) -> list[str]:
    return ["publish", "{post}", "--yes", "--json", *extra]


# ---------------------------------------------------------------- 断言片段

def last_history(run: Run) -> dict[str, Any]:
    hist = h.history(run.home)
    assert hist, "没有发布记录"
    return hist[-1]


def video_ok(run: Run, status: str = "submitted", i: int = 0) -> None:
    code, _, _, secs = run.outs[i]
    r = run.result_of(i)
    assert code == 0 and r["status"] == status, r
    assert r["url"] == VIDEO_URL and r["item_id"] == BVID, r
    assert r["submit_started"] is True, r
    assert secs < 30, f"耗时 {secs:.0f} 秒"


def note_ok(run: Run, status: str = "published", i: int = 0) -> None:
    code, _, _, secs = run.outs[i]
    r = run.result_of(i)
    assert code == 0 and r["status"] == status, r
    assert r["url"] == OPUS_URL and r["item_id"] == DYN_ID, r
    assert r["submit_started"] is True, r
    assert secs < 50, f"耗时 {secs:.0f} 秒"


def not_sent(run: Run, status: str, code: str) -> None:
    """发布之前就停下：没有点发布、没有发布请求、没有标记 submit_started，查重不会拦下一次。"""
    r = run.result
    assert r["status"] == status and r["error_code"] == code, r
    assert r["submit_started"] is False, r
    assert run.clicks == 0 and not run.creates, (run.clicks, len(run.creates))
    hist = last_history(run)
    assert hist["error_code"] == code and hist["submit_started"] is False, hist


def skipped_as_duplicate(run: Run, i: int) -> None:
    r = run.result_of(i)
    assert run.outs[i][0] == 0 and r["status"] == "skipped" and "--force" in r["message"], r


def option_of(run: Run) -> dict[str, Any]:
    assert len(run.creates) == 1, f"发布请求 {len(run.creates)} 次"
    return run.creates[0]["body"]["dyn_req"]["option"]


def biliup_opts(run: Run, i: int = 0) -> dict[str, Any]:
    assert len(run.uploads) > i, "biliup upload 没有被调用"
    return run.uploads[i]


# ================================================================ 视频（biliup）

@requires_chromium
@posix_only
def test_video_daily(tmp_path: Path):
    when = schedule_at(3)
    cases = [
        Case("ok"),
        Case("schedule", fields={"schedule": when}),
        Case("dedupe_force", runs=[P(), P(), P("--force")]),
        # 标题以 - 开头、80 字；12 个标签；各种开关；Windows 下 biliup 的本地时间格式
        Case("full_options", scenario={"biliup_time": "local"},
             fields={"title": "-" + "长" * 79, "tags": [f"标签{i}" for i in range(12)], "ai_generated": True},
             bili={"submit": "web", "line": "bda2", "limit": 5, "tid_v2": 2001, "dynamic": "-开头的粉丝动态",
                   "no_reprint": True, "close_danmu": True, "copyright": 2, "source": "https://example.com/源"}),
        # mpub setup 装的位置（没有 biliup_path）
        Case("managed", biliup="managed"),
        Case("no_bvid", scenario={"biliup": "no_bvid"}),
        # 上传 8 秒、期间没有任何输出（管道里 biliup 不显示进度条）
        Case("slow_upload", scenario={"biliup_upload_seconds": 8}),
        # 登录文件带 BOM（biliup 的 serde_json 不认，真 biliup 报 expected value at line 1 column 1）
        Case("bom", bom=True),
        # 数据目录在带中文和空格的路径下（Windows 中文用户名）
        Case("中文 用户"),
    ]
    runs = run_all(tmp_path, cases)

    def ok(run: Run) -> None:
        video_ok(run)
        call = biliup_opts(run)
        argv = call["argv"]
        assert argv[:5] == ["--rust-log", "info", "-u", str(run.home / "accounts" / "bilibili" / "main.json"),
                            "upload"], argv
        assert argv[-2] == "--" and argv[-1] == str(run.post / "视频 第1期.mp4"), argv
        assert call["opts"] == {"--title": "三分钟讲清楚 Git 分支", "--desc": "用三分钟讲清楚 Git 分支。\n第二行简介。",
                                "--tid": "231", "--tag": "编程,Git", "--copyright": "1",
                                "--cover": str(run.post / "封面 1.jpg")}, call["opts"]
        assert call["no_color"] == "1"
        versions = [e for e in run.events("biliup") if e.get("cmd") == "version"]
        assert len(versions) == 1 and versions[0]["t"] < call["t"], "上传前没有先试运行 --version"
        hist = last_history(run)
        assert hist["status"] == "submitted" and hist["item_id"] == BVID, hist

    def schedule(run: Run) -> None:
        video_ok(run, "scheduled")
        assert biliup_opts(run)["opts"]["--dtime"] == str(epoch(when))

    def dedupe_force(run: Run) -> None:
        video_ok(run)
        skipped_as_duplicate(run, 1)
        video_ok(run, i=2)
        assert len(run.uploads) == 2

    def full_options(run: Run) -> None:
        video_ok(run)
        call = biliup_opts(run)
        o = call["opts"]
        assert o["--title"] == "-" + "长" * 79 and o["--dynamic"] == "-开头的粉丝动态", o
        assert o["--tag"] == ",".join(f"标签{i}" for i in range(12))
        assert o["--submit"] == "web" and o["--line"] == "bda2" and o["--limit"] == "5" and o["--tid-v2"] == "2001"
        assert o["--copyright"] == "2" and o["--source"] == "https://example.com/源"
        assert o["--no-reprint"] == "1" and json.loads(o["--extra-fields"]) == {"creation_statement": {"id": 1}}
        assert call["flags"] == ["--up-close-danmu"]

    def managed(run: Run) -> None:
        video_ok(run)
        assert biliup_opts(run)

    def no_bvid(run: Run) -> None:
        r = run.result
        assert run.code == 0 and r["status"] == "submitted" and r["url"] is None, r
        assert "创作中心" in r["message"]

    def slow_upload(run: Run) -> None:
        video_ok(run)
        assert run.secs > 8

    def bom(run: Run) -> None:
        video_ok(run)
        assert not (run.home / "accounts" / "bilibili" / "main.json").read_bytes().startswith(b"\xef\xbb\xbf")

    def chinese_home(run: Run) -> None:
        video_ok(run)
        assert "中文 用户" in biliup_opts(run)["argv"][3]

    check_all(runs, {"ok": ok, "schedule": schedule, "dedupe_force": dedupe_force, "full_options": full_options,
                     "managed": managed, "no_bvid": no_bvid, "slow_upload": slow_upload, "bom": bom,
                     "中文 用户": chinese_home})


@requires_chromium
@posix_only
def test_video_failures(tmp_path: Path):
    cases = [
        Case("token_expired", scenario={"token": "expired"}),
        Case("biliup_login_error", scenario={"biliup": "login_error"}, runs=[P(), P()]),
        Case("reject", scenario={"biliup": "reject"}, runs=[P(), P()]),
        Case("server_error", scenario={"biliup": "server_error"}, runs=[P(), P()]),
        Case("crash_after_upload", scenario={"biliup": "crash_after_upload"}),
        Case("network_error", scenario={"biliup": "network_error"}, runs=[P(), P()]),
        Case("version_fail", scenario={"biliup_version": "fail"}),
        Case("no_biliup", biliup=False),
        Case("short_timeout", runs=[P("--timeout", "100")]),
    ]
    runs = run_all(tmp_path, cases)

    def token_expired(run: Run) -> None:
        r = run.result
        assert run.code == 3 and r["status"] == "needs_action" and r["error_code"] == "login_required", r
        assert r["submit_started"] is False and not run.uploads and run.secs < 15

    def biliup_login_error(run: Run) -> None:
        r = run.result
        assert run.code == 3 and r["error_code"] == "login_required", r
        # 视频还没开始传：不算发过；消息是 biliup 报告里的原因，不是 backtrace
        assert r["submit_started"] is False, r
        assert "credential validation failed with Bilibili code -101" in r["message"], r["message"]
        assert "backtrace" not in r["message"] and "╴at" not in r["message"], r["message"]
        assert run.result_of(1)["status"] == "needs_action" and len(run.uploads) == 2  # 不被查重拦住

    def reject(run: Run) -> None:
        r = run.result
        assert run.code == 1 and r["status"] == "failed" and r["error_code"] == "platform_error", r
        assert "21012" in r["message"] and r["submit_started"] is True and r["retryable"] is False, r
        assert run.result_of(1)["status"] == "failed" and len(run.uploads) == 2  # 明确被拒：可以改了再发

    def server_error(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "unknown_outcome" and r["submit_started"] is True, r
        skipped_as_duplicate(run, 1)
        assert len(run.uploads) == 1

    def crash_after_upload(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "unknown_outcome" and r["submit_started"] is True, r

    def network_error(run: Run) -> None:
        r = run.result
        assert run.code == 1 and r["error_code"] == "platform_error" and r["submit_started"] is False, r
        assert "Network is unreachable" in r["message"] and "尚未投稿" in r["message"], r["message"]
        assert "backtrace" not in r["message"], r["message"]
        assert run.result_of(1)["error_code"] == "platform_error" and len(run.uploads) == 2

    def version_fail(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "internal" and r["submit_started"] is False and not run.uploads, r
        assert "尚未投稿" in r["message"]

    def no_biliup(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "internal" and "mpub setup" in r["message"], r

    def short_timeout(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "timeout" and r["submit_started"] is False and not run.uploads, r

    check_all(runs, {"token_expired": token_expired, "biliup_login_error": biliup_login_error, "reject": reject,
                     "server_error": server_error, "crash_after_upload": crash_after_upload,
                     "network_error": network_error, "version_fail": version_fail, "no_biliup": no_biliup,
                     "short_timeout": short_timeout})


@requires_chromium
@posix_only
def test_video_hangs(tmp_path: Path):
    """biliup 卡住：上传中卡住 = 超时、确定没投稿（下次可以直接发）；传完后卡住 = 结果未知（查重拦住）。"""
    cases = [
        # 第二次给的时间不够启动 biliup：只看它没有被查重拦成 skipped
        Case("hang_upload", scenario={"biliup": "hang_upload"}, runs=[P("--timeout", "125"), P("--timeout", "100")]),
        Case("hang_after_upload", scenario={"biliup": "hang_after_upload"}, runs=[P("--timeout", "125"), P()]),
    ]
    runs = run_all(tmp_path, cases)

    def hang_upload(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "timeout" and r["submit_started"] is False and "尚未投稿" in r["message"], r
        assert run.secs < 100
        r2 = run.result_of(1)
        assert r2["status"] == "failed" and r2["error_code"] == "timeout", r2  # 没被查重拦成 skipped

    def hang_after_upload(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "unknown_outcome" and r["submit_started"] is True, r
        assert run.secs < 100
        skipped_as_duplicate(run, 1)

    check_all(runs, {"hang_upload": hang_upload, "hang_after_upload": hang_after_upload})


def _reached(log: Path, wait_for: str) -> bool:
    if wait_for == "biliup_upload":  # biliup 已经开始传视频（--version 试运行之后的那次调用）
        return any(e.get("cmd") == "upload" for e in h.events(log, "biliup"))
    return bool(h.events(log, wait_for))


def _interrupt(tmp_path: Path, name: str, kind: str, scenario: dict[str, Any], wait_for: str,
               toml: str = "") -> tuple[int, list[int], Path, Path, Path]:
    root = tmp_path / name
    home = h.home_for(root, extra=toml)
    post = h.write_post(root / "帖子", kind)
    log = root / "sim.jsonl"
    tag = h.new_tag()
    proc = h.start(["publish", str(post), "--yes", "--json"], home, log, scenario, tag=tag)
    deadline = time.monotonic() + 90
    while not _reached(log, wait_for) and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.2)
    assert _reached(log, wait_for), f"等不到 {wait_for}: {h.describe(proc.poll() or 0, None, '', home, log)}"
    time.sleep(1)
    proc.send_signal(h.SIGTERM)
    proc.communicate(timeout=60)
    time.sleep(2)
    return proc.returncode, h.tagged_pids(tag), home, log, post


@requires_chromium
@posix_only
def test_interrupts(tmp_path: Path):
    """SIGTERM 中途中断：浏览器 / biliup 不留孤儿进程；记录与查重正确。"""
    cases = {
        "video_uploading": ("video", {"biliup": "hang_upload"}, "biliup_upload", False),
        "video_uploaded": ("video", {"biliup": "hang_after_upload"}, "biliup_uploaded", True),
        "note_uploading": ("note", {"upload_ms": 8000}, "upload", False),
        "note_sent": ("note", {"create": "hang"}, "create", True),
    }

    def one(item: tuple[str, tuple[str, dict[str, Any], str, bool]]) -> str:
        name, (kind, scenario, wait_for, sent) = item
        code, orphans, home, log, post = _interrupt(tmp_path, name, kind, scenario, wait_for, CONSENT)
        problems = []
        if orphans:
            problems.append(f"孤儿进程 {orphans}")
        hist = h.history(home)
        if not hist or hist[-1]["submit_started"] is not sent:
            problems.append(f"发布记录不对: {hist}")
        elif sent and hist[-1]["error_code"] != "unknown_outcome":
            problems.append(f"已发出却没记成结果未知: {hist[-1]}")
        code2, out2, err2, _ = h.run(["publish", str(post), "--yes", "--json"], home, log, {})
        status = ((out2 or {}).get("results") or [{}])[-1].get("status")
        if sent and status != "skipped":
            problems.append(f"结果未知却没被查重拦住: {status}")
        if not sent and status not in ("submitted", "published"):
            problems.append(f"没发出却不能重发: {status} {err2[-500:]}")
        return f"{name}: {'; '.join(problems)}" if problems else ""

    with ThreadPoolExecutor(max_workers=4) as ex:
        problems = [p for p in ex.map(one, cases.items()) if p]
    assert not problems, "\n".join(problems)


# ================================================================ 图文动态（浏览器模式）

@requires_chromium
def test_note_daily(tmp_path: Path):
    when = schedule_at(1)
    cases = [
        Case("ok", "note", toml=CONSENT),
        Case("manifest_consent", "note", bili={"confirm_rules": True}),
        # 选文件框只能单选、9 张图、标题 20 字、正文接近 1000 字
        Case("nine_single", "note", toml=CONSENT, images=9, scenario={"pic_multiple": False, "upload_ms": 100},
             fields={"title": "二十个字的标题二十个字的标题二十个字的", "body": "长" * 990}),
        # 页面 6 秒才渲染出发布框、每张图传 2.5 秒、发布接口 3 秒才回
        Case("slow", "note", toml=CONSENT, scenario={"render_delay_ms": 6000, "upload_ms": 2500, "create_delay_ms": 3000}),
        # 定时发布不弹规范确认框：没有同意也能发
        Case("schedule", "note", fields={"schedule": when}),
        Case("aigc_picmode", "note", toml=CONSENT, fields={"ai_generated": True}, bili={"pic_mode": 1}),
        # 带了 dy_spec_agreed 仍然弹出规范确认框：按已有的同意代为确认
        Case("spec_always", "note", toml=CONSENT, scenario={"spec_always": True}),
        # 旧版发布框：点「图片」不会自动弹选文件框
        Case("no_autotrigger", "note", toml=CONSENT, scenario={"pic_autotrigger": False}),
        Case("dedupe_force", "note", toml=CONSENT, runs=[P(), P(), P("--force")]),
        # 网页登录态过期：biliup renew 刷新后继续
        Case("renew", "note", toml=CONSENT, scenario={"nav": "expired_until_renew"}),
        # 扫码登录生成的登录文件直接拿来发图文
        Case("after_tv_login", "note", toml=CONSENT, logged_in=False,
             runs=[["login", "bilibili", "--account", "main", "--json"], P()]),
        Case("no_body", "note", toml=CONSENT, fields={"body": None, "tags": None}),
    ]
    runs = run_all(tmp_path, cases)

    def ok(run: Run) -> None:
        note_ok(run)
        assert run.clicks == 1 and len(run.creates) == 1 and not run.page("spec_popup")
        req = run.creates[0]["body"]["dyn_req"]
        assert req["content"]["title"] == "周末徒步路线"
        assert req["content"]["contents"][0]["raw_text"] == "西山环线 12 公里，\n适合新手。\n#徒步#"
        assert [p["img_src"].rsplit("/", 1)[-1] for p in req["pics"]] == [f"sim{i:02d}10086.png" for i in range(3)]
        assert req["option"] == {"aigc": 2, "pic_mode": 0}
        assert run.creates[0]["csrf_ok"] is True
        assert [u["filename"] for u in run.events("upload")] == ["图 1.png", "图 2.png", "图 3.png"]
        # 发布框自己弹出的选文件框被接管了（有头模式下不会冒出系统的「打开」对话框）
        assert len(run.page("chooser_opened")) == 1, run.page("chooser_opened")
        hist = last_history(run)
        assert hist["status"] == "published" and hist["url"] == OPUS_URL, hist

    def manifest_consent(run: Run) -> None:
        note_ok(run)

    def nine_single(run: Run) -> None:
        note_ok(run)
        req = run.creates[0]["body"]["dyn_req"]
        assert len(req["pics"]) == 9 and len(run.events("upload")) == 9
        assert req["content"]["title"] == "二十个字的标题二十个字的标题二十个字的"
        assert len(run.page("chooser_opened")) == 9

    def slow(run: Run) -> None:
        note_ok(run)
        assert run.clicks == 1 and len(run.creates) == 1

    def schedule(run: Run) -> None:
        note_ok(run, "scheduled")
        assert option_of(run)["timer_pub_time"] == epoch(when)
        assert not run.page("spec_popup") and run.page("publish_clicked")[0]["timing"] is True

    def aigc_picmode(run: Run) -> None:
        note_ok(run)
        assert option_of(run) == {"aigc": 1, "pic_mode": 1}

    def spec_always(run: Run) -> None:
        note_ok(run)
        assert run.page("spec_confirmed") and len(run.creates) == 1

    def no_autotrigger(run: Run) -> None:
        note_ok(run)
        assert len(run.page("chooser_opened")) == 1

    def dedupe_force(run: Run) -> None:
        note_ok(run)
        skipped_as_duplicate(run, 1)
        note_ok(run, i=2)
        assert len(run.creates) == 2

    def renew(run: Run) -> None:
        note_ok(run)
        assert [e for e in run.events("biliup") if e.get("cmd") == "renew"]
        account = json.loads((run.home / "accounts" / "bilibili" / "main.json").read_text(encoding="utf-8"))
        assert account["token_info"]["access_token"] == "renewed-access"

    def after_tv_login(run: Run) -> None:
        assert run.result_of(0)["status"] == "ok", run.result_of(0)
        note_ok(run, i=1)
        assert run.creates[0]["csrf_ok"] is True

    def no_body(run: Run) -> None:
        note_ok(run)
        assert run.creates[0]["body"]["dyn_req"]["content"]["contents"] == []

    check_all(runs, {"ok": ok, "manifest_consent": manifest_consent, "nine_single": nine_single, "slow": slow,
                     "schedule": schedule, "aigc_picmode": aigc_picmode, "spec_always": spec_always,
                     "no_autotrigger": no_autotrigger, "dedupe_force": dedupe_force, "renew": renew,
                     "after_tv_login": after_tv_login, "no_body": no_body})


@requires_chromium
def test_note_failures(tmp_path: Path):
    when = schedule_at(1)
    cases = [
        # 没同意过《动态发布规范》：无头模式在传图之前就停下来问人
        Case("no_consent", "note"),
        Case("renew_fail", "note", toml=CONSENT, scenario={"nav": "expired", "biliup_renew": "fail"}),
        # httpx 检查通过、页面里却是未登录：页头显示「登录」/ 跳到登录页，都要很快停下
        Case("page_logged_out", "note", toml=CONSENT, scenario={"browser_nav": "expired"}),
        Case("page_redirect_login", "note", toml=CONSENT, scenario={"browser_nav": "expired", "logged_out": "redirect"}),
        Case("page_412", "note", toml=CONSENT, scenario={"page": "412"}),
        Case("upload_fail", "note", toml=CONSENT, scenario={"upload_fail": [1]}),
        # 第二张图只有 5x5：发布框本地校验不通过，不会上传
        Case("tiny_image", "note", toml=CONSENT, sizes=[(64, 48), (5, 5), (64, 48)]),
        Case("sched_hidden", "note", fields={"schedule": when}, scenario={"timing": "hidden"}),
        Case("sched_swallow", "note", fields={"schedule": when}, scenario={"timing": "swallow"}),
        Case("aigc_missing", "note", toml=CONSENT, fields={"ai_generated": True}, scenario={"aigc_option": False}),
        Case("check_fail", "note", toml=CONSENT, scenario={"submit_check": 4126001}),
        Case("captcha", "note", toml=CONSENT, scenario={"create": "captcha"}),
        Case("login_popup", "note", toml=CONSENT, scenario={"create": "login_popup"}),
    ]
    runs = run_all(tmp_path, cases)

    def quick(run: Run, limit: float = 20) -> None:
        assert run.secs < limit, f"耗时 {run.secs:.0f} 秒"

    def no_consent(run: Run) -> None:
        not_sent(run, "needs_action", "consent_required")
        assert run.code == 3 and not run.events("upload"), "传了图才问"
        assert "confirm_dynamic_rules" in run.result["message"]

    def renew_fail(run: Run) -> None:
        not_sent(run, "needs_action", "login_required")
        assert [e for e in run.events("biliup") if e.get("cmd") == "renew"]

    def page_logged_out(run: Run) -> None:
        not_sent(run, "needs_action", "login_required")
        quick(run)

    def page_redirect_login(run: Run) -> None:
        not_sent(run, "needs_action", "login_required")
        quick(run)

    def page_412(run: Run) -> None:
        not_sent(run, "needs_action", "risk_control")
        quick(run)
        assert "412" in run.result["message"]

    def upload_fail(run: Run) -> None:
        not_sent(run, "failed", "platform_error")
        assert "图片上传失败" in run.result["message"]

    def tiny_image(run: Run) -> None:
        not_sent(run, "failed", "platform_error")
        assert "10像素" in run.result["message"], run.result["message"]

    def sched_hidden(run: Run) -> None:
        not_sent(run, "failed", "platform_error")
        assert "定时" in run.result["message"]

    def sched_swallow(run: Run) -> None:
        not_sent(run, "failed", "platform_error")
        assert run.page("timing_swallowed") and "定时发布没有生效" in run.result["message"]

    def aigc_missing(run: Run) -> None:
        not_sent(run, "failed", "validation")

    def check_fail(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "platform_error" and "没有发出" in r["message"], r
        assert run.clicks == 1 and not run.creates

    def captcha(run: Run) -> None:
        r = run.result
        assert run.code == 3 and r["error_code"] == "captcha" and not run.creates, r
        assert last_history(run)["error_code"] == "captcha"

    def login_popup(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "login_required" and run.clicks == 1 and not run.creates, r

    check_all(runs, {"no_consent": no_consent, "renew_fail": renew_fail, "page_logged_out": page_logged_out,
                     "page_redirect_login": page_redirect_login, "page_412": page_412, "upload_fail": upload_fail,
                     "tiny_image": tiny_image, "sched_hidden": sched_hidden, "sched_swallow": sched_swallow,
                     "aigc_missing": aigc_missing, "check_fail": check_fail, "captcha": captcha,
                     "login_popup": login_popup})


@requires_chromium
def test_note_after_click(tmp_path: Path):
    """点了发布之后：平台明确拒绝 = 失败（只点一次，可改了再发）；服务端异常 / 没回 = 结果未知（查重拦住）。"""
    when = schedule_at(1)
    wait = CONSENT + "\npublish_wait = 12"
    cases = [
        Case("reject", "note", toml=CONSENT, scenario={"create": "reject"}, runs=[P(), P()]),
        Case("create_412", "note", toml=CONSENT, scenario={"create": "412"}),
        Case("create_352", "note", toml=CONSENT, scenario={"create": "-352"}),
        Case("server_error", "note", toml=CONSENT, scenario={"create": "server_error"}, runs=[P(), P()]),
        Case("html_502", "note", toml=CONSENT, scenario={"create": "html"}),
        Case("no_id", "note", toml=CONSENT, scenario={"create": "no_id"}),
        Case("hang", "note", toml=wait, scenario={"create": "hang"}, runs=[P(), P()]),
        # 页面没把定时带进请求：如实报告立即发布了
        Case("sched_drop", "note", fields={"schedule": when}, scenario={"timing": "drop"}),
    ]
    runs = run_all(tmp_path, cases)

    def sent_once(run: Run) -> None:
        assert run.clicks == 1 and len(run.creates) == 1, (run.clicks, len(run.creates))
        assert run.result["submit_started"] is True

    def reject(run: Run) -> None:
        assert run.clicks == 2 and len(run.creates) == 2, (run.clicks, len(run.creates))  # 每次只点一次
        r = run.result
        assert r["status"] == "failed" and r["error_code"] == "platform_error" and "绑定手机" in r["message"], r
        assert run.result_of(1)["status"] == "failed"  # 不被查重拦住

    def risk(run: Run) -> None:
        sent_once(run)
        r = run.result
        assert run.code == 3 and r["error_code"] == "risk_control", r

    def unknown(run: Run) -> None:
        sent_once(run)
        assert run.result["error_code"] == "unknown_outcome", run.result

    def server_error(run: Run) -> None:
        unknown(run)
        skipped_as_duplicate(run, 1)

    def hang(run: Run) -> None:
        unknown(run)
        assert run.secs < 45, f"耗时 {run.secs:.0f} 秒"
        skipped_as_duplicate(run, 1)

    def sched_drop(run: Run) -> None:
        note_ok(run, "published")
        assert "立即发布" in run.result["message"], run.result

    check_all(runs, {"reject": reject, "create_412": risk, "create_352": risk, "server_error": server_error,
                     "html_502": unknown, "no_id": unknown, "hang": hang, "sched_drop": sched_drop})


@requires_chromium
@pytest.mark.skipif(not shutil.which("Xvfb") and not os.environ.get("DISPLAY"), reason="有头模式需要 X 显示")
def test_note_headed(tmp_path: Path):
    """有头模式：人在浏览器里点「确认并发送」后记住同意，之后无头发布不再询问；人取消 = 没发出；人过了验证码 = 发出。"""
    with h.xvfb() as display:
        cases = [
            Case("spec_confirm", "note", scenario={"human_spec": "confirm"}, display=display,
                 runs=[P("--headed"), P("--force")]),
            Case("spec_cancel", "note", scenario={"human_spec": "cancel"}, display=display, runs=[P("--headed")]),
            Case("captcha_solved", "note", toml=CONSENT, scenario={"create": "captcha", "human_captcha": True},
                 display=display, runs=[P("--headed")]),
        ]
        runs = run_all(tmp_path, cases)

    def spec_confirm(run: Run) -> None:
        note_ok(run)
        note_ok(run, i=1)
        assert len(run.page("spec_popup")) == 1 and len(run.creates) == 2  # 第二次（无头）没有再弹
        assert (run.home / "accounts" / "bilibili" / "main.dy_spec_agreed").is_file()
        assert len(run.page("chooser_opened")) == 2  # 每次一个，都被接管

    def spec_cancel(run: Run) -> None:
        r = run.result
        assert r["status"] == "failed" and "没有发出" in r["message"] and not run.creates, r
        assert not (run.home / "accounts" / "bilibili" / "main.dy_spec_agreed").exists()

    def captcha_solved(run: Run) -> None:
        note_ok(run)
        assert run.page("captcha_solved") and len(run.creates) == 1

    check_all(runs, {"spec_confirm": spec_confirm, "spec_cancel": spec_cancel, "captcha_solved": captcha_solved})


# ================================================================ 图文动态（实验性的 HTTP 模式）

@requires_chromium
def test_note_http_mode(tmp_path: Path):
    when = schedule_at(1)
    http = {"mode": "http"}
    cases = [
        Case("ok", "note", bili=http, fields={"ai_generated": True, "schedule": when}, runs=[P(), P()]),
        Case("create_412", "note", bili=http, scenario={"create": "412"}),
        Case("create_352", "note", bili=http, scenario={"create": "-352"}),
        Case("reject", "note", bili=http, scenario={"create": "reject", "create_code": 4126099,
                                                    "create_message": "内容违规"}),
        Case("server_error", "note", bili=http, scenario={"create": "server_error"}),
        Case("upload_fail", "note", bili=http, scenario={"upload_fail": [0]}),
        Case("check_login", "note", bili=http, scenario={"submit_check": -101}),
        Case("nav_412", "note", bili=http, scenario={"nav": "412"}),
    ]
    runs = run_all(tmp_path, cases)

    def ok(run: Run) -> None:
        note_ok(run, "scheduled")
        create = run.creates[0]
        assert create["via"] == "http" and create["wbi_ok"] is True and create["csrf_ok"] is True, create
        req = create["body"]["dyn_req"]
        assert req["option"] == {"aigc": 1, "pic_mode": 0, "timer_pub_time": epoch(when)}
        assert len(req["pics"]) == 3 and [u["size"] > 0 for u in run.events("upload")] == [True] * 3
        skipped_as_duplicate(run, 1)

    def risk(run: Run) -> None:
        r = run.result
        assert run.code == 3 and r["error_code"] == "risk_control" and len(run.creates) == 1, r

    def reject(run: Run) -> None:
        r = run.result
        assert r["error_code"] == "platform_error" and "4126099" in r["message"], r

    def server_error(run: Run) -> None:
        assert run.result["error_code"] == "unknown_outcome"

    def upload_fail(run: Run) -> None:
        not_sent(run, "failed", "platform_error")
        assert "「图 1.png」" in run.result["message"], run.result["message"]

    def check_login(run: Run) -> None:
        not_sent(run, "needs_action", "login_required")

    def nav_412(run: Run) -> None:
        not_sent(run, "needs_action", "risk_control")

    check_all(runs, {"ok": ok, "create_412": risk, "create_352": risk, "reject": reject,
                     "server_error": server_error, "upload_fail": upload_fail, "check_login": check_login,
                     "nav_412": nav_412})


# ================================================================ 登录 / 检查

@requires_chromium
@posix_only
def test_login_and_check(tmp_path: Path):
    login = ["login", "bilibili", "--account", "main", "--json"]
    check = ["check", "bilibili", "--json"]
    cases = [
        # 扫码（未扫 -> 已扫未确认 -> 成功）-> 登录文件 biliup 能直接用 -> check -> 发视频
        Case("fresh", logged_in=False, runs=[login, check, P()]),
        Case("already", runs=[login]),
        Case("qr_expired", logged_in=False, scenario={"qr_poll": [86039, 86038, 86038, 86038, 86038]}, runs=[login]),
        # 令牌失效时重新扫码，覆盖旧文件
        Case("relogin", scenario={"token": "expired_sim", "qr_poll": [86090, 0]}, runs=[login, check]),
        Case("check_expired", scenario={"nav": "expired"}, runs=[check]),
        Case("check_refresh", scenario={"token": "refresh"}, runs=[check]),
    ]
    runs = run_all(tmp_path, cases)
    account = "accounts/bilibili/main.json"

    def fresh(run: Run) -> None:
        r = run.result_of(0)
        assert run.code == 0 and r["status"] == "ok" and "模拟UP主" in r["message"], r
        assert run.secs < 30
        info = json.loads((run.home / account).read_text(encoding="utf-8"))
        assert info["platform"] == "BiliTV" and info["token_info"]["access_token"] == "tvqr-access"
        assert info["sso"] and info["cookie_info"]["cookies"]
        assert not (run.home / "qrcode" / "bilibili-main.png").exists()  # 登录后删掉二维码
        assert run.result_of(1)["status"] == "ok"
        video_ok(run, i=2)
        qrs = [e for e in run.events("qr") if e["step"] == "poll"]
        assert [e["code"] for e in qrs] == [86039, 86090, 0]
        err = run.outs[0][2]
        assert '"event": "qrcode"' in err and "bilibili-main.png" in err

    def already(run: Run) -> None:
        r = run.result
        assert r["status"] == "ok" and "无需重新扫码" in r["message"] and not run.events("qr"), r
        assert run.secs < 15

    def qr_expired(run: Run) -> None:
        r = run.result
        assert run.code == 1 and r["error_code"] == "timeout" and "过期" in r["message"], r
        assert len([e for e in run.events("qr") if e["step"] == "auth_code"]) == 4
        assert not (run.home / account).exists()

    def relogin(run: Run) -> None:
        assert run.result_of(0)["status"] == "ok", run.result_of(0)
        info = json.loads((run.home / account).read_text(encoding="utf-8"))
        assert info["token_info"]["access_token"] == "tvqr-access"
        assert run.result_of(1)["status"] == "ok", run.result_of(1)

    def check_expired(run: Run) -> None:
        r = run.result
        assert run.code == 3 and r["error_code"] == "login_required", r

    def check_refresh(run: Run) -> None:
        r = run.result
        assert run.code == 0 and "快到期" in r["message"], r

    check_all(runs, {"fresh": fresh, "already": already, "qr_expired": qr_expired, "relogin": relogin,
                     "check_expired": check_expired, "check_refresh": check_refresh})
