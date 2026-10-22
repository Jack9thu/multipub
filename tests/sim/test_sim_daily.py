"""日常使用端到端模拟：像用户和 Claude Code skill 那样一步步跑真实的 mpub 命令。

- 假平台 fake / fake2 / fake3 / fake4（不开浏览器）给出各种结果：成功、失败、需要处理、结果未知、慢、卡住；
- simweb 真开 Chromium，页面是本地仿页面（tests/sim/daily/simplat.py），用来验证浏览器进程的清理、
  登录失效、上传慢、平台拒绝；
- 真实的四个平台只做 validate / --dry-run（不联网）。

数据目录、清单目录、工作目录都带空格和中文；每个测试的场景互不相关，部分并发跑以缩短耗时。
"""

from __future__ import annotations

import json
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ._harness import requires_chromium
from .daily.helpers import Sim, events, read_stderr_until, wait_for, write_media, write_storage_state

posix_only = pytest.mark.skipif(sys.platform == "win32" or not Path("/proc").is_dir(),
                                reason="用 /proc 和 POSIX 信号检查进程")

RESULT_KEYS = {"platform", "account", "action", "status", "kind", "message", "url", "item_id", "error_code",
               "retryable", "submit_started", "evidence", "started_at", "finished_at", "ok"}


def _post(sim: Sim, name: str, text: str, images: int = 3) -> Path:
    d = sim.root / "我的 帖子" / name
    d.mkdir(parents=True, exist_ok=True)
    write_media(d, images)
    (d / "post.yaml").write_text(text, encoding="utf-8")
    return d


def _later(hours: float = 3) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- 准备清单

def test_new_edit_validate_dry_run_like_a_user(tmp_path: Path):
    """mpub new → 用户改 post.yaml（中文、emoji、引号、数字标题、别名、字符串标签、相对路径、body_file）→ validate → dry-run。"""
    sim = Sim(tmp_path)
    posts = sim.root / "我的 帖子"
    for kind in ("video", "note", "article"):
        code, out, err = sim.run(["new", str(posts / f"第 1 期 {kind}"), "--kind", kind, "--json"])
        assert code == 0 and out["ok"] and out["kind"] == kind and Path(out["path"]).is_file(), sim.debug(code, out, err)
        # 模板原样能被解析
        code, out, err = sim.run(["validate", str(posts / f"第 1 期 {kind}"), "--json"])
        assert code in (0, 2) and out["kind"] == kind and "issues" in out, sim.debug(code, out, err)
    code, out, err = sim.run(["new", str(posts / "第 1 期 video"), "--kind", "video", "--json"])
    assert code == 2 and out["error"] == "exists"

    # 用户把素材放在另一个带空格的目录，正文写在单独的文件里，标题是纯数字时间
    d = posts / "第 1 期 note"
    write_media(d)
    (d / "正文.md").write_text("\ufeff第一行 \"引号\" 'single'\n第二行 🔥\n", encoding="utf-8")
    (d / "post.yaml").write_text(f"""
kind: note
title: 12:30
body_file: 正文.md
tags: "徒步，周末 #户外"
images: ['images/图 1.jpg', "images/图 2.jpg", images/图 3.jpg]
schedule: {_later()}
account: 007
platforms:
  B站: {{}}
  抖音: {{title: 1.10}}
  xhs:
    title: no
  知乎: false
""", encoding="utf-8")
    code, out, err = sim.run(["validate", str(d), "--json"])
    assert code == 0 and out["ok"], sim.debug(code, out, err)
    by = {p["platform"]: p for p in out["posts"]}
    assert set(by) == {"bilibili", "douyin", "xiaohongshu"}
    # YAML 会把这些读成 750 / 1.1 / False / 7：必须按原文发
    assert by["bilibili"]["title"] == "12:30" and by["douyin"]["title"] == "1.10" and by["xiaohongshu"]["title"] == "no"
    assert {p["account"] for p in by.values()} == {"007"}
    assert by["douyin"]["body"] == "第一行 \"引号\" 'single'\n第二行 🔥"
    assert by["douyin"]["tags"] == ["徒步", "周末", "户外"]
    assert by["douyin"]["images"][0].endswith("图 1.jpg") and Path(by["douyin"]["images"][0]).is_absolute()
    # 没登录的账号给出提示（不联网，只看登录文件）
    assert any(i["field"] == "account" and "mpub login douyin --account 007" in i["message"] for i in out["issues"])

    code, out, err = sim.run(["publish", str(d / "post.yaml"), "--dry-run", "--json"])
    assert code == 0 and out["ok"] and [r["status"] for r in out["results"]] == ["dry_run"] * 3, sim.debug(code, out, err)
    assert set(out["results"][0]) >= RESULT_KEYS and "issues" in out and "run_id" in out

    # 只发其中两个平台：中文逗号、别名
    code, out, err = sim.run(["publish", str(d), "--dry-run", "--to", "抖音，xhs", "--json"])
    assert code == 0 and [r["platform"] for r in out["results"]] == ["douyin", "xiaohongshu"]


