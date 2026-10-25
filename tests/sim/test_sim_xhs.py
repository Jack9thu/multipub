"""小红书端到端模拟：真实 CLI（mpub publish / login / check）+ 真 Chromium + 本地仿真页面。

仿真页面与接口见 tests/sim/xhs/（server.py、pages/），场景参数见 server.DEFAULTS；真实网络一律被拦截。
每个测试里互不相关的场景并发跑（各自独立的数据目录和浏览器），整体耗时约为最慢场景的耗时。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ._harness import requires_chromium
from .xhs import helpers as h
from .xhs.server import NOTE_ID

pytestmark = requires_chromium

WORKERS = 6


@dataclass
class Case:
    name: str
    kind: str = "video"
    scenario: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    images: int = 3
    logged_in: bool | str = True
    args: list[str] | None = None  # 默认：publish <post.yaml> --yes --json
    publish_timeout: int = 25


@dataclass
class Run:
    case: Case
    code: int
    out: dict[str, Any] | None
    err: str
    secs: float
    home: Path
    log: Path

    @property
    def result(self) -> dict[str, Any]:
        out = self.out or {}
        rows = out.get("results") or ([out["result"]] if out.get("result") else [])
        assert rows, self.debug()
        return rows[-1]

    def page(self, evt: str) -> list[dict[str, Any]]:
        return h.page_events(self.log, evt)

    @property
    def creates(self) -> list[dict[str, Any]]:
        return h.events(self.log, "create")

    @property
    def clicks(self) -> int:
        return len(self.page("publish_clicked"))

    def cli_events(self, name: str) -> list[dict[str, Any]]:
        rows = []
        for line in self.err.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event") == name:
                rows.append(ev)
        return rows

    def debug(self) -> str:
        return f"[{self.case.name}] {self.secs:.1f}s\n" + h.describe(self.code, self.out, self.err, self.home, self.log)


def _run_case(tmp_path: Path, case: Case) -> Run:
    root = tmp_path / case.name
    home = h.home_for(root, publish_timeout=case.publish_timeout, logged_in=case.logged_in)
    log = root / "sim.jsonl"
    if case.args is None:
        post = h.write_post(root / "我的 帖子", case.kind, images=case.images, **case.fields)
        args = ["publish", str(post / "post.yaml"), "--yes", "--json"]
    else:
        args = case.args
    code, out, err, secs = h.run(args, home, log, case.scenario, timeout=240)
    return Run(case, code, out, err, secs, home, log)


def run_all(tmp_path: Path, cases: list[Case]) -> dict[str, Run]:
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        runs = list(ex.map(lambda c: _run_case(tmp_path, c), cases))
    return {r.case.name: r for r in runs}


def check_all(runs: dict[str, Run], checks: dict[str, Callable[[Run], None]]) -> None:
    """逐个场景断言，收集所有失败一起报（一次跑完能看到全部问题）。"""
    failures = []
    for name, check in checks.items():
        run = runs[name]
        try:
            check(run)
        except AssertionError as exc:
            failures.append(f"{name}: {exc}\n{run.debug()}")
    assert not failures, "\n\n".join(failures)


def schedule_at(hours: float = 3) -> str:
    return (datetime.now() + timedelta(hours=hours)).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def body_of(run: Run) -> dict[str, Any]:
    assert len(run.creates) == 1, f"创建笔记请求 {len(run.creates)} 次"
    return run.creates[0]["body"]


# ---------------------------------------------------------------- 断言片段

def ok_submitted(run: Run, status: str = "submitted", *, max_secs: float = 60) -> None:
    r = run.result
    assert run.code == 0, f"exit={run.code}"
    assert r["status"] == status, r
    assert r["item_id"] == NOTE_ID, r
    assert r["submit_started"] is True
    assert run.clicks == 1 and len(run.creates) == 1, (run.clicks, len(run.creates))
    assert run.secs < max_secs, f"耗时 {run.secs:.0f} 秒"
    hist = h.history(run.home)
    assert hist and hist[-1]["status"] == status and hist[-1]["item_id"] == NOTE_ID, hist


def failed_before_submit(run: Run, code: str, status: str = "failed") -> None:
    r = run.result
    assert r["status"] == status and r["error_code"] == code, r
    assert r["submit_started"] is False, r
    assert run.clicks == 0 and not run.creates, (run.clicks, len(run.creates))
    hist = h.history(run.home)
    assert hist and hist[-1]["error_code"] == code, hist


# ---------------------------------------------------------------- 发布：日常成功路径

def test_publish_success_paths(tmp_path: Path):
    when = schedule_at()
    cases = [
        Case("video_ok"),
        Case("note_ok", "note", images=18, scenario={"image_upload_seconds": 0.3}),
        Case("note_no_title", "note", fields={"title": None, "body": "这是一段很长的正文，用来当作标题的来源，超过二十个字的部分会被截掉。"}),
        # 图片还在后台上传、标题框早就出来了；上传中点「发布」只会弹提示
        Case("note_slow_images_toast", "note", scenario={"image_upload_seconds": 2.5, "while_uploading": "toast"}),
        # 视频上传慢（15 秒）、页面渲染慢、发布按钮上传完才可用
        Case("video_slow_upload", scenario={"upload_seconds": 15, "render_delay_ms": 3000, "while_uploading": "disabled"}),
        Case("video_success_bare_url", scenario={"success_redirect": "bare"}),
        # 创建接口成功但页面不跳成功页：宽限后按成功处理（不重复点击）
        Case("video_receipt_no_redirect", scenario={"success_redirect": "none"}),
        Case("video_schedule", fields={"schedule": when}),
        Case("note_schedule", "note", fields={"schedule": when}),
        # 上传失败一次，自动重传
        Case("video_upload_retry", scenario={"upload_fail": 1}),
        # 发布页恢复了上次的草稿（正文没有 placeholder）
        Case("video_draft", scenario={"draft": "上次没发出去的草稿内容"}),
        # 侧栏「发布笔记」也是 <button>
        Case("video_sidebar_button", scenario={"sidebar_button": True}),
        Case("video_ai_declaration", fields={"ai_generated": True}, scenario={"decl_popover": "body"}),
        Case("video_repost", fields={"platforms": {"xiaohongshu": {"repost_source": "某某 日报"}}}),
        # 显式指定内容类型声明
        Case("video_fiction_declaration", fields={"platforms": {"xiaohongshu": {"declaration": "虚构演绎，仅供娱乐"}}}),
        # 一输入 # 就弹热门话题、联想结果稍后才刷新；正文里自己写了 #话题 / @某人
        Case("video_topics_instant", scenario={"topics": "instant", "topic_delay_ms": 800},
             fields={"body": "周末去爬山 #徒步\n第二行 @小明\n最后一行"}),
        Case("video_cover", fields={"cover": "封面 图.png"}),
        Case("video_cover_direct", fields={"cover": "封面 图.png"}, scenario={"cover": "direct"}),
    ]
    runs = run_all(tmp_path, cases)

    def video_ok(run: Run) -> None:
        ok_submitted(run, max_secs=40)
        common = body_of(run)["common"]
        assert common["title"] == "三分钟讲清楚 Git"
        assert [t["name"] for t in common["hash_tag"]] == ["编程", "Git"]
        assert common["desc"].startswith("用三分钟讲清楚 Git 分支。\n第二行。")
        assert "笔记 ID" in run.result["message"]

    def note_ok(run: Run) -> None:
        ok_submitted(run)
        assert len(body_of(run)["image_info"]["images"]) == 18
        assert run.page("files")[0]["names"][0] == "图 1.png"

    def note_no_title(run: Run) -> None:
        ok_submitted(run)
        assert body_of(run)["common"]["title"] == "这是一段很长的正文，用来当作标题的来源，"

    def slow_images(run: Run) -> None:
        ok_submitted(run)
        assert not run.page("toast") or all("上传中" not in t["text"] for t in run.page("toast"))
        assert run.page("publish_clicked")[0]["uploadDone"] is True

    def scheduled(run: Run) -> None:
        ok_submitted(run, "scheduled")
        want = datetime.strptime(when, "%Y-%m-%d %H:%M")
        assert body_of(run)["common"]["post_time"] == int(want.timestamp() * 1000)
        assert run.page("publish_clicked")[0]["text"] == "定时发布"

    def upload_retry(run: Run) -> None:
        ok_submitted(run)
        assert len(run.page("upload_failed")) == 1 and len(run.page("files")) == 2

    def draft(run: Run) -> None:
        ok_submitted(run)
        desc = body_of(run)["common"]["desc"]
        assert "草稿" not in desc and "用三分钟讲清楚 Git 分支。" in desc

    def sidebar(run: Run) -> None:
        ok_submitted(run)
        assert not run.page("sidebar_publish_clicked")

    def ai(run: Run) -> None:
        ok_submitted(run)
        assert body_of(run)["common"]["declaration"] == "笔记含AI合成内容"
        assert "注意" not in run.result["message"]

    def fiction(run: Run) -> None:
        ok_submitted(run)
        assert body_of(run)["common"]["declaration"] == "虚构演绎，仅供娱乐"
        assert "注意" not in run.result["message"]

    def repost(run: Run) -> None:
        ok_submitted(run)
        common = body_of(run)["common"]
        assert common["declaration"] == "来源转载" and common["repost_source"] == "某某 日报"
        assert len(run.page("repost_dialog")) == 1  # 原生 click 冒泡不会弹出好几层媒体名称框

    def topics_instant(run: Run) -> None:
        ok_submitted(run)
        common = body_of(run)["common"]
        # 只挂上同名话题（不是先弹出来的热门话题）；正文里的 #徒步 原样保留、换行没被回车选话题吞掉
        assert [t["name"] for t in common["hash_tag"]] == ["编程", "Git"], common
        assert common["desc"].startswith("周末去爬山 #徒步\n第二行 @小明\n最后一行"), common["desc"]

    def cover(run: Run) -> None:
        ok_submitted(run)
        assert body_of(run)["video_info"]["cover"] == "封面 图.png"
        assert "注意" not in run.result["message"]

    check_all(runs, {
        "video_ok": video_ok,
        "note_ok": note_ok,
        "note_no_title": note_no_title,
        "note_slow_images_toast": slow_images,
        "video_slow_upload": lambda r: ok_submitted(r),
        "video_success_bare_url": lambda r: ok_submitted(r),
        "video_receipt_no_redirect": lambda r: ok_submitted(r),
        "video_schedule": scheduled,
        "note_schedule": scheduled,
        "video_upload_retry": upload_retry,
        "video_draft": draft,
        "video_sidebar_button": sidebar,
        "video_ai_declaration": ai,
        "video_repost": repost,
        "video_fiction_declaration": fiction,
        "video_topics_instant": topics_instant,
        "video_cover": cover,
        "video_cover_direct": cover,
    })


# ---------------------------------------------------------------- 发布：失败、拒绝、卡住

def test_publish_failure_paths(tmp_path: Path):
    when = schedule_at()
    cases = [
        Case("expired_cookie", logged_in="expired"),
        # cookie 校验刚通过、打开发布页时掉线
        Case("session_drop_video", scenario={"session_drop_after": 1}),
        Case("session_drop_note", "note", scenario={"session_drop_after": 1}),
        Case("platform_reject", scenario={"api": "reject"}),
        Case("gateway_500", scenario={"api": "error500"}),
        Case("api_hang", scenario={"api": "hang"}),
        Case("captcha_after_click", scenario={"api": "captcha"}),
        # 风控：编辑区一出来就弹人机验证（无头模式）
        Case("captcha_on_load", scenario={"captcha_on_load": True}),
        Case("schedule_switch_swallowed", fields={"schedule": when}, scenario={"schedule_switch": "swallow"}),
        Case("schedule_time_reverted", fields={"schedule": when}, scenario={"schedule_switch": "revert"}),
        Case("repost_unavailable", fields={"platforms": {"xiaohongshu": {"repost_source": "某某日报"}}},
             scenario={"declaration": "none"}),
        # 页面上没有指定的声明选项（模拟页没有「内容包含营销广告」）→ 拒绝发布
        Case("declaration_option_missing", fields={"platforms": {"xiaohongshu": {"declaration": "内容包含营销广告"}}}),
        Case("video_upload_keeps_failing", scenario={"upload_fail": 9}),
        Case("note_image_failed_block", "note", scenario={"image_fail": [1]}),
        Case("note_image_failed_drop", "note", scenario={"image_fail": [1], "image_fail_mode": "drop"}),
        # 封面弹窗没有上传入口 / 「确定」后不关：不再把弹窗留在页面上挡住「发布」
        Case("cover_no_upload_entry", fields={"cover": "封面 图.png"}, scenario={"cover": "broken"}),
        Case("cover_modal_stuck", fields={"cover": "封面 图.png"}, scenario={"cover": "stuck"}),
    ]
    runs = run_all(tmp_path, cases)

    def expired(run: Run) -> None:
        failed_before_submit(run, "login_required", "needs_action")
        assert run.code == 3 and run.secs < 30, (run.code, run.secs)

    def session_drop(run: Run) -> None:
        failed_before_submit(run, "login_required", "needs_action")
        assert "mpub login xiaohongshu" in run.result["message"]

    def reject(run: Run) -> None:
        r = run.result
        assert r["status"] == "failed" and r["error_code"] == "platform_error", r
        assert "标题或正文含有违规内容" in r["message"]
        assert run.clicks == 1 and len(run.creates) == 1  # 不会反复点
        assert r["item_id"] is None

    def unknown(run: Run, *, detail: str = "") -> None:
        r = run.result
        assert r["status"] == "failed" and r["error_code"] == "unknown_outcome", r
        assert r["submit_started"] is True and r["item_id"] is None
        assert run.clicks == 1 and len(run.creates) == 1, (run.clicks, len(run.creates))
        assert detail in r["message"], r["message"]
        assert h.history(run.home)[-1]["error_code"] == "unknown_outcome"

    def captcha_on_load(run: Run) -> None:
        failed_before_submit(run, "captcha", "needs_action")
        assert "--headed" in run.result["message"]

    def schedule_failed(run: Run) -> None:
        failed_before_submit(run, "validation")
        assert "定时发布没能设置上" in run.result["message"]

    def repost_failed(run: Run) -> None:
        failed_before_submit(run, "validation")
        assert "来源转载" in run.result["message"]

    def declaration_failed(run: Run) -> None:
        failed_before_submit(run, "validation")
        assert "内容包含营销广告" in run.result["message"] and "笔记没有发出" in run.result["message"]

    def upload_failed(run: Run) -> None:
        failed_before_submit(run, "platform_error")
        assert run.result["retryable"] is True
        assert "笔记没有发出" in run.result["message"]

    def cover_warned(run: Run) -> None:
        ok_submitted(run)
        assert "自定义封面没能设置上" in run.result["message"]
        opened, closed = run.page("cover_modal"), [e for e in run.page("cover_modal") if not e["open"]]
        assert opened and closed, "封面弹窗没有被关掉"

    check_all(runs, {
        "expired_cookie": expired,
        "session_drop_video": session_drop,
        "session_drop_note": session_drop,
        "platform_reject": reject,
        "gateway_500": lambda r: unknown(r, detail="HTTP 500"),
        "api_hang": unknown,
        "captcha_after_click": lambda r: unknown(r, detail="人机验证"),
        "captcha_on_load": captcha_on_load,
        "schedule_switch_swallowed": schedule_failed,
        "schedule_time_reverted": schedule_failed,
        "repost_unavailable": repost_failed,
        "declaration_option_missing": declaration_failed,
        "video_upload_keeps_failing": upload_failed,
        "note_image_failed_block": upload_failed,
        "note_image_failed_drop": upload_failed,
        "cover_no_upload_entry": cover_warned,
        "cover_modal_stuck": cover_warned,
    })


# ---------------------------------------------------------------- 查重 / --force

def test_rerun_is_skipped_and_force_republishes(tmp_path: Path):
    home = h.home_for(tmp_path)
    post = h.write_post(tmp_path / "帖子 一", "note")
    log = tmp_path / "sim.jsonl"
    args = ["publish", str(post / "post.yaml"), "--yes", "--json"]

    code, out, err, _ = h.run(args, home, log)
    assert code == 0 and h.result_of(out)["status"] == "submitted", h.describe(code, out, err, home, log)
    code, out, err, secs = h.run(args, home, log)
    r = h.result_of(out)
    assert r["status"] == "skipped" and "--force" in r["message"], h.describe(code, out, err, home, log)
    assert secs < 15 and len(h.events(log, "create")) == 1
    code, out, err, _ = h.run(args + ["--force"], home, log)
    assert code == 0 and h.result_of(out)["status"] == "submitted", h.describe(code, out, err, home, log)
    assert len(h.events(log, "create")) == 2
    assert [row["status"] for row in h.history(home)] == ["submitted", "submitted"]


def test_unknown_outcome_blocks_rerun(tmp_path: Path):
    home = h.home_for(tmp_path, publish_timeout=15)
    post = h.write_post(tmp_path / "帖子", "video")
    log = tmp_path / "sim.jsonl"
    args = ["publish", str(post / "post.yaml"), "--yes", "--json"]
    code, out, err, _ = h.run(args, home, log, {"api": "hang"})
    assert h.result_of(out)["error_code"] == "unknown_outcome", h.describe(code, out, err, home, log)
    code, out, err, _ = h.run(args, home, log)
    r = h.result_of(out)
    assert r["status"] == "skipped" and "结果未知" in r["message"], h.describe(code, out, err, home, log)
    assert len(h.events(log, "create")) == 1


# ---------------------------------------------------------------- 中断

def _interrupt(tmp_path: Path, name: str, scenario: dict[str, Any], wait_evt: str) -> dict[str, Any]:
    root = tmp_path / name
    home = h.home_for(root, publish_timeout=60)
    post = h.write_post(root / "帖子", "video")
    log = root / "sim.jsonl"
    tag = h.new_tag()
    proc = h.start(["publish", str(post / "post.yaml"), "--yes", "--json"], home, log, scenario, tag=tag)
    try:
        end = time.monotonic() + 90
        while not h.page_events(log, wait_evt):
            assert time.monotonic() < end and proc.poll() is None, f"等不到 {wait_evt}"
            time.sleep(0.3)
        time.sleep(1.5)
        proc.send_signal(h.SIGTERM)
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    end = time.monotonic() + 15
    while h.tagged_pids(tag) and time.monotonic() < end:
        time.sleep(0.5)
    return {"code": proc.returncode, "orphans": h.tagged_pids(tag), "history": h.history(home), "err": err[-2000:]}


def test_sigterm_mid_publish_leaves_no_orphans(tmp_path: Path):
    with ThreadPoolExecutor(max_workers=2) as ex:
        during_upload = ex.submit(_interrupt, tmp_path, "upload", {"upload_seconds": 40}, "files")
        after_click = ex.submit(_interrupt, tmp_path, "click", {"api": "hang"}, "publish_clicked")
        up, ck = during_upload.result(), after_click.result()
    assert up["code"] != 0 and not up["orphans"], up
    assert [(r["status"], r["error_code"], r["submit_started"]) for r in up["history"]] == \
        [("failed", "internal", False)], up
    assert ck["code"] != 0 and not ck["orphans"], ck
    assert [(r["status"], r["error_code"], r["submit_started"]) for r in ck["history"]] == \
        [("failed", "unknown_outcome", True)], ck


# ---------------------------------------------------------------- 登录 / 检查

LOGIN = ["login", "xiaohongshu", "--account", "main", "--headless", "--json"]
CHECK = ["check", "xiaohongshu", "--account", "main", "--json"]


def test_login_and_check(tmp_path: Path):
    cases = [
        Case("qr_default", logged_in=False, args=LOGIN, scenario={"login_panel": "qrcode", "login_after": 3}),
        # 默认手机号登录，点右上角 img.css-wemwzq 切到扫码
        Case("qr_switch", logged_in=False, args=LOGIN, scenario={"login_panel": "switch", "login_after": 3}),
        # 切换图标的哈希类名、「扫码登录」文字都改版了：点登录框右上角的图标
        Case("qr_switch_renamed", logged_in=False, args=LOGIN, scenario={"login_panel": "nosw", "login_after": 3}),
        # 根本切不过去：上报整页截图，照样等登录完成
        Case("qr_unreachable", logged_in=False, args=LOGIN, scenario={"login_panel": "broken", "login_after": 15}),
        # 二维码 5 秒过期：自动点刷新并重新上报
        Case("qr_expires", logged_in=False, args=LOGIN,
             scenario={"login_panel": "switch", "login_after": 12, "qr_ttl": 5}),
        Case("already_logged_in", args=LOGIN),
        Case("expired_relogin", logged_in="expired", args=LOGIN, scenario={"login_panel": "switch", "login_after": 3}),
        Case("check_ok", args=CHECK),
        Case("check_expired", logged_in="expired", args=CHECK),
        Case("check_missing", logged_in=False, args=CHECK),
    ]
    runs = run_all(tmp_path, cases)

    def logged_in(run: Run, *, qrcodes: int | None = 1, fallback: bool = False) -> None:
        r = run.result
        assert run.code == 0 and r["status"] == "ok", r
        account = json.loads((run.home / "accounts" / "xiaohongshu" / "main.json").read_text(encoding="utf-8"))
        session = next(c["value"] for c in account["cookies"] if c["name"] == "web_session")
        assert session.startswith("valid-"), session  # 扫码后种下的新 cookie
        events = run.cli_events("qrcode")
        if qrcodes is not None:
            assert len(events) == qrcodes, events
        if events:
            assert ("登录页截图" in events[0]["note"]) is fallback, events
        assert not list((run.home / "qrcode").glob("*.png")), "二维码图片没有清理"
        assert run.secs < 60, f"耗时 {run.secs:.0f} 秒"

    def qr_expires(run: Run) -> None:
        logged_in(run, qrcodes=None)
        assert len(run.page("qr_refreshed")) >= 1
        assert len(run.cli_events("qrcode")) >= 2  # 刷新后的新二维码重新上报

    def unreachable(run: Run) -> None:
        logged_in(run, fallback=True)
        assert "--headless" in run.cli_events("qrcode")[0]["note"]

    def already(run: Run) -> None:
        r = run.result
        assert run.code == 0 and r["status"] == "ok" and "无需重新扫码" in r["message"], r
        assert not run.cli_events("qrcode")

    def check_ok(run: Run) -> None:
        assert run.code == 0 and run.result["status"] == "ok", run.result

    def check_bad(run: Run) -> None:
        r = run.result
        assert run.code == 3 and r["status"] == "needs_action" and r["error_code"] == "login_required", r

    check_all(runs, {
        "qr_default": logged_in,
        "qr_switch": logged_in,
        "qr_switch_renamed": logged_in,
        "qr_unreachable": unreachable,
        "qr_expires": qr_expires,
        "already_logged_in": already,
        "expired_relogin": logged_in,
        "check_ok": check_ok,
        "check_expired": check_bad,
        "check_missing": check_bad,
    })
