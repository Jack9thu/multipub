"""知乎端到端模拟：真实 CLI（mpub publish / login / check）+ 本地仿真接口（httpx.MockTransport）+ 真 Chromium 登录页。

仿真见 tests/sim/zhihu/（server.py 的场景参数、接口与登录页；helpers.py 的数据目录 / 登录文件 / 清单）。
发布全程不开浏览器，只有登录类测试需要 Chromium；真实网络一律被拦截（httpx 走 MockTransport，浏览器走 context.route）。
每个测试里互不相关的场景并发跑（各自独立的数据目录），整体耗时约为最慢场景的耗时。
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from ._harness import requires_chromium
from .zhihu import helpers as h
from .zhihu.images import png
from .zhihu.server import QR_Z_C0_PREFIX, RISK_REDIRECT, SIM_Z_C0, XSRF

WORKERS = 10
ARTICLE_URL = "https://zhuanlan.zhihu.com/p/{}"
VERIFY_URL = "https://www.zhihu.com" + RISK_REDIRECT
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="用 SIGTERM 和 /proc 检查进程")
needs_display = pytest.mark.skipif(not h.has_display(), reason="有头登录需要 X 显示（DISPLAY 或 Xvfb）")


def P(*extra: str) -> list[str]:
    return ["publish", "{post}", "--yes", "--json", *extra]


LOGIN = ["login", "zhihu", "--account", "main", "--headless", "--json"]
CHECK = ["check", "zhihu", "--json"]


@dataclass
class Case:
    name: str
    kind: str = "article"
    scenario: dict[str, Any] | list[dict[str, Any]] = field(default_factory=dict)
    zhihu: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    images: int = 2
    body_md: str | None = None
    home: dict[str, Any] = field(default_factory=dict)
    runs: list[list[str]] | None = None  # 默认一次 publish；"{post}" 会被替换
    setup: Callable[[Path, Path], None] | None = None  # (root, home)：额外准备（如 Obsidian 库）


@dataclass
class Run:
    case: Case
    outs: list[tuple[int, dict[str, Any] | None, str, float]]
    home: Path
    log: Path
    post: Path
    ends: list[float] = field(default_factory=list)  # 每次运行结束的时刻（time.time()），按时间切分仿真日志

    def code(self, i: int = 0) -> int:
        return self.outs[i][0]

    def secs(self, i: int = 0) -> float:
        return self.outs[i][3]

    def result(self, i: int = 0) -> dict[str, Any]:
        out = self.outs[i][1] or {}
        rows = out.get("results") or ([out["result"]] if out.get("result") else [])
        assert rows, f"第 {i + 1} 次运行没有结果"
        return rows[-1]

    def stderr(self, i: int = 0) -> str:
        return self.outs[i][2]

    def events(self, kind: str | None = None, **match: Any) -> list[dict[str, Any]]:
        return h.events(self.log, kind, **match)

    def reqs(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        return h.requests(self.log, method, path)

    @property
    def publishes(self) -> list[dict[str, Any]]:
        return self.events("publish")

    def publishes_in(self, i: int) -> list[dict[str, Any]]:
        start = self.ends[i - 1] if i else 0
        return [e for e in self.publishes if start <= e["t"] <= self.ends[i]]

    def debug(self) -> str:
        parts = [f"[{self.case.name}]"]
        for code, out, err, secs in self.outs:
            parts.append(f"exit={code} {secs:.1f}s out={json.dumps(out, ensure_ascii=False)[:1500]}\n"
                         f"stderr={err[-1500:]}")
        rows = [{k: v for k, v in e.items() if k not in ("html", "biz", "json")} for e in self.events()]
        parts.append(f"sim-log={json.dumps(rows, ensure_ascii=False)[-3000:]}")
        parts.append(h.run_log(self.home)[-2000:])
        return "\n".join(parts)


def _run_case(tmp_path: Path, case: Case) -> Run:
    root = tmp_path / case.name
    home = h.home_for(root, **case.home)
    post = h.write_post(root / "我的 帖子" / "第1期", case.kind, images=case.images, zhihu=case.zhihu,
                        body_md=case.body_md, **case.fields)
    if case.setup:
        case.setup(root, home)
    log = root / "sim.jsonl"
    outs, ends = [], []
    for i, args in enumerate(case.runs or [P()]):
        scenario = case.scenario[i] if isinstance(case.scenario, list) else case.scenario
        outs.append(h.run([a.replace("{post}", str(post)) for a in args], home, log, scenario, timeout=240))
        ends.append(time.time())
    return Run(case, outs, home, log, post, ends)


def run_all(tmp_path: Path, cases: list[Case]) -> dict[str, Run]:
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        runs = list(ex.map(lambda c: _run_case(tmp_path, c), cases))
    return {r.case.name: r for r in runs}


def check_all(runs: dict[str, Run], checks: dict[str, Callable[[Run], None]]) -> None:
    """逐个场景断言，收集所有失败一起报（一次跑完能看到全部问题）。"""
    assert set(checks) == set(runs), set(runs) ^ set(checks)
    failures = []
    for name, check in checks.items():
        try:
            check(runs[name])
        except AssertionError as exc:
            failures.append(f"{name}: {exc}\n{runs[name].debug()}")
    assert not failures, "\n\n".join(failures)


# ---------------------------------------------------------------- 断言片段

def published(run: Run, i: int = 0, *, kind: str = "article", status: str = "published", limit: float = 45) -> str:
    r = run.result(i)
    assert run.code(i) == 0 and r["status"] == status, r
    assert r["submit_started"] is True and r["item_id"], r
    if kind == "article":
        assert r["url"] == ARTICLE_URL.format(r["item_id"]), r
    else:
        assert r["url"] == f"https://www.zhihu.com/pin/{r['item_id']}", r
    assert run.secs(i) < limit, f"耗时 {run.secs(i):.0f} 秒"
    return r["item_id"]


def stopped_before_submit(run: Run, status: str, code: str, i: int = 0) -> dict[str, Any]:
    """第 i 次运行在发布之前就停下：没有发布请求、没有 submit_started，查重不会拦下一次。"""
    r = run.result(i)
    assert r["status"] == status and r["error_code"] == code, r
    assert r["submit_started"] is False and not run.publishes_in(i), r
    hist = [row for row in h.history(run.home) if row.get("started_at") == r.get("started_at")]
    assert hist and hist[-1]["error_code"] == code and hist[-1]["submit_started"] is False, hist
    return r


def sent_once(run: Run) -> None:
    assert len(run.publishes) == 1, f"发布接口被调用 {len(run.publishes)} 次"


def skipped_as_duplicate(run: Run, i: int) -> None:
    r = run.result(i)
    assert run.code(i) == 0 and r["status"] == "skipped" and "--force" in r["message"], r


def no_leaks(run: Run) -> None:
    """知乎 cookie 不发给图床和第三方图片站；写请求都带 x-xsrftoken；所有请求都用登录时记下的 UA。"""
    assert not run.events("cookie_leak"), run.events("cookie_leak")
    assert not run.events("xsrf_rejected"), run.events("xsrf_rejected")
    assert not run.events("unknown_route"), run.events("unknown_route")
    zhihu = [r for r in run.reqs() if r["host"].endswith("zhihu.com")]
    assert zhihu and all(r["ua"] == h.SIM_UA for r in zhihu), {r["ua"] for r in zhihu}
    writes = [r for r in zhihu if r["method"] != "GET"]
    assert all(r["xsrf"] == XSRF and r["xrw"] == "fetch" and r["origin"] for r in writes), writes
    assert all(r["z_c0"] == SIM_Z_C0 for r in zhihu)


# ================================================================ 文章 / 想法：日常使用

def _obsidian_vault(root: Path, home: Path) -> None:
    """Obsidian 库：笔记在 笔记/旅行/，附件在库根目录的 附件/；post.yaml 的 body_file 指向笔记（见 OBSIDIAN_NOTE）。"""
    vault = root / "我的 库"
    (vault / "附件").mkdir(parents=True)
    (vault / "笔记" / "旅行").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    (vault / "附件" / "Pasted image 20261020.png").write_bytes(png(90, 60))
    (vault / "附件" / "山顶 全景.png").write_bytes(png(91, 61))
    (vault / "笔记" / "旅行" / "西山.md").write_text(
        "# 西山\n\n第一段。\n\n![[Pasted image 20261020.png]]\n\n![[附件/山顶 全景.png|400]]\n\n"
        "![本地](../../附件/山顶 全景.png)\n\n`![[代码里的不算.png]]`\n", encoding="utf-8")


OBSIDIAN_NOTE = Path("我的 库") / "笔记" / "旅行" / "西山.md"


TITLE_100 = "西山环线徒步记录" * 12 + "到此为止"  # 正好 100 字


def test_article_and_pin_daily(tmp_path: Path):
    cases = [
        # 文章：正文本地图（中文 + 空格，<…> 与百分号两种写法）、远程 webp、追加图、封面、2 个话题、AI 声明、目录；
        # 再跑一次被查重跳过，--force 再发（图片知乎已有：state=1，不再传 OSS）
        Case("article", zhihu={"statement": "ai", "toc": True}, runs=[P(), P(), P("--force")]),
        Case("draft_only", zhihu={"draft": True, "statement": "ai"}),
        Case("obsidian", images=0, setup=_obsidian_vault,
             fields={"body_file": str(tmp_path / "obsidian" / OBSIDIAN_NOTE)}),
        Case("title_100", images=0, body_md=h.SMALL_MD, fields={"title": TITLE_100, "cover": None}),
        Case("pin_9", kind="note", images=9, zhihu={"tags_in_text": True}),
        Case("note_as_article", kind="note", images=3, zhihu={"as": "article", "statement": "fiction"}),
        Case("bom_account", images=0, body_md=h.SMALL_MD, home={"bom": True}),
    ]
    runs = run_all(tmp_path, cases)

    def article(run: Run) -> None:
        first = published(run, 0)
        no_leaks(run)
        sent_once_per_run = [e for e in run.publishes]
        assert len(sent_once_per_run) == 2  # 第二次被查重拦下，--force 发了第二次
        skipped_as_duplicate(run, 1)
        second = published(run, 2)
        assert first != second
        pub = run.publishes[0]
        assert not pub["problems"], pub["problems"]
        assert pub["title"] == "周末徒步：西山环线 12 公里"
        assert pub["statement"] == {"disclaimer_type": "ai_creation", "disclaimer_status": "open"}
        biz = json.loads(pub["biz"])
        assert biz["table_of_contents_enabled"] is True and biz["disclaimer_type"] == "ai_creation"
        html = pub["html"]
        # 3 张正文图（同一张图两种写法、远程 webp 转 JPEG）+ 2 张追加图，全部在知乎图床
        assert html.count("<img src=\"https://picx.zhimg.com/") == 5 and "img.example.com" not in html
        assert "<h2>路线</h2>" in html and "<h3>装备</h3>" in html and "data-draft-type=\"table\"" in html
        assert "西山环线：" not in html and "tags: [草稿]" not in html  # 一级标题、front matter 去掉
        done = run.events("published")
        assert done[0]["topics"] == ["徒步", "Python"] and done[0]["cover"].startswith("https://picx.zhimg.com/")
        oss = run.events("oss_put")
        assert len(oss) == 5 and not any(e["problems"] for e in oss), oss  # 4 张不同的图 + 封面，签名都对
        regs = run.events("image_registered")
        assert {e["state"] for e in regs[:5]} == {2} and {e["state"] for e in regs[5:]} == {1}, regs
        hist = h.history(run.home)
        assert [r["status"] for r in hist] == ["published", "published"], hist
        assert "上传图片 1/" in run.stderr() and "%E5" not in run.stderr()  # 进度里显示文件名，不是百分号编码
        # 写请求之间至少间隔 write_interval（1 秒）
        writes = [r["t"] for r in run.reqs() if r["method"] != "GET" and r["host"].endswith("zhihu.com")
                  and r["pid"] == run.reqs()[0]["pid"]]
        assert all(b - a >= 0.95 for a, b in zip(writes, writes[1:])), writes

    def draft_only(run: Run) -> None:
        r = run.result()
        assert run.code() == 0 and r["status"] == "draft" and r["submit_started"] is False, r
        assert r["url"] == f"https://zhuanlan.zhihu.com/p/{r['item_id']}/edit" and "声明" in r["message"], r
        assert not run.publishes and run.events("draft_patched")
        no_leaks(run)

    def obsidian(run: Run) -> None:
        published(run)
        html = run.publishes[0]["html"]
        assert html.count("<img src=\"https://picx.zhimg.com/") == 3 and "![[" not in html.replace(
            "<code>![[代码里的不算.png]]</code>", ""), html
        assert "<code>![[代码里的不算.png]]</code>" in html

    def title_100(run: Run) -> None:
        published(run)
        assert run.publishes[0]["title"] == TITLE_100 and not run.publishes[0]["problems"]

    def pin_9(run: Run) -> None:
        published(run, kind="pin")
        pub = run.publishes[0]
        assert pub["action"] == "pin" and pub["medias"] == 9 and not pub["problems"], pub
        assert "<p>#徒步#</p>" in pub["html"] and pub["text_length"] > 0
        assert {e["source"] for e in run.events("image_registered")} == {"pin"}
        no_leaks(run)

    def note_as_article(run: Run) -> None:
        published(run)
        pub = run.publishes[0]
        assert pub["action"] == "article" and pub["html"].count("<img") == 3
        assert pub["statement"]["disclaimer_type"] == "fictional_creation"

    def bom_account(run: Run) -> None:
        published(run)

    check_all(runs, {"article": article, "draft_only": draft_only, "obsidian": obsidian, "title_100": title_100,
                     "pin_9": pin_9, "note_as_article": note_as_article, "bom_account": bom_account})


def test_validation_blocks_before_network(tmp_path: Path):
    """本地校验拦下的不联网：定时、标题超长、想法 10 张图、嵌入笔记 / 找不到的图片。"""
    cases = [
        Case("schedule", fields={"schedule": "2030-01-01 10:00"}),
        Case("title_101", fields={"title": TITLE_100 + "啊"}),
        Case("pin_10", kind="note", images=10),
        Case("embed_note", images=0, body_md="# 标题\n\n![[另一篇笔记]]\n\n![[不存在.png]]\n"),
    ]
    runs = run_all(tmp_path, cases)

    def blocked(*needles: str) -> Callable[[Run], None]:
        def check(run: Run) -> None:
            assert run.code() == 2, run.code()
            out = run.outs[0][1] or {}
            text = json.dumps(out.get("issues"), ensure_ascii=False)
            assert all(n in text for n in needles), text
            assert not run.reqs(), "校验失败还发了请求"
        return check

    check_all(runs, {"schedule": blocked("定时"), "title_101": blocked("100"), "pin_10": blocked("9 张"),
                     "embed_note": blocked("![[另一篇笔记]]", "不存在.png")})


# ================================================================ 失败与风控

def test_failures_never_false_success(tmp_path: Path):
    small = {"images": 0, "body_md": h.SMALL_MD}
    cases = [
        Case("expired", scenario={"auth": "expired"}, **small),
        Case("expired_file", home={"z_c0_days": -1}, **small),
        Case("reject", scenario={"publish": "reject"}, **small),
        Case("reject_http", scenario={"publish": "reject_http"}, **small),
        Case("risk_publish", scenario={"risk": ["publish"]}, **small),
        Case("bad_gateway", scenario={"publish": "502"}, runs=[P(), P()], **small),
        Case("read_timeout", scenario={"publish": "read_timeout"}, runs=[P(), P()], **small),
        Case("no_code", scenario={"publish": "no_code"}, **small),
        Case("html", scenario={"publish": "html"}, **small),
        Case("image_stuck", scenario={"image_status": "stuck"}, home={"image_timeout": 4}, **small),
        Case("oss_403", scenario={"oss": "403"}, **small),
        Case("topic_risk", scenario={"risk": ["topics"]}, **small),
        Case("invisible", scenario={"visible": False}, **small),
        Case("pin_reject", kind="note", images=1, scenario={"publish": "reject"}),
        Case("draft_500", scenario={"draft_create": "500"}, **small),
        # 网络慢、图片处理慢：照样发完，不超时
        Case("slow", images=0, scenario={"api_delay_ms": 300, "image_processing_polls": 3, "publish_delay_ms": 2000}),
        # 登录在中途过期：草稿已建好 → 提示重新登录并给编辑页；发布接口 401 → 没发出去，重新登录后可再发
        Case("expire_mid", scenario={"expire_at": "patch"}, **small),
        Case("expire_at_publish", scenario={"expire_at": "publish"}, **small),
    ]
    runs = run_all(tmp_path, cases)

    def login_required(run: Run) -> None:
        r = stopped_before_submit(run, "needs_action", "login_required")
        assert run.code() == 3 and "mpub login zhihu --account main" in r["message"], r
        assert run.secs() < 10 and not run.events("draft_created")

    def expired_file(run: Run) -> None:
        login_required(run)
        assert not run.reqs(), "登录文件里的 z_c0 已过期，不该再请求"

    def rejected(run: Run) -> None:
        r = run.result()
        assert run.code() == 1 and r["status"] == "failed" and r["error_code"] == "platform_error", r
        assert r["url"].endswith("/edit") and "内容涉嫌违反社区规范" in r["message"], r
        assert r["evidence"] and Path(r["evidence"][0]).is_file(), r
        sent_once(run)

    def risk_publish(run: Run) -> None:
        r = run.result()
        assert run.code() == 3 and r["status"] == "needs_action" and r["error_code"] == "risk_control", r
        assert r["url"] == VERIFY_URL and "草稿已保存" in r["message"], r
        assert (run.home / "accounts" / "zhihu" / "main.verify.txt").read_text(encoding="utf-8") == VERIFY_URL

    def unknown(run: Run, *, rerun: bool = True) -> None:
        r = run.result()
        assert run.code() == 1 and r["error_code"] == "unknown_outcome" and r["retryable"] is False, r
        assert "不要直接重发" in r["message"] and r["url"].endswith("/edit"), r
        sent_once(run)
        # 草稿和文章同一个 id：绝不拿草稿 id 去「确认」（作者查自己的草稿也是 200）
        assert not run.reqs("GET", "/api/v4/articles/*"), "结果不明时拿草稿 id 去确认了"
        if rerun:
            skipped_as_duplicate(run, 1)
            assert "结果未知" in run.result(1)["message"]

    def stopped(code: str) -> Callable[[Run], None]:
        def check(run: Run) -> None:
            stopped_before_submit(run, "failed", code)
            assert not run.events("draft_created"), "图片失败前不该建草稿"
        return check

    def topic_risk(run: Run) -> None:
        r = run.result()
        published(run)
        assert "话题被知乎风控拦截" in r["message"], r

    def invisible(run: Run) -> None:
        r = run.result()
        published(run, status="submitted")
        assert "审核" in r["message"]

    def pin_reject(run: Run) -> None:
        r = run.result()
        assert run.code() == 1 and r["error_code"] == "platform_error" and r["submit_started"] is True, r
        sent_once(run)

    def draft_500(run: Run) -> None:
        r = stopped_before_submit(run, "failed", "platform_error")
        assert r["retryable"] is True, r

    def slow(run: Run) -> None:
        published(run, limit=60)
        sent_once(run)

    def expire_mid(run: Run) -> None:
        r = stopped_before_submit(run, "needs_action", "login_required")
        assert "草稿已保存" in r["message"] and r["url"].endswith("/edit"), r

    def expire_at_publish(run: Run) -> None:
        r = run.result()
        assert run.code() == 3 and r["error_code"] == "login_required" and r["submit_started"] is True, r
        assert r["url"].endswith("/edit") and not run.events("published"), r
        assert h.history(run.home)[-1]["status"] == "needs_action"  # 没发出去：查重不拦，重新登录后可再发

    check_all(runs, {
        "slow": slow, "expire_mid": expire_mid, "expire_at_publish": expire_at_publish,
        "expired": login_required, "expired_file": expired_file, "reject": rejected, "reject_http": rejected,
        "risk_publish": risk_publish, "bad_gateway": unknown, "read_timeout": unknown,
        "no_code": lambda r: unknown(r, rerun=False), "html": lambda r: unknown(r, rerun=False),
        "image_stuck": stopped("timeout"), "oss_403": stopped("platform_error"), "topic_risk": topic_risk,
        "invisible": invisible, "pin_reject": pin_reject, "draft_500": draft_500,
    })


# ================================================================ 登录、检查、风控验证（需要浏览器）

def _file_cookies(home: Path) -> dict[str, dict[str, Any]]:
    data = json.loads((home / "accounts" / "zhihu" / "main.json").read_text(encoding="utf-8"))
    return {c["name"]: c for c in data["cookies"]}


@requires_chromium
def test_login_check_and_risk_flows(tmp_path: Path):
    small = {"images": 0, "body_md": h.SMALL_MD, "fields": {"cover": None}}
    cases = [
        # 扫码登录（页面自己请求二维码接口；拦截后重画 PNG）→ 检查 → 用新登录态发布
        Case("fresh", home={"logged_in": False}, runs=[LOGIN, CHECK, P()], **small),
        Case("qr_refresh", home={"logged_in": False}, scenario={"qr_expire_after_polls": 2, "scan_after_polls": 6},
             runs=[LOGIN]),
        Case("closed_after_scan", home={"logged_in": False}, scenario={"close_after_scan_ms": 150},
             runs=[LOGIN, P()], **small),
        Case("closed_before_scan", home={"logged_in": False},
             scenario={"signin_close_after_ms": 1500, "scan_after_polls": 10**6}, runs=[LOGIN]),
        Case("already", runs=[LOGIN]),
        # 发布前 /me 就被风控 → 检查也提示 → 登录（带旧登录态打开验证页，自动放行）→ 再发布成功
        Case("risk_flow", scenario={"risk": ["me", "publish"]}, runs=[P(), CHECK, LOGIN, P()], **small),
        Case("risk_stuck", scenario={"risk": ["me"], "verify_page": "stuck"}, runs=[CHECK, LOGIN], **small),
        # 手写的 {name: value} 登录文件 + 风控：浏览器要能用这份 cookie 打开验证页
        Case("dict_file_risk", home={"fmt": "dict"}, scenario={"risk": ["me"]}, runs=[CHECK, LOGIN, P()], **small),
        # 只有 z_c0 没有 _xsrf：发布提示重新登录；login 不用扫码就能补齐；再发布成功
        Case("no_xsrf", home={"xsrf": False}, runs=[P(), LOGIN, P()], **small),
        # 登录时首页没下发 _xsrf：登录结果里提示；再登录一次补齐（不用扫码）
        Case("home_no_xsrf", home={"logged_in": False}, scenario=[{"home_sets_xsrf": False}, {}, {}],
             runs=[LOGIN, LOGIN, P()], **small),
        # 登录后首页自己再跳一次（_xsrf 在跳转后的页面下发）：照样等到 _xsrf
        Case("home_redirect", home={"logged_in": False}, scenario={"home_redirect": True}, runs=[LOGIN]),
        # 拦不到二维码接口：8 秒后截图上报；用户在页面上用手机号登录也能完成
        Case("qr_screenshot", home={"logged_in": False}, scenario={"qr_api": False, "phone_login_after_ms": 10000},
             runs=[LOGIN]),
    ]
    runs = run_all(tmp_path, cases)

    def ok(run: Run, i: int, *needles: str) -> dict[str, Any]:
        r = run.result(i)
        assert run.code(i) == 0 and r["status"] == "ok", r
        assert all(n in r["message"] for n in needles), r
        return r

    def fresh(run: Run) -> None:
        ok(run, 0, "登录成功：模拟知友")
        assert '"event": "qrcode"' in run.stderr(0) and "zhihu-main.png" in run.stderr(0)
        assert not (run.home / "qrcode" / "zhihu-main.png").exists(), "登录完二维码图片没删"
        cookies = _file_cookies(run.home)
        assert cookies["z_c0"]["value"].startswith(QR_Z_C0_PREFIX) and cookies["z_c0"]["httpOnly"] is True
        assert cookies["_xsrf"]["value"] == XSRF and "d_c0" in cookies
        ua = (run.home / "accounts" / "zhihu" / "main.ua.txt").read_text(encoding="utf-8")
        assert "Chrome/" in ua and "Headless" not in ua, ua
        ok(run, 1, "已登录：模拟知友", "天后过期")
        published(run, 2)
        reqs = [r for r in run.reqs() if r["host"].endswith("zhihu.com") and r["t"] > run.events("page")[-1]["t"]]
        assert reqs and all(r["ua"] == ua and r["z_c0"] == cookies["z_c0"]["value"] for r in reqs)
        assert run.secs(0) < 30

    def qr_refresh(run: Run) -> None:
        ok(run, 0)
        assert run.stderr(0).count('"event": "qrcode"') == 2 and "二维码已刷新" in run.stderr(0)

    def closed_after_scan(run: Run) -> None:
        ok(run, 0, "登录成功")
        assert run.events("page", evt="user_closed_window")
        assert _file_cookies(run.home)["_xsrf"]["value"] == XSRF  # 窗口关了也开新页面补齐了 _xsrf
        published(run, 1)

    def closed_before_scan(run: Run) -> None:
        r = run.result()
        assert run.code() == 1 and "窗口已关闭" in r["message"] and run.secs() < 20, r
        assert not (run.home / "accounts" / "zhihu" / "main.json").exists()

    def already(run: Run) -> None:
        ok(run, 0, "无需重新登录")
        assert not run.events("page"), "已登录还开了浏览器"

    def risk_flow(run: Run) -> None:
        r = run.result(0)
        assert r["status"] == "needs_action" and r["error_code"] == "risk_control" and r["url"] == VERIFY_URL, r
        assert run.result(1)["error_code"] == "risk_control"
        ok(run, 2, "登录成功")
        assert run.events("page", evt="verify_open") and run.events("page", evt="verify", passed=True)
        assert not run.events("page", evt="qrcode_issued"), "过验证不该重新扫码"
        assert not (run.home / "accounts" / "zhihu" / "main.verify.txt").exists()
        published(run, 3)

    def risk_stuck(run: Run) -> None:
        r = run.result(1)
        assert run.code(1) == 3 and r["error_code"] == "risk_control" and "去掉 --headless" in r["message"], r
        assert run.secs(1) < 40, run.secs(1)
        assert (run.home / "accounts" / "zhihu" / "main.verify.txt").exists()

    def dict_file_risk(run: Run) -> None:
        assert run.result(0)["error_code"] == "risk_control"
        ok(run, 1, "登录成功")
        assert run.events("page", evt="verify", passed=True) and not run.events("page", evt="qrcode_issued")
        published(run, 2)

    def no_xsrf(run: Run) -> None:
        r = stopped_before_submit(run, "needs_action", "login_required", i=0)
        assert "_xsrf" in r["message"] and "不用重新扫码" in r["message"], r
        ok(run, 1, "登录成功")
        assert not run.events("page", evt="qrcode_issued"), "补 _xsrf 不该重新扫码"
        assert _file_cookies(run.home)["_xsrf"]["value"] == XSRF
        published(run, 2)

    def home_no_xsrf(run: Run) -> None:
        ok(run, 0, "没拿到 _xsrf")
        ok(run, 1, "登录成功")
        assert len(run.events("page", evt="qrcode_issued")) == 1, "第二次登录不该重新扫码"
        published(run, 2)

    def home_redirect(run: Run) -> None:
        ok(run, 0, "登录成功")
        assert "没拿到 _xsrf" not in run.result(0)["message"] and run.events("page", evt="follow")
        assert _file_cookies(run.home)["_xsrf"]["value"] == XSRF

    def qr_screenshot(run: Run) -> None:
        ok(run, 0, "登录成功")
        assert "页面截图（含二维码）" in run.stderr(0) and run.events("page", evt="phone_login")

    check_all(runs, {"fresh": fresh, "qr_refresh": qr_refresh, "closed_after_scan": closed_after_scan,
                     "closed_before_scan": closed_before_scan, "already": already, "risk_flow": risk_flow,
                     "risk_stuck": risk_stuck, "dict_file_risk": dict_file_risk, "no_xsrf": no_xsrf,
                     "home_no_xsrf": home_no_xsrf, "home_redirect": home_redirect, "qr_screenshot": qr_screenshot})


@requires_chromium
@needs_display
def test_headed_login(tmp_path: Path):
    """默认的有头登录（不加 --headless）：弹出窗口扫码，提示里说可以直接在窗口里用手机号登录。"""
    root = tmp_path / "headed"
    home = h.home_for(root, logged_in=False)
    log = root / "sim.jsonl"
    with h.xvfb() as display:
        code, out, err, secs = h.run(["login", "zhihu", "--account", "main", "--json"], home, log, {},
                                     display=display)
    assert code == 0 and (out or {}).get("result", {}).get("status") == "ok", (code, out, err[-2000:])
    assert "也可以直接在弹出的浏览器窗口里用手机号登录" in err
    assert (home / "accounts" / "zhihu" / "main.json").is_file() and secs < 30


# ================================================================ 中断与并发

def _interrupt(tmp_path: Path, name: str, args: list[str], scenario: dict[str, Any],
               reached: Callable[[Path], bool], logged_in: bool = True) -> tuple[int, list[int], Path, Path, Path]:
    root = tmp_path / name
    home = h.home_for(root, logged_in=logged_in)
    post = h.write_post(root / "帖子", "article", images=0, body_md=h.SMALL_MD)
    log = root / "sim.jsonl"
    tag = h.new_tag()
    proc = h.start([a.replace("{post}", str(post)) for a in args], home, log, scenario, tag=tag)
    deadline = time.monotonic() + 90
    while not reached(log) and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.2)
    assert reached(log), f"[{name}] 没走到中断点: {h.describe(proc.poll() or 0, None, '', home, log)}"
    time.sleep(0.5)
    proc.send_signal(h.SIGTERM)
    proc.communicate(timeout=60)
    time.sleep(2)
    return proc.returncode, h.tagged_pids(tag), home, log, post


@posix_only
@requires_chromium
def test_interrupts(tmp_path: Path):
    """SIGTERM 中途中断：不留孤儿进程（worker、登录用的浏览器）；发布记录与查重正确。"""
    cases = {
        # 图片处理中（没发出）→ 记成可重试，下次照常发
        "before_submit": (P(), {"image_status": "stuck"}, lambda log: len(h.requests(log, "GET", "/images/*")) >= 2,
                          False),
        # 发布请求已发出、没回 → 记成结果未知，下次被查重拦住
        "after_submit": (P(), {"publish": "hang"}, lambda log: bool(h.events(log, "publish")), True),
        # 登录等扫码时
        "login": (LOGIN, {"scan_after_polls": 10**6}, lambda log: bool(h.events(log, "page", evt="qrcode_issued")),
                  None),
    }

    def one(item: tuple[str, tuple[list[str], dict[str, Any], Callable[[Path], bool], bool | None]]) -> str:
        name, (args, scenario, reached, sent) = item
        code, orphans, home, log, post = _interrupt(tmp_path, name, args, scenario, reached,
                                                    logged_in=sent is not None)
        problems = []
        if code != 130:
            problems.append(f"退出码 {code}")
        if orphans:
            problems.append(f"孤儿进程 {orphans}")
        if sent is None:
            if (home / "accounts" / "zhihu" / "main.json").exists():
                problems.append("没登录完却留下了登录文件")
            return f"{name}: {'; '.join(problems)}" if problems else ""
        hist = h.history(home)
        if not hist or hist[-1]["submit_started"] is not sent:
            problems.append(f"发布记录不对: {hist}")
        elif sent and hist[-1]["error_code"] != "unknown_outcome":
            problems.append(f"已发出却没记成结果未知: {hist[-1]}")
        code2, out2, err2, _ = h.run(["publish", str(post), "--yes", "--json"], home, log, {})
        status = ((out2 or {}).get("results") or [{}])[-1].get("status")
        if sent and status != "skipped":
            problems.append(f"结果未知却没被查重拦住: {status}")
        if not sent and status != "published":
            problems.append(f"没发出却不能重发: {status} {err2[-500:]}")
        return f"{name}: {'; '.join(problems)}" if problems else ""

    with ThreadPoolExecutor(max_workers=3) as ex:
        problems = [p for p in ex.map(one, cases.items()) if p]
    assert not problems, "\n".join(problems)


@posix_only
def test_concurrent_runs_publish_once(tmp_path: Path):
    """同一条想法几乎同时跑两次（重复回车、两个终端、技能重试）：只能发出去一次。"""
    root = tmp_path / "race"
    home = h.home_for(root)
    post = h.write_post(root / "帖子", "note", images=1)
    log = root / "sim.jsonl"
    scenario = {"api_delay_ms": 150, "publish_delay_ms": 300}
    first = h.start(["publish", str(post), "--yes", "--json"], home, log, scenario)
    time.sleep(1)
    second = h.start(["publish", str(post), "--yes", "--json"], home, log, scenario)
    for proc in (first, second):
        proc.communicate(timeout=120)
    assert len(h.events(log, "published")) == 1, [e["id"] for e in h.events(log, "published")]