@pytest.mark.parametrize("text,needle", [
    ("tags: #早餐 #打卡\n", "tags 里的「#早餐 #打卡」"),
    ("body: 今天去徒步了 #徒步 #周末\n", "body 里的「#徒步 #周末」"),
    ("tags:\n  - #早餐\n  - 打卡\n", "tags 里的「#早餐」"),
    ('video: "D:\\视频\\a.mp4"\n', "Windows 路径请用单引号"),
    ("cover: [未闭合\n", "YAML 解析失败"),
])
def test_yaml_pitfalls_are_reported_not_silently_dropped(tmp_path: Path, text: str, needle: str):
    sim = Sim(tmp_path)
    d = _post(sim, "坑", "kind: note\ntitle: 早安\nimages: ['images/图 1.jpg']\nplatforms: [xhs]\n" + text)
    code, out, err = sim.run(["validate", str(d), "--json"])
    assert code == 2 and out["error"] == "manifest" and needle in out["message"], sim.debug(code, out, err)


def test_hashtags_inside_quotes_or_blocks_are_fine(tmp_path: Path):
    sim = Sim(tmp_path)
    d = _post(sim, "引号", """kind: note
title: "早安 #打卡"   # 这是注释
body: |
  今天去徒步了 #徒步
  tags: #不是字段
tags: "#早餐 #打卡"
images: ['images/图 1.jpg']
platforms: [xhs]
""")
    code, out, err = sim.run(["validate", str(d), "--json"])
    assert code == 0, sim.debug(code, out, err)
    p = out["posts"][0]
    assert p["title"] == "早安 #打卡" and p["tags"] == ["早餐", "打卡"] and "#徒步" in p["body"]


def test_windows_path_escapes_get_a_hint(tmp_path: Path):
    sim = Sim(tmp_path)
    d = _post(sim, "转义", 'kind: video\ntitle: t\nvideo: "D:\\new\\test.mp4"\nplatforms: [fake]\n')
    code, out, err = sim.run(["validate", str(d), "--json"])
    assert code == 2 and "单引号" in out["issues"][0]["message"], sim.debug(code, out, err)


# --------------------------------------------------------------------------- 发布、查重、记录

MIXED = """kind: video
title: "第12期：三分钟讲清楚 Git 分支 🔥"
body: |
  用三分钟讲清楚 Git 分支。
tags: [编程, Git]
video: '../../素材 库/视频 成片.mp4'
account: main
platforms:
  fake: {}
  fake2: {fake: fail}
  fake3: {fake: needs_action}
  fake4: {fake: unknown}
"""


