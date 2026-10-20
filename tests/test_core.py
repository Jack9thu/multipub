from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from multipub import history
from multipub.cli import main
from multipub.config import load_settings
from multipub.manifest import (ManifestError, common_issues, load_manifest,
                               parse_platform_list, parse_schedule, parse_tags)
from multipub.models import Kind, PlatformPost, Result, Status
from multipub.runner import run_worker

from .conftest import write_post


def test_parse_helpers():
    assert parse_tags("a,b，c #d e#") == ["a", "b", "c", "d", "e"]
    assert parse_tags(["#x", "x", " y "]) == ["x", "y"]
    assert parse_platform_list("b站,抖音 xhs,知乎") == ["bilibili", "douyin", "xiaohongshu", "zhihu"]
    assert parse_schedule("2030-01-02 03:04") == datetime(2030, 1, 2, 3, 4)
    assert parse_schedule("2030-01-02T03:04:05") == datetime(2030, 1, 2, 3, 4)
    with pytest.raises(ManifestError):
        parse_schedule("tomorrow")
    with pytest.raises(ManifestError):
        parse_platform_list("weibo")


def test_manifest_overrides(media: Path):
    (media / "zh.md").write_text("知乎专用正文", encoding="utf-8")
    p = write_post(media, """
kind: note
title: 通用标题
body: 通用正文
tags: 徒步, 周末
images: [images/1.jpg, images/2.jpg]
account: main
platforms:
  douyin: {title: 抖音标题}
  xiaohongshu:
    tags: [小红书专用]
    account: alt
  zhihu:
    body_file: zh.md
    as: article
  bilibili: false
""")
    m = load_manifest(p)
    assert m.kind is Kind.NOTE
    assert m.targets() == ["douyin", "xiaohongshu", "zhihu"]
    dy = m.for_platform("douyin")
    assert dy.title == "抖音标题" and dy.body == "通用正文" and dy.account == "main"
    assert dy.images[0] == (media / "images" / "1.jpg").resolve()
    xhs = m.for_platform("xhs")
    assert xhs.tags == ["小红书专用"] and xhs.account == "alt"
    zh = m.for_platform("zhihu", account_override="other")
    assert zh.body == "知乎专用正文" and zh.options == {"as": "article"} and zh.account == "other"
    roundtrip = PlatformPost.from_dict(json.loads(json.dumps(zh.to_dict())))
    assert roundtrip == zh


def test_common_issues(media: Path):
    post = PlatformPost(platform="douyin", account="bad name", kind=Kind.VIDEO,
                        video=media / "missing.mp4",
                        schedule=datetime.now() - timedelta(minutes=1))
    fields = {i.field for i in common_issues(post)}
    assert {"account", "video", "schedule"} <= fields


def test_cli_validate_and_dry_run(home: Path, media: Path, capsys):
    write_post(media, """
kind: video
title: 标题
body: 简介
video: video.mp4
platforms: {fake: {}}
""")
    assert main(["validate", str(media), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["posts"][0]["platform"] == "fake"

    assert main(["publish", str(media), "--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["status"] == "dry_run"


def test_cli_validation_error(home: Path, media: Path, capsys):
    write_post(media, "kind: video\ntitle: " + "长" * 60 + "\nvideo: video.mp4\nplatforms: [fake]\n")
    assert main(["publish", str(media), "--json", "--yes"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "validation"


def test_publish_requires_yes_when_not_tty(home: Path, media: Path, monkeypatch):
    write_post(media, "kind: video\ntitle: t\nvideo: video.mp4\nplatforms: [fake]\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["publish", str(media)]) == 2


def test_publish_end_to_end_and_dedupe(home: Path, media: Path, capsys):
    write_post(media, "kind: video\ntitle: t\nvideo: video.mp4\nplatforms: [fake]\n")
    assert main(["publish", str(media), "--yes", "--json", "--gap", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    r = out["results"][0]
    assert r["status"] == "published" and r["url"] == "https://example.com/p/1" and r["submit_started"]

    # 第二次：同一内容默认跳过
    assert main(["publish", str(media), "--yes", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["status"] == "skipped"

    # --force 再发
    assert main(["publish", str(media), "--yes", "--json", "--force"]) == 0
    rows = history.read(load_settings())
    assert [row["status"] for row in rows] == ["published", "published"]


@pytest.mark.parametrize("mode,status,code,retryable,submit", [
    ("fail", "failed", "platform_error", True, False),
    ("needs_action", "needs_action", "login_required", False, False),
    ("crash_after_submit", "failed", "unknown_outcome", False, True),
])
def test_worker_failure_modes(home: Path, media: Path, mode, status, code, retryable, submit):
    settings = load_settings()
    post = PlatformPost(platform="fake", account="a", kind=Kind.VIDEO, title="t",
                        video=media / "video.mp4", options={"fake": mode})
    events = []
    r = run_worker(settings, action="publish", platform="fake", account="a", headless=True,
                   run_dir=settings.runs_dir / "t", post=post, timeout=60, on_event=events.append)
    assert (r.status.value, r.error_code, r.retryable, r.submit_started) == (status, code, retryable, submit)


def test_worker_hang_is_killed(home: Path, media: Path):
    settings = load_settings()
    post = PlatformPost(platform="fake", account="a", kind=Kind.VIDEO, title="t",
                        video=media / "video.mp4", options={"fake": "hang"})
    # worker 内部超时 = max(30, timeout-30)，外部硬超时 = timeout
    r = run_worker(settings, action="publish", platform="fake", account="a", headless=True,
                   run_dir=settings.runs_dir / "t", post=post, timeout=31)
    assert r.status is Status.FAILED and r.error_code == "timeout" and r.retryable


def test_login_check_flow(home: Path, capsys):
    assert main(["check", "fake", "--json"]) == 3  # 没有账号
    capsys.readouterr()
    assert main(["login", "fake", "--account", "acc1", "--json"]) == 0
    capsys.readouterr()
    assert main(["check", "fake", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["account"] == "acc1" and out["results"][0]["status"] == "ok"


def test_new_templates(home: Path, tmp_path: Path):
    for kind in ("video", "note", "article"):
        d = tmp_path / f"new-{kind}"
        assert main(["new", str(d), "--kind", kind]) == 0
        m = load_manifest(d)
        assert m.kind.value == kind


def test_result_roundtrip():
    r = Result(platform="x", account="a", action="publish", status=Status.SUBMITTED)
    assert Result.from_dict(json.loads(json.dumps(r.to_dict()))) == r


def test_worker_module_runs_standalone(home: Path):
    job = {"action": "check", "platform": "fake", "account": "zz", "home": str(home),
           "headless": True, "run_dir": str(home / "r"), "timeout": 30}
    proc = subprocess.run([sys.executable, "-m", "multipub.worker"], input=json.dumps(job),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    last = json.loads(proc.stdout.strip().splitlines()[-1])
    assert last["event"] == "result" and last["result"]["status"] == "needs_action"
