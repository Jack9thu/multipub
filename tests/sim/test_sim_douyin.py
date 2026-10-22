"""抖音端到端模拟：真实 CLI（mpub publish / login / check / verify-code）+ 真 Chromium + 本地仿真页面。

仿真页面与接口见 tests/sim/douyin/（mock.py、app.html、login.html）；真实网络一律被拦截。
每个测试里互不相关的场景并发跑（各自独立的数据目录和浏览器），整体耗时约为最慢场景的耗时。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from ._harness import REPO, make_home, requires_chromium, run_cli, run_log
from .douyin.mock import DEFAULTS, VALID_SESSION, qr_png, read_log

pytestmark = requires_chromium

SIM_DIR = Path(__file__).resolve().parent / "douyin"
ITEM = DEFAULTS["item_id"]
TOML = """
[timeouts]
publish_video = 300
publish_note = 300
login = 150
check = 90

[platforms.douyin]
page_timeout = 30
upload_timeout = 60
publish_timeout = 30
sms_timeout = 60
"""


# ---------------------------------------------------------------- 工具

class Background:
    """后台运行的 CLI：逐行收集 stderr（--json 时 worker 的进度事件是 stderr 上的 JSON 行）。"""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lines: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self.eof = False
        self._cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            with self._cond:
                self.lines.append(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = None
                if isinstance(ev, dict) and "event" in ev:
                    ev["_t"] = time.monotonic() - self.started
                    self.events.append(ev)
                self._cond.notify_all()
        with self._cond:
            self.eof = True
            self._cond.notify_all()

    def _wait(self, found: Callable[[], Any], what: str, timeout: float) -> Any:
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                hit = found()
                if hit is not None:
                    return hit
                if self.eof or time.monotonic() >= end:
                    raise AssertionError(f"等不到{what}；stderr:\n{''.join(self.lines)[-3000:]}")
                self._cond.wait(0.5)

    def wait_event(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 120, nth: int = 1) -> dict[str, Any]:
        def found() -> dict[str, Any] | None:
            hits = [e for e in self.events if pred(e)]
            return hits[nth - 1] if len(hits) >= nth else None
        return self._wait(found, f"第 {nth} 个事件", timeout)

    def wait_line(self, text: str, timeout: float = 60) -> str:
        return self._wait(lambda: next((x for x in self.lines if text in x), None), f"「{text}」", timeout)

    def finish(self, timeout: float = 300) -> tuple[int, dict[str, Any] | None]:
        assert self.proc.stdout is not None
        out = self.proc.stdout.read()
        code = self.proc.wait(timeout=timeout)
        try:
            return code, json.loads(out) if out.strip() else None
        except ValueError:
            return code, None


class Sim:
    """一个独立的模拟环境：数据目录、账号文件、场景文件、页面记录。"""

    def __init__(self, tmp_path: Path, name: str, scenario: dict[str, Any] | None = None, *,
                 account: str | None = "valid", toml: str = TOML):
        self.root = tmp_path / name
        self.root.mkdir(parents=True)
        self.home = make_home(self.root, toml)
        self.scenario_file = self.root / "scenario.json"
        self.log_file = self.root / "sim.jsonl"
        self.set(**(scenario or {}))
        if account:
            self.write_account(VALID_SESSION if account == "valid" else "expired-session")

    # 场景 / 账号 / 清单
    def set(self, **scenario: Any) -> None:
        self.scenario_file.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")

    @property
    def account_file(self) -> Path:
        return self.home / "accounts" / "douyin" / "main.json"

    def write_account(self, session: str) -> None:
        self.account_file.parent.mkdir(parents=True, exist_ok=True)
        cookie = {"name": "sessionid", "value": session, "domain": ".douyin.com", "path": "/", "expires": -1,
                  "httpOnly": True, "secure": True, "sameSite": "Lax"}
        self.account_file.write_text(json.dumps({"cookies": [cookie], "origins": []}), encoding="utf-8")

    def _manifest(self, d: Path, data: dict[str, Any]) -> Path:
        data = {"account": "main", "platforms": {"douyin": {}}, **data}
        (d / "post.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return d

    def video_post(self, dirname: str = "post", cover: tuple[int, int] | None = (900, 1200),
                   cover_name: str = "cover.jpg", **fields: Any) -> Path:
        from PIL import Image

        d = self.root / dirname
        d.mkdir(parents=True)
        (d / "视频 文件.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(4096))
        data: dict[str, Any] = {"kind": "video", "title": "模拟发布测试", "body": "这是一段简介", "tags": ["科技"],
                                "video": "视频 文件.mp4"}
        if cover:
            Image.new("RGB", cover, (10, 200, 30)).save(d / cover_name)
            data["cover"] = cover_name
        data.update(fields)
        return self._manifest(d, data)

    def note_post(self, dirname: str = "note", n_images: int = 3, **fields: Any) -> Path:
        from PIL import Image

        d = self.root / dirname
        (d / "图 片").mkdir(parents=True)
        images = []
        for i in range(1, n_images + 1):
            Image.new("RGB", (1080, 1440), (i * 9 % 255, 120, 200)).save(d / "图 片" / f"第 {i} 张.png")
            images.append(f"图 片/第 {i} 张.png")
        data: dict[str, Any] = {"kind": "note", "title": "周末徒步路线分享", "body": "正文", "tags": ["徒步"],
                                "images": images}
        data.update(fields)
        return self._manifest(d, data)

    # 运行
    @property
    def env(self) -> dict[str, str]:
        return {"MULTIPUB_SIM": "douyin", "DY_SIM_FILE": str(self.scenario_file), "DY_SIM_LOG": str(self.log_file)}

    def run(self, *args: str, timeout: int = 300) -> tuple[int, dict[str, Any] | None, str, float]:
        t0 = time.monotonic()
        code, out, err = run_cli(list(args), home=self.home, sim_dir=SIM_DIR, timeout=timeout, env=self.env)
        return code, out, err, time.monotonic() - t0

    def start(self, *args: str) -> Background:
        paths = [str(SIM_DIR), str(REPO)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
        env = dict(os.environ, MULTIPUB_HOME=str(self.home), **self.env, PYTHONPATH=os.pathsep.join(paths))
        proc = subprocess.Popen([sys.executable, "-m", "multipub.cli", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                env=env, cwd=str(REPO))
        return Background(proc)

    def verify_code(self, code: str) -> None:
        rc, out, _, _ = self.run("verify-code", "douyin", code, "--json", timeout=60)
        assert rc == 0 and out and out["ok"], out

    # 观察
    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [e for e in read_log(self.log_file) if kind is None or e.get("type") == kind]

    def history(self) -> list[dict[str, Any]]:
        f = self.home / "history.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.is_file() else []

    def debug(self, err: str = "") -> str:
        sim = "\n".join(json.dumps(e, ensure_ascii=False)[:300] for e in self.events())
        return f"\n--- stderr\n{err[-3000:]}\n--- sim log\n{sim[-4000:]}\n{run_log(self.home)}"


def only_result(out: dict[str, Any] | None) -> dict[str, Any]:
    assert out is not None, "没有 JSON 输出"
    if "result" in out:
        return out["result"]
    assert len(out["results"]) == 1, out
    return out["results"][0]


def parallel(*fns: Callable[[], None]) -> None:
    """并发跑互不相关的场景；全部跑完后汇总失败。"""
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futures = {pool.submit(fn): getattr(fn, "__name__", repr(fn)) for fn in fns}
        for fut, name in futures.items():
            try:
                fut.result()
            except BaseException as exc:  # noqa: BLE001 - 汇总后统一报告
                errors.append(f"[{name}] {type(exc).__name__}: {exc}")
    assert not errors, "\n\n".join(errors)


def schedule_in(hours: float) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 视频：日常发布

@requires_chromium
def test_video_daily_use(tmp_path: Path):
    def happy_then_dedupe_then_force() -> None:
        sim = Sim(tmp_path, "happy")
        post = sim.video_post(ai_generated=True, body="第一行简介\n第二行", tags=["科技", "教程"], cover_name="封面 图.jpg",
                              platforms={"douyin": {"collection": "旅行日记"}})
        before = sim.account_file.read_text(encoding="utf-8")
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug(err)
        assert r["item_id"] == ITEM and r["url"] == f"https://www.douyin.com/video/{ITEM}" and r["submit_started"]
        assert secs < 120
        creates, clicks = sim.events("create"), sim.events("publish_click")
        assert len(creates) == 1 and len(clicks) == 1, sim.debug(err)
        item = creates[0]["payload"]["item"]
        assert item["common"]["item_title"] == "模拟发布测试" and item["common"]["timing"] == 0
        assert "第一行简介\n第二行" in item["common"]["text"] and item["tags"] == ["科技", "教程"]
        assert item["declare"] == "内容由AI生成" and item["mix"] == "旅行日记"
        assert item["cover"] == {"source": "upload", "name": "封面 图.jpg", "orientation": "portrait"}
        assert r["message"] == "已提交，审核通过后公开"  # 没有任何与清单不一致的提示
        assert item["sync_toutiao"] is False and item["video"]["name"] == "视频 文件.mp4"
        hist = sim.history()
        assert [(h["status"], h["item_id"]) for h in hist] == [("submitted", ITEM)]
        saved = json.loads(sim.account_file.read_text(encoding="utf-8"))["cookies"]
        assert "sim_refresh" not in before and any(c["name"] == "sim_refresh" for c in saved)  # 成功后回写了 cookie

        # 同一内容再发：查重跳过，不开浏览器
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "skipped" and "--force" in r["message"] and secs < 20
        assert len(sim.events("create")) == 1

        # --force：再发一次（平台返回新的作品 ID）
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json", "--force")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted" and r["item_id"] == str(int(ITEM) + 1), sim.debug(err)
        assert len(sim.events("create")) == 2  # 查重跳过不记入发布记录
        assert [(h["status"], h["item_id"]) for h in sim.history()] == [("submitted", ITEM), ("submitted", str(int(ITEM) + 1))]

    def slow_v1_upload_landscape_cover_sync_off() -> None:
        # 旧版发布页：表单先出来、视频还在传（12 秒），「发布」按钮上传完才可用；横封面；同步开关默认开着要关掉；
        # 表单上还弹了新手引导（遮罩挡住简介编辑框）
        sim = Sim(tmp_path, "slow", {"page_version": "v1", "upload_ms": 12000, "sync_default": True, "guide": True})
        post = sim.video_post(cover=(1280, 720), platforms={"douyin": {
            "sync_toutiao": False, "declaration": "内容为个人观点或见解"}})
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug(err)
        done, clicks = sim.events("upload_done"), sim.events("publish_click")
        assert len(clicks) == 1 and clicks[0]["t"] > done[0]["t"]
        item = sim.events("create")[0]["payload"]["item"]
        assert item["cover"]["orientation"] == "landscape" and item["sync_toutiao"] is False
        assert item["declare"] == "内容为个人观点或见解" and sim.events("guide_shown")
        assert item["common"]["text"].startswith("这是一段简介")

    def success_receipt_without_redirect() -> None:
        # 创建接口明确成功、但页面停在原页（改版 / 只弹「发布成功」）：按已提交处理，不报 unknown_outcome
        sim = Sim(tmp_path, "noredirect", {"redirect": "none"})
        post = sim.video_post(cover=None)
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted" and r["item_id"] == ITEM, sim.debug(err)
        assert len(sim.events("publish_click")) == 1 and secs < 90

    def cover_required_and_upload_retry() -> None:
        # 第一次上传失败要重传；没设封面时平台提示「请设置封面后再发布」，选推荐封面后再点一次
        sim = Sim(tmp_path, "cover", {"require_cover": True, "upload_fail_times": 1})
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug(err)
        assert sim.events("upload_failed") and sim.events("upload_retry")
        assert [e["source"] for e in sim.events("cover_set")] == ["recommend"]
        assert len(sim.events("publish_click")) == 2 and len(sim.events("create")) == 1

    def spa_route_change_right_after_success() -> None:
        # 成功后立刻在 SPA 内切到作品管理页（不整页跳转）：照样拿到作品 ID
        sim = Sim(tmp_path, "push", {"redirect": "push", "redirect_delay_ms": 0, "create_delay_ms": 0})
        post = sim.video_post(cover=None)
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted" and r["item_id"] == ITEM, sim.debug(err)
        assert len(sim.events("publish_click")) == 1 and secs < 60

    parallel(happy_then_dedupe_then_force, slow_v1_upload_landscape_cover_sync_off,
             success_receipt_without_redirect, cover_required_and_upload_retry, spa_route_change_right_after_success)


# ---------------------------------------------------------------- 视频：失败要报对

@requires_chromium
def test_video_failures_are_reported_correctly(tmp_path: Path):
    def login_expired() -> None:
        sim = Sim(tmp_path, "expired", account="expired")
        post = sim.video_post()
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 3 and r["status"] == "needs_action" and r["error_code"] == "login_required", sim.debug(err)
        assert not r["submit_started"] and secs < 45
        assert not sim.events("video_selected") and not sim.events("publish_click")

    def platform_rejects() -> None:
        sim = Sim(tmp_path, "reject", {"create": "reject"})
        post = sim.video_post(cover=None)
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "platform_error", sim.debug(err)
        assert DEFAULTS["reject_msg"] in r["message"] and r["submit_started"] and not r["retryable"]
        assert len(sim.events("publish_click")) == 1 and len(sim.events("create")) == 1  # 不会反复点
        assert secs < 90

    def create_request_hangs() -> None:
        sim = Sim(tmp_path, "hang", {"create": "hang"})
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "unknown_outcome" and r["submit_started"], sim.debug(err)
        assert len(sim.events("publish_click")) == 1 and len(sim.events("create")) == 1  # 请求没回来时不补点
        # 结果未知的内容不会被直接重发
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "skipped" and "结果未知" in r["message"]
        assert len(sim.events("create")) == 1

    def captcha_after_click() -> None:
        sim = Sim(tmp_path, "captcha", {"captcha": "on_click"})
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "unknown_outcome" and "人机验证" in r["message"], sim.debug(err)
        assert not sim.events("create") and len(sim.events("publish_click")) <= 3
        assert any(p.endswith(".png") and Path(p).is_file() for p in r["evidence"])

    def gateway_error_after_click() -> None:
        # 创建接口返回 502（请求可能已被处理）：结果未知，不补点，消息里带上 HTTP 状态
        sim = Sim(tmp_path, "h502", {"create": "http500"})
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "unknown_outcome" and "HTTP 502" in r["message"], sim.debug(err)
        assert len(sim.events("publish_click")) == 1 and len(sim.events("create")) == 1

    def session_expires_after_cookie_check() -> None:
        # cookie 校验刚通过，打开上传页时却被踢回登录页：很快返回 login_required（以前干等 60 秒再报 internal）
        sim = Sim(tmp_path, "midlogout", {"logout_after_pages": 1})
        post = sim.video_post(cover=None)
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 3 and r["error_code"] == "login_required" and not r["submit_started"], sim.debug(err)
        assert secs < 30 and not sim.events("video_selected")

    parallel(login_expired, platform_rejects, create_request_hangs, captcha_after_click,
             gateway_error_after_click, session_expires_after_cookie_check)


# ---------------------------------------------------------------- 挡住页面的浮层 / 与清单不一致的内容

@requires_chromium
def test_blocking_overlays_and_content_checks(tmp_path: Path):
    def cover_modal_stuck_and_missing_collection() -> None:
        # 自定义封面一直「检测中」，封面弹窗关不掉（会盖住「发布」）：取消封面、照常发布，结果消息里说明；
        # 合集名不存在同样写进消息
        sim = Sim(tmp_path, "coverstuck", {"cover_stuck": True})
        post = sim.video_post(platforms={"douyin": {"collection": "不存在的合集"}})
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted" and r["item_id"] == ITEM, sim.debug(err)
        assert "推荐封面" in r["message"] and "不存在的合集" in r["message"]
        assert len(sim.events("publish_click")) == 1 and not sim.events("cover_set") and secs < 90

    def publish_button_masked() -> None:
        # 「发布」上盖着关不掉的浮层：不能点（force 点击会落在浮层上），按「发布前超时」报错，不是 unknown_outcome
        sim = Sim(tmp_path, "pmask", {"publish_mask": True})
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "timeout" and not r["submit_started"], sim.debug(err)
        assert "挡住" in r["message"] and r["retryable"]
        assert not sim.events("publish_click") and not sim.events("mask_clicked") and not sim.events("create")
        assert [(h["status"], h["error_code"]) for h in sim.history()] == [("failed", "timeout")]  # 重发不会被查重拦住

    def notice_modal_dismissed() -> None:
        # 发布页弹出只有「我知道了」的平台通知（Esc 关不掉）：关掉后照常发布
        sim = Sim(tmp_path, "notice", {"notice_on_form": True})
        post = sim.note_post("notice", 2)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug(err)
        assert sim.events("notice_closed") and len(sim.events("publish_click")) == 1

    def emoji_title_truncated_by_page() -> None:
        # 30 个字符的标题含 emoji：网页按 UTF-16 计数截掉了最后的 emoji → 点「发布」之前拒绝（不发出被改过的标题）
        sim = Sim(tmp_path, "emoji")
        post = sim.video_post(cover=None, title="一" * 29 + "😀")
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "validation" and not r["submit_started"], sim.debug(err)
        assert "UTF-16" in r["message"] and not sim.events("publish_click")

    def captcha_before_click() -> None:
        # 还没点发布页面就停在人机验证：needs_action / captcha（不是 internal，也不是 unknown_outcome）
        sim = Sim(tmp_path, "capform", {"captcha": "on_form"})
        post = sim.video_post(cover=None)
        code, out, err, secs = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 3 and r["error_code"] == "captcha" and not r["submit_started"], sim.debug(err)
        assert not sim.events("publish_click") and secs < 60

    parallel(cover_modal_stuck_and_missing_collection, publish_button_masked, notice_modal_dismissed,
             emoji_title_truncated_by_page, captcha_before_click)


# ---------------------------------------------------------------- 图文 / 定时 / 浮层

@requires_chromium
def test_note_schedule_and_overlays(tmp_path: Path):
    def note_chinese_paths_bgm_schedule() -> None:
        sim = Sim(tmp_path, "note")
        when = schedule_in(3)
        title = "周末徒步路线分享一二三四五六七八九十"[:20]  # 正好 20 字（上限）
        post = sim.note_post("我的 帖子/第1期", 12, title=title, body="正文第一行\n第二行 带空格  和 emoji 😀",
                             schedule=when, platforms={"douyin": {"bgm": "晴天"}})
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "scheduled", sim.debug(err)
        assert r["url"] == f"https://www.douyin.com/note/{ITEM}"
        item = sim.events("create")[0]["payload"]["item"]
        assert [i["name"] for i in item["images"]] == [f"第 {i} 张.png" for i in range(1, 13)]
        assert item["common"]["item_title"] == title and "emoji 😀" in item["common"]["text"]
        assert item["bgm"] == "晴天" and item["schedule_text"] == when and item["common"]["media_type"] == 2

    def video_schedule_verified() -> None:
        sim = Sim(tmp_path, "sched")
        when = schedule_in(3)
        post = sim.video_post(cover=None, schedule=when)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "scheduled" and when in r["message"], sim.debug(err)
        item = sim.events("create")[0]["payload"]["item"]
        assert item["schedule_text"] == when
        assert item["common"]["timing"] == int(datetime.strptime(when, "%Y-%m-%d %H:%M").timestamp())

    def schedule_click_swallowed() -> None:
        sim = Sim(tmp_path, "swallow", {"schedule_swallow": True})
        post = sim.video_post(cover=None, schedule=schedule_in(3))
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "validation" and "定时" in r["message"], sim.debug(err)
        assert not r["submit_started"] and not sim.events("publish_click") and not sim.events("create")

    def note_guide_and_sticky_topic_dropdown() -> None:
        # 新手引导遮罩挡住简介编辑框；话题下拉按 Esc 收不起来、挡住音乐和「发布」
        sim = Sim(tmp_path, "overlay", {"guide": True, "mention_close": "sticky"})
        post = sim.note_post("overlay", 2, platforms={"douyin": {"bgm": "稻香"}})
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug(err)
        assert sim.events("guide_shown") and [e["value"] for e in sim.events("bgm_set")] == ["稻香"]
        assert len(sim.events("publish_click")) == 1

    def schedule_time_rejected_by_page() -> None:
        # 页面要求的提前量比本地校验更长（4 小时），输入的时间被改回默认值：发布前拒绝
        sim = Sim(tmp_path, "lead", {"schedule_min_lead_min": 240})
        post = sim.note_post("lead", 1, schedule=schedule_in(3))
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "validation" and "定时" in r["message"], sim.debug(err)
        assert sim.events("schedule_rejected") and not sim.events("publish_click")

    parallel(note_chinese_paths_bgm_schedule, video_schedule_verified, schedule_click_swallowed,
             note_guide_and_sticky_topic_dropdown, schedule_time_rejected_by_page)


# ---------------------------------------------------------------- 短信验证（mpub verify-code）

def _is_sms(ev: dict[str, Any]) -> bool:
    return ev.get("event") == "needs_input" and ev.get("kind") == "sms_code"


@requires_chromium
def test_sms_verification_with_verify_code(tmp_path: Path):
    def video_sms_after_click_wrong_then_right_code() -> None:
        sim = Sim(tmp_path, "sms", {"sms": "on_click"})
        post = sim.video_post(cover=None)
        bg = sim.start("publish", str(post), "--yes", "--json")
        first = bg.wait_event(_is_sms)
        assert first["path"] and Path(first["path"]).parent == sim.home / "sau"
        sim.verify_code("111111")  # 输错
        bg.wait_event(_is_sms, nth=2)  # 弹窗还在：再要一次
        sim.verify_code("123456")
        code, out = bg.finish()
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted" and r["item_id"] == ITEM, sim.debug("".join(bg.lines))
        assert [(e["code"], e["ok"]) for e in sim.events("sms_verify")] == [("111111", False), ("123456", True)]
        assert len(sim.events("create")) == 1 and len(sim.events("publish_click")) == 2

    def note_identity_verification_on_entry() -> None:
        # 一进上传页就弹身份验证（遮罩挡住「发布图文」）：先要验证码，再继续发布
        sim = Sim(tmp_path, "entry", {"sms": "on_enter"})
        post = sim.note_post("entry", 2)
        bg = sim.start("publish", str(post), "--yes", "--json")
        bg.wait_event(_is_sms)
        assert not any(e["event"] == "submit_started" for e in bg.events)  # 还没点发布
        sim.verify_code("123456")
        code, out = bg.finish()
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug("".join(bg.lines))
        assert sim.events("sms_verify")[0]["ok"] and len(sim.events("create")) == 1

    def sms_never_provided_after_click() -> None:
        # 点了发布才弹短信验证、一直没人给验证码：结果未知（不能当成没发出去而重发）
        toml = TOML.replace("sms_timeout = 60", "sms_timeout = 15")
        sim = Sim(tmp_path, "smstimeout", {"sms": "on_click"}, toml=toml)
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 1 and r["error_code"] == "unknown_outcome", sim.debug(err)
        assert "verify-code" in r["message"] and not sim.events("create")

    def api_requires_verification_then_page_resubmits() -> None:
        # 创建接口先返回「需要身份验证」(2190) 并弹出短信验证；验证通过后页面自己重新提交：不再补点
        sim = Sim(tmp_path, "vto", {"create": "verify_then_ok", "sms_resubmit": "auto"})
        post = sim.note_post("vto", 1)
        bg = sim.start("publish", str(post), "--yes", "--json")
        bg.wait_event(_is_sms)
        sim.verify_code("123456")
        code, out = bg.finish()
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug("".join(bg.lines))
        assert r["item_id"] == str(int(ITEM) + 1)  # 第二次创建请求的回执
        assert len(sim.events("publish_click")) == 1 and len(sim.events("create")) == 2

    def user_verifies_in_window() -> None:
        # 用户直接在浏览器窗口里完成了短信验证（--headed）：不再要验证码，补点一次发布
        sim = Sim(tmp_path, "inwindow", {"sms": "on_click", "sms_user_closes_ms": 4000})
        post = sim.video_post(cover=None)
        code, out, err, _ = sim.run("publish", str(post), "--yes", "--json")
        r = only_result(out)
        assert code == 0 and r["status"] == "submitted", sim.debug(err)
        assert not sim.events("sms_verify") and len(sim.events("publish_click")) == 2
        assert len(sim.events("create")) == 1

    parallel(video_sms_after_click_wrong_then_right_code, note_identity_verification_on_entry,
             sms_never_provided_after_click, api_requires_verification_then_page_resubmits, user_verifies_in_window)


# ---------------------------------------------------------------- 中断：不留孤儿进程、发布记录正确

def _children_map() -> dict[int, list[int]]:
    m: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                stat = Path(f"/proc/{d}/stat").read_text()
            except OSError:
                continue
            m.setdefault(int(stat[stat.rindex(")") + 2:].split()[1]), []).append(int(d))
    return m


def _descendants(pid: int) -> list[int]:
    m, out, todo = _children_map(), [], [pid]
    while todo:
        for c in m.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2:].split()[0] != "Z"


@requires_chromium
def test_sigterm_mid_publish_leaves_no_orphans(tmp_path: Path):
    if not Path("/proc/self/stat").exists():
        import pytest

        pytest.skip("需要 /proc")

    def interrupt(name: str, scenario: dict[str, Any], wait: Callable[[Background], Any], delay: float,
                  status: tuple[str, str]) -> None:
        sim = Sim(tmp_path, name, scenario)
        post = sim.video_post(cover=None)
        bg = sim.start("publish", str(post), "--yes", "--json")
        wait(bg)
        time.sleep(delay)
        tree = _descendants(bg.proc.pid)
        assert any("chrome" in Path(f"/proc/{p}/cmdline").read_bytes().decode(errors="replace") for p in tree
                   if Path(f"/proc/{p}/cmdline").exists()), "中断时浏览器应该在运行"
        bg.proc.send_signal(signal.SIGTERM)
        code, _ = bg.finish(timeout=60)
        assert code == 130
        end = time.monotonic() + 15
        while time.monotonic() < end and any(_alive(p) for p in tree):
            time.sleep(0.3)
        assert not [p for p in tree if _alive(p)], "worker / 浏览器进程没有被清理"
        hist = sim.history()
        assert [(h["status"], h["error_code"]) for h in hist] == [status]

    def before_submit() -> None:  # 视频还在上传（上传要 60 秒）
        interrupt("term-upload", {"upload_ms": 60000}, lambda bg: bg.wait_line("发布到"), 12,
                  ("failed", "internal"))

    def after_submit() -> None:  # 已点发布，创建请求还没回来
        interrupt("term-submit", {"create": "hang"},
                  lambda bg: bg.wait_event(lambda e: e.get("event") == "submit_started"), 2,
                  ("failed", "unknown_outcome"))

    parallel(before_submit, after_submit)


# ---------------------------------------------------------------- 登录 / 检查

@requires_chromium
def test_login_headless_qr_refresh_and_check(tmp_path: Path):
    def login_with_qr_refresh() -> None:
        # 二维码 5 秒过期、自动刷新；第 2 次刷新前后用户扫码。上报的图片就是页面上的二维码，扫码后很快结束
        sim = Sim(tmp_path, "login", {"login": {"scan_after_ms": 14000, "qr_expire_ms": 5000}}, account=None)
        bg = sim.start("login", "douyin", "--account", "main", "--headless", "--json")
        seen: list[int] = []
        first = bg.wait_event(lambda e: e.get("event") == "qrcode")
        data = Path(first["path"]).read_bytes()
        seen.append(next(n for n in range(1, 4) if data == qr_png(n)))
        code, out = bg.finish()
        total = time.monotonic() - bg.started
        r = only_result(out)
        assert code == 0 and r["status"] == "ok" and "扫码登录成功" in r["message"], sim.debug("".join(bg.lines))
        assert total < 60, f"扫码后登录拖了 {total:.0f} 秒"
        qr_events = [e for e in bg.events if e["event"] == "qrcode"]
        assert seen == [1] and len(qr_events) >= 2
        assert not any("截图" in (e.get("note") or "") for e in qr_events)  # 没有把登录后的页面当二维码上报
        assert qr_events[-1]["_t"] < 45  # 扫码后不再白等
        cookies = json.loads(sim.account_file.read_text(encoding="utf-8"))["cookies"]
        assert any(c["name"] == "sessionid" and c["value"] == VALID_SESSION for c in cookies)
        assert list((sim.home / "qrcode").iterdir()) == []  # 二维码图片已删除

    def check_valid_and_expired() -> None:
        sim = Sim(tmp_path, "check")
        code, out, err, _ = sim.run("check", "douyin", "--json")
        assert code == 0 and only_result(out)["status"] == "ok", sim.debug(err)
        sim.write_account("expired-session")
        code, out, err, secs = sim.run("check", "douyin", "--json")
        r = only_result(out)
        assert code == 3 and r["error_code"] == "login_required" and secs < 45

    def login_when_cookie_still_valid() -> None:
        sim = Sim(tmp_path, "relogin")
        code, out, err, _ = sim.run("login", "douyin", "--account", "main", "--headless", "--json")
        r = only_result(out)
        assert code == 0 and "无需重新扫码" in r["message"], sim.debug(err)
        assert not sim.events("qr_issued")

    parallel(login_with_qr_refresh, check_valid_and_expired, login_when_cookie_still_valid)