def test_mixed_outcomes_dedupe_force_and_history(tmp_path: Path):
    sim = Sim(tmp_path)
    write_media(sim.root / "素材 库")
    d = _post(sim, "混合", MIXED)
    t0 = time.monotonic()
    code, out, err = sim.run(["publish", str(d), "--yes", "--json"])
    assert time.monotonic() - t0 < 30
    assert code == 1 and out["ok"] is False and out["run_id"], sim.debug(code, out, err)
    rows = {r["platform"]: r for r in out["results"]}
    assert all(set(r) >= RESULT_KEYS for r in rows.values())
    assert (rows["fake"]["status"], rows["fake"]["url"], rows["fake"]["item_id"]) == ("published", "https://example.com/p/1", "1")
    assert (rows["fake2"]["status"], rows["fake2"]["error_code"], rows["fake2"]["retryable"]) == ("failed", "platform_error", True)
    assert (rows["fake3"]["status"], rows["fake3"]["error_code"]) == ("needs_action", "login_required")
    assert (rows["fake4"]["status"], rows["fake4"]["error_code"], rows["fake4"]["submit_started"],
            rows["fake4"]["retryable"]) == ("failed", "unknown_outcome", True, False)
    assert [e["platform"] for e in events(err, "submit_started")] == ["fake", "fake4"]

    # 历史：每个平台一条最终记录，预记录被最终结果覆盖
    code, hist, _ = sim.run(["history", "--json"])
    assert [(h["platform"], h["status"]) for h in hist["history"]] == [
        ("fake", "published"), ("fake2", "failed"), ("fake3", "needs_action"), ("fake4", "failed")]
    assert all(h["source"].endswith("post.yaml") and h["title"].startswith("第12期") for h in hist["history"])

    # dry-run 也能预先看到查重结果
    code, out, err = sim.run(["publish", str(d), "--dry-run", "--json"])
    st = {r["platform"]: r["status"] for r in out["results"]}
    assert st == {"fake": "skipped", "fake2": "dry_run", "fake3": "dry_run", "fake4": "skipped"}, sim.debug(code, out, err)

    # 原样重跑：已发出 / 结果未知的跳过，其余重试
    code, out, err = sim.run(["publish", str(d), "--yes", "--json"])
    st = {r["platform"]: (r["status"], r["error_code"]) for r in out["results"]}
    assert st["fake"] == ("skipped", "validation") and st["fake4"] == ("skipped", "validation"), sim.debug(code, out, err)
    assert "--to fake4 --force" in {r["platform"]: r for r in out["results"]}["fake4"]["message"]
    assert st["fake2"][0] == "failed" and st["fake3"][0] == "needs_action" and code == 1

    # --force 只对 --to 的平台
    code, out, err = sim.run(["publish", str(d), "--yes", "--json", "--to", "fake4", "--force"])
    assert [r["platform"] for r in out["results"]] == ["fake4"], sim.debug(code, out, err)
    assert len(sim.published("fake4")) == 2 and len(sim.published("fake")) == 0  # fake 是产品自带的假平台，不记日志

    # 改了标题就是新内容
    (d / "post.yaml").write_text(MIXED.replace("第12期", "第13期").replace("fake2: {fake: fail}", "fake2: {}")
                                 .replace("fake3: {fake: needs_action}", "fake3: {}"), encoding="utf-8")
    code, out, err = sim.run(["publish", str(d), "--yes", "--json", "--to", "fake2,fake3"])
    assert code == 0 and [r["status"] for r in out["results"]] == ["published", "published"], sim.debug(code, out, err)


def test_worker_crash_after_click_blocks_blind_retry(tmp_path: Path):
    """点了发布之后 worker 崩溃：结果未知，查重拦住原样重发，提示先去后台确认。"""
    sim = Sim(tmp_path)
    d = _post(sim, "拒绝", "kind: video\ntitle: 拒绝\nvideo: '视频 成片.mp4'\nplatforms: {fake2: {fake: crash_after_submit}}\n")
    code, out, err = sim.run(["publish", str(d), "--yes", "--json"])
    r = out["results"][0]
    assert (r["status"], r["error_code"], r["submit_started"]) == ("failed", "unknown_outcome", True), sim.debug(code, out, err)
    assert "退出码 9" in r["message"]
    code, out, err = sim.run(["publish", str(d), "--yes", "--json"])
    assert out["results"][0]["status"] == "skipped" and len(sim.published()) == 1
    assert "后台确认" in out["results"][0]["message"] and "--to fake2 --force" in out["results"][0]["message"]


def test_concurrent_runs_of_the_same_post_publish_once(tmp_path: Path):
    """两个终端（或 Claude 后台任务 + 计划任务）同时发同一条：每个平台只能发一次。"""
    sim = Sim(tmp_path)
    d = _post(sim, "并发", """kind: video
title: 并发
video: '视频 成片.mp4'
platforms:
  fake2: {fake: slow, fake_secs: 3}
  fake3: {fake: slow, fake_secs: 3}
""")
    a = sim.start(["publish", str(d), "--yes", "--json"])
    time.sleep(1.0)
    b = sim.start(["publish", str(d), "--yes", "--json"])
    outs = [json.loads(p.communicate(timeout=120)[0]) for p in (a, b)]
    assert len(sim.published("fake2")) == 1 and len(sim.published("fake3")) == 1, outs
    statuses = sorted(r["status"] for o in outs for r in o["results"])
    assert statuses.count("published") == 2 and all(s in ("published", "skipped", "failed") for s in statuses)


def test_sms_code_while_publish_runs_in_background(tmp_path: Path):
    """skill 的做法：发布在后台跑，看到 needs_input 后向用户要验证码，用 mpub verify-code 提交。"""
    sim = Sim(tmp_path)
    d = _post(sim, "短信", f"""kind: video
title: 要验证码
video: '视频 成片.mp4'
schedule: "{_later()}"
platforms:
  fake2: {{fake: sms, fake_secs: 30}}
  fake3: {{fake: scheduled}}
""")
    proc = sim.start(["publish", str(d), "--yes", "--json"])
    seen = read_stderr_until(proc, '"needs_input"', 30)
    ev = events(seen, "needs_input")
    assert ev and ev[0]["kind"] == "sms_code" and ev[0]["platform"] == "fake2" and ev[0]["path"], seen
    code, out, err = sim.run(["verify-code", "douyin", " 654321 ", "--json"])
    assert code == 0 and Path(out["path"]) == Path(ev[0]["path"])
    assert proc.wait(timeout=60) == 0
    out = json.loads(proc.stdout.read())
    assert [(r["platform"], r["status"]) for r in out["results"]] == [("fake2", "published"), ("fake3", "scheduled")]
    assert sim.published("fake2")[0]["code"] == "654321"
    assert sim.published("fake3")[0]["schedule"] == out["results"][1]["message"].split("：")[1]
    # 定时的也算发过：重跑跳过
    code, out, err = sim.run(["publish", str(d), "--yes", "--json", "--to", "fake3"])
    assert out["results"][0]["status"] == "skipped"
    # 没人给验证码：超时返回 sms_code_required（退出码 3），没有点发布
    (d / "post.yaml").write_text("kind: video\ntitle: 没人理\nvideo: '视频 成片.mp4'\n"
                                 "platforms: {fake2: {fake: sms, fake_secs: 2}}\n", encoding="utf-8")
    code, out, err = sim.run(["publish", str(d), "--yes", "--json"])
    r = out["results"][0]
    assert code == 3 and (r["status"], r["error_code"], r["submit_started"]) == ("needs_action", "sms_code_required", False)


def test_timeouts_before_and_after_submit(tmp_path: Path):
    sim = Sim(tmp_path)
    d = _post(sim, "超时", """kind: video
title: 超时
video: '视频 成片.mp4'
platforms:
  fake2: {fake: slow, fake_secs: 30}
  fake3: {fake: hang_after_submit}
""")
    t0 = time.monotonic()
    code, out, err = sim.run(["publish", str(d), "--yes", "--json", "--timeout", "6"])
    assert time.monotonic() - t0 < 40, "超时没有生效"
    rows = {r["platform"]: r for r in out["results"]}
    assert (rows["fake2"]["error_code"], rows["fake2"]["retryable"], rows["fake2"]["submit_started"]) == ("timeout", True, False)
    assert (rows["fake3"]["error_code"], rows["fake3"]["submit_started"]) == ("unknown_outcome", True)
    assert "超过 6 秒" in rows["fake3"]["message"] and code == 1
    assert not sim.pids(), "worker 没有被清理"


@posix_only
def test_sigterm_mid_run_reports_json_and_records(tmp_path: Path):
    sim = Sim(tmp_path)
    d = _post(sim, "中断", """kind: video
title: 中断
video: '视频 成片.mp4'
platforms:
  fake2: {}
  fake3: {fake: slow, fake_secs: 30}
  fake4: {}
""")
    proc = sim.start(["publish", str(d), "--yes", "--json"])
    seen = read_stderr_until(proc, "上传中", 30)
    assert "上传中" in seen
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 130
    out = json.loads(proc.stdout.read())
    assert out["ok"] is False and out["error"] == "interrupted"
    st = [(r["platform"], r["status"], r["error_code"]) for r in out["results"]]
    assert st == [("fake2", "published", None), ("fake3", "failed", "internal"), ("fake4", "failed", "internal")]
    assert [(h["platform"], h["status"]) for h in sim.history()] == [("fake2", "published"), ("fake3", "failed")]
    assert wait_for(lambda: not sim.pids(), 10), sim.pids()
    # 重跑：fake2 已发过跳过，fake3 / fake4 照常发
    code, out, err = sim.run(["publish", str(d), "--yes", "--json", "--to", "fake2,fake4"])
    assert [(r["platform"], r["status"]) for r in out["results"]] == [("fake2", "skipped"), ("fake4", "published")]


# --------------------------------------------------------------------------- 真浏览器

WEB = "kind: video\ntitle: {title}\nvideo: '视频 成片.mp4'\naccount: main\nplatforms: [simweb]\n"


@requires_chromium
def test_browser_platform_daily_scenarios(tmp_path: Path):
    """成功 / 上传慢（按钮上传完才可点）/ 平台拒绝 / 登录失效，各自独立并发跑。"""
    cases = {
        "ok": ({"upload_secs": 1}, "valid"),
        "slow": ({"upload_secs": 6}, "valid"),
        "reject": ({"upload_secs": 1, "create": "reject"}, "valid"),
        "expired": ({"upload_secs": 1}, "expired"),
    }

    def run(name: str):
        scenario, session = cases[name]
        sim = Sim(tmp_path / name)
        write_storage_state(sim.home, "simweb", "main", session)
        d = _post(sim, name, WEB.format(title=f"浏览器 {name}"))
        t0 = time.monotonic()
        code, out, err = sim.run(["publish", str(d), "--yes", "--json"], scenario=scenario)
        return name, sim, code, out, err, time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=4) as ex:
        runs = {r[0]: r[1:] for r in ex.map(run, cases)}

    sim, code, out, err, secs = runs["ok"]
    r = out["results"][0]
    assert code == 0 and (r["status"], r["item_id"]) == ("published", "9001") and "manage?id=9001" in r["url"], sim.debug(code, out, err)
    assert len(sim.published("simweb")) == 1 and secs < 45

    sim, code, out, err, secs = runs["slow"]
    assert code == 0 and out["results"][0]["status"] == "published", sim.debug(code, out, err)
    assert secs >= 6

    sim, code, out, err, secs = runs["reject"]
    r = out["results"][0]
    assert code == 1 and (r["status"], r["error_code"], r["submit_started"]) == ("failed", "platform_error", True), sim.debug(code, out, err)
    assert "内容违规" in r["message"] and any(e.endswith("simweb-rejected.png") for e in r["evidence"])
    assert len(sim.published("simweb")) == 1  # 只点了一次
    # 平台明确拒绝 = 没发出：不被查重拦住，可以改了再发（这里原样再发，平台再拒一次）
    code, out, err = sim.run(["publish", str(sim.root / "我的 帖子" / "reject"), "--yes", "--json"],
                             scenario=cases["reject"][0])
    assert out["results"][0]["status"] == "failed" and len(sim.published("simweb")) == 2

    sim, code, out, err, secs = runs["expired"]
    r = out["results"][0]
    assert code == 3 and (r["status"], r["error_code"], r["submit_started"]) == ("needs_action", "login_required", False), sim.debug(code, out, err)
    assert not sim.published() and secs < 30
    for s in (runs[n][0] for n in cases):
        assert wait_for(lambda: not s.pids(), 10), f"残留进程 {s.pids()}"


@requires_chromium
@posix_only
def test_killed_after_submit_leaves_a_record_and_no_processes(tmp_path: Path):
    """mpub 在点了发布之后被强行结束（任务管理器 / 关窗口 / kill -9）：浏览器要关掉，下次不能原样重发。"""
    sim = Sim(tmp_path)
    write_storage_state(sim.home, "simweb", "main")
    d = _post(sim, "kill9", WEB.format(title="被强行结束"))
    scenario = {"upload_secs": 1, "create": "hang", "result_wait": 120}
    proc = sim.start(["publish", str(d), "--yes", "--json"], scenario=scenario)
    seen = read_stderr_until(proc, "submit_started", 60)
    assert "submit_started" in seen, seen
    assert wait_for(lambda: len(sim.published()) == 1, 15)
    proc.kill()
    proc.wait(timeout=10)
    assert wait_for(lambda: not sim.pids(), 30), f"残留进程 {sim.pids()}"
    # 主进程没机会写最终结果：只剩点发布时记下的「结果待定」
    assert not sim.history()
    pend = sim.pending()
    assert len(pend) == 1 and pend[0]["error_code"] == "unknown_outcome" and pend[0]["submit_started"] is True
    code, out, err = sim.run(["publish", str(d), "--yes", "--json"], scenario={"upload_secs": 1})
    r = out["results"][0]
    assert r["status"] == "skipped" and "结果未知" in r["message"], sim.debug(code, out, err)
    assert len(sim.published()) == 1
    # 账号空闲后被转成正式记录
    assert not sim.pending()
    hist = sim.history()
    assert len(hist) == 1 and hist[0]["error_code"] == "unknown_outcome" and "被结束" in hist[0]["message"]
    code, out, err = sim.run(["history", "--json"])
    assert out["history"][-1]["error_code"] == "unknown_outcome"


@requires_chromium
@posix_only
def test_sigterm_during_browser_upload_cleans_up(tmp_path: Path):
    sim = Sim(tmp_path)
    write_storage_state(sim.home, "simweb", "main")
    d = _post(sim, "term", WEB.format(title="上传中被中断"))
    proc = sim.start(["publish", str(d), "--yes", "--json"], scenario={"upload_secs": 60})
    assert "上传中" in read_stderr_until(proc, "上传中", 60)
    time.sleep(1)
    assert len(sim.pids()) > 3  # CLI、worker、driver、浏览器
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 130
    out = json.loads(proc.stdout.read())
    r = out["results"][0]
    assert (r["status"], r["error_code"], r["submit_started"], r["retryable"]) == ("failed", "internal", False, True)
    assert wait_for(lambda: not sim.pids(), 10), f"残留进程 {sim.pids()}"
    assert not sim.published()


@requires_chromium
def test_login_check_and_accounts_with_several_accounts(tmp_path: Path):
    sim = Sim(tmp_path)
    code, out, err = sim.run(["login", "simweb", "--account", "main", "--headless", "--json"],
                             scenario={"login_after": 2})
    assert code == 0 and out["ok"] and out["result"]["status"] == "ok", sim.debug(code, out, err)
    qr = events(err, "qrcode")
    assert qr and Path(qr[0]["path"]).is_file() and "note" in qr[0]
    write_storage_state(sim.home, "simweb", "old", "expired")
    for acc in ("acc1", "acc2"):
        assert sim.run(["login", "fake", "--account", acc, "--json"])[0] == 0
    code, out, err = sim.run(["check", "fake", "simweb", "--json"])
    got = [(r["platform"], r["account"], r["status"], r["error_code"]) for r in out["results"]]
    assert got == [("fake", "acc1", "ok", None), ("fake", "acc2", "ok", None), ("simweb", "main", "ok", None),
                   ("simweb", "old", "needs_action", "login_required")], sim.debug(code, out, err)
    assert code == 3 and out["ok"] is False
    # 每个账号的日志各自一份，不会互相覆盖
    logs = {p.parent.name for p in (sim.home / "runs").rglob("*-check.log")}
    assert {"fake-acc1", "fake-acc2", "simweb-main", "simweb-old"} <= logs

    # accounts 只列真实平台；check 指定账号
    for platform, acc in (("douyin", "main"), ("douyin", "alt"), ("zhihu", "main")):
        write_storage_state(sim.home, platform, acc)
    code, out, err = sim.run(["accounts", "--json"])
    assert [(a["platform"], a["account"]) for a in out["accounts"]] == [
        ("douyin", "alt"), ("douyin", "main"), ("zhihu", "main")]
    assert all({"file", "updated_at"} <= set(a) for a in out["accounts"])
    code, out, err = sim.run(["check", "simweb", "--account", "old", "--json"])
    assert code == 3 and len(out["results"]) == 1


# --------------------------------------------------------------------------- skill 的每一步

def test_skill_walkthrough_commands_and_fields(tmp_path: Path):
    """按 .claude/skills/multipub/SKILL.md 的流程执行每条命令，检查它引用的 JSON 字段都存在。"""
    sim = Sim(tmp_path)
    # 1. 环境
    code, out, err = sim.run(["doctor", "--json"])
    assert {"ok", "checks"} <= set(out) and all({"name", "ok", "detail"} <= set(c) for c in out["checks"])
    names = {c["name"] for c in out["checks"]}
    assert {"python", "data_home", "patchright", "browser", "bilibili.biliup", "zhihu.deps",
            "douyin.accounts"} <= names
    # 2. 准备清单
    code, out, err = sim.run(["new", "posts/ep12", "--kind", "video", "--json"])
    assert code == 0 and out["path"].endswith("post.yaml")
    d = Path(out["path"]).parent
    write_media(d)
    (d / "post.yaml").write_text(f"""kind: video
title: 第12期
body: 简介
tags: [科技]
video: '视频 成片.mp4'
schedule: "{_later(5)}"
account: main
platforms:
  bilibili: {{tid: 231}}
  douyin: {{}}
  xiaohongshu: {{}}
  zhihu: {{}}
  fake: {{}}
""", encoding="utf-8")
    code, out, err = sim.run(["validate", "posts/ep12", "--json"])
    assert code == 0 and out["ok"], sim.debug(code, out, err)
    for p in out["posts"]:
        assert {"platform", "account", "kind", "title", "schedule", "ai_generated", "options", "tags", "video",
                "images", "cover"} <= set(p)
    assert [(s["platform"], s["error_code"]) for s in out["skipped"]] == [("zhihu", "unsupported")]
    assert all({"level", "field", "message", "platform"} <= set(i) for i in out["issues"])
    # 3. 检查登录
    code, out, err = sim.run(["check", "douyin", "xiaohongshu", "--account", "main", "--json"])
    assert {"ok", "results"} <= set(out)
    # 4. dry-run
    code, out, err = sim.run(["publish", "posts/ep12", "--dry-run", "--json"])
    assert code == 0 and {"ok", "run_id", "results", "issues"} <= set(out)
    # 5. 发布（只发假平台；真实平台只做校验）
    code, out, err = sim.run(["publish", "posts/ep12", "--to", "fake", "--yes", "--json"])
    assert code == 0 and out["results"][0]["status"] == "published", sim.debug(code, out, err)
    assert {"event", "platform", "account"} <= set(events(err, "submit_started")[0])
    log = events(err, "log")[0]
    assert {"level", "message"} <= set(log)
    # 6. 汇报 / 记录
    code, out, err = sim.run(["history", "--limit", "20", "--json"])
    assert out["history"][-1]["status"] == "published" and {"run_id", "fingerprint", "source", "title"} <= set(out["history"][-1])
    code, out, err = sim.run(["accounts", "--json"])
    assert out == {"accounts": []}
    code, out, err = sim.run(["login", "fake", "--account", "main", "--json"])
    assert code == 0 and {"ok", "result"} <= set(out) and out["result"]["status"] == "ok"
    code, out, err = sim.run(["verify-code", "douyin", "123456", "--json"])
    assert out["ok"] and Path(out["path"]).read_text(encoding="utf-8") == "123456"
    assert Path(out["path"]).parent == sim.home / "sau"
    code, out, err = sim.run(["publish", "posts/ep12", "--json"])  # 非交互没加 --yes
    assert code == 2 and out["error"] == "confirmation_required"
    # 「全局安装」：在别的目录用绝对路径
    code, out, err = sim.run(["validate", str(d), "--json"], cwd=tmp_path)
    assert code == 0
