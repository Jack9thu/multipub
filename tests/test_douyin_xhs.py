"""抖音 / 小红书适配器与 vendored SAU 的离线测试（不启动浏览器、不联网）。"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from multipub.config import Settings, load_settings
from multipub.models import (ErrorCode, Kind, LoginRequired, PlatformPost,
                             PublishError, Status, ValidationFailed)
from multipub.platforms import _sau_bridge as bridge
from multipub.platforms.base import JobContext
from multipub.platforms.douyin import DouyinAdapter, cover_slots, resolve_declaration
from multipub.platforms.xiaohongshu import XiaohongshuAdapter

SAU_DIR = Path(__file__).resolve().parents[1] / "src" / "multipub" / "_sau"
UPSTREAM_SHA = "0012d2c355f88f683cc38dde2a2db209e14091bc"


# ------------------------------------------------------------------ fixtures / fakes

@pytest.fixture
def settings(home: Path) -> Settings:
    s = load_settings()
    s.ensure_dirs()
    return s


@pytest.fixture(autouse=True)
def _cleanup_sau_logging():
    yield
    from multipub._sau.utils import log

    log.remove_sinks()


@pytest.fixture
def sau(settings: Settings):
    """configure() 之后导入 vendored 模块。"""
    from multipub._sau import conf

    conf.configure(settings, headless=True)
    from multipub._sau.uploader.douyin_uploader import main as dy
    from multipub._sau.uploader.xiaohongshu_uploader import main as xhs

    return conf, dy, xhs


def make_ctx(settings: Settings, platform: str, tmp_path: Path, action: str = "publish",
             account: str = "main") -> tuple[JobContext, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    ctx = JobContext(settings=settings, platform=platform, account=account, action=action,
                     headless=True, run_dir=tmp_path / "run", emit=events.append)
    return ctx, events


def login_file(ctx: JobContext) -> Path:
    ctx.account_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.account_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    return ctx.account_file


class FakeLocator:
    def __init__(self, page: "FakePage", key: str):
        self.page, self.key = page, key

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self.page.visible.get(self.key) else 0

    async def is_visible(self) -> bool:
        return bool(self.page.visible.get(self.key))

    async def click(self, **_: Any) -> None:
        if not self.page.visible.get(self.key):
            raise RuntimeError(f"找不到元素 {self.key}")
        self.page.clicks.append(self.key)
        hook = self.page.on_click.get(self.key)
        if hook is not None:
            hook(self.page)

    async def fill(self, value: str, **_: Any) -> None:
        self.page.filled[self.key] = value

    async def wait_for(self, state: str = "visible", timeout: float | None = None) -> None:
        if not self.page.visible.get(self.key):
            raise RuntimeError(f"等不到元素 {self.key}")

    async def get_attribute(self, name: str) -> None:
        return None


class FakeRequest:
    def __init__(self, url: str, method: str = "POST"):
        self.url, self.method = url, method


class FakeResponse:
    def __init__(self, request: FakeRequest, body: Any, status: int = 200):
        self.request, self.url, self.body, self.status = request, request.url, body, status

    async def json(self) -> Any:
        return self.body


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    """只实现 vendored 代码在被测路径上用到的那几个 API。"""

    def __init__(self, visible: dict[str, bool] | None = None, success_after_click: str | None = None):
        self.visible = dict(visible or {})
        self.success_after_click = success_after_click
        self.succeeded = False  # 由 on_click 回调置位，模拟页面跳到成功页
        self.on_click: dict[str, Any] = {}
        self.listeners: dict[str, list[Any]] = {}
        self.clicks: list[str] = []
        self.filled: dict[str, str] = {}
        self.keyboard = FakeKeyboard()
        self.url = "https://creator.example/publish"

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, f"text={text}")

    def get_by_role(self, role: str, name: str = "", exact: bool = False) -> FakeLocator:
        return FakeLocator(self, f"role={role}:{name}")

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event: str, arg: Any) -> None:
        for callback in self.listeners.get(event, []):
            callback(arg)

    async def evaluate(self, *args: Any) -> None:
        return None

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def wait_for_url(self, url: str, timeout: float | None = None) -> None:
        if self.succeeded:
            return
        if self.success_after_click is None or self.success_after_click not in self.clicks:
            raise RuntimeError("wait_for_url timeout")

    async def wait_for_timeout(self, ms: float) -> None:
        return None

    async def screenshot(self, path: str, full_page: bool = False) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    async def eval_on_selector(self, *args: Any) -> None:
        return None


# ------------------------------------------------------------------ vendored 模块

def test_vendored_import_after_configure(settings: Settings, sau):
    conf, dy, xhs = sau
    assert conf.is_configured()
    assert conf.BASE_DIR == settings.sau_home and conf.BASE_DIR.is_dir()
    # 与 `mpub verify-code douyin` 写入的位置一致
    assert conf.verify_code_file() == settings.sau_home / "verify_code.txt"
    assert conf.launch_kwargs(True, ["--x"]) == {"headless": True, "args": ["--x"], "channel": "chromium"}

    from multipub._sau.utils.base_social_media import STEALTH_JS_PATH

    assert STEALTH_JS_PATH.is_file() and STEALTH_JS_PATH.parent == SAU_DIR / "utils"
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json")
    assert v.hooks.platform == "douyin" and v.published_item_id is None


def test_launch_kwargs_follow_browser_settings(settings: Settings, monkeypatch):
    from multipub._sau import conf

    settings.browser.executable_path = r"C:\Program Files\浏览器\chrome.exe"
    conf.configure(settings, headless=False)
    assert conf.LOCAL_CHROME_HEADLESS is False
    kw = conf.launch_kwargs(False)
    assert kw == {"headless": False, "executable_path": r"C:\Program Files\浏览器\chrome.exe"}

    settings.browser.executable_path = ""
    settings.browser.channel = "msedge"
    conf.configure(settings, headless=True)
    assert conf.launch_kwargs(True)["channel"] == "msedge"

    monkeypatch.setattr(conf, "_configured", False)
    with pytest.raises(RuntimeError):
        conf.launch_kwargs(True)


def test_imports_write_nothing_to_stdout(tmp_path: Path):
    code = r"""
import sys
from pathlib import Path
import multipub.platforms.douyin as d, multipub.platforms.xiaohongshu as x
from multipub.platforms import load_adapter
from multipub.config import load_settings
from multipub.models import Kind, PlatformPost
for name in ("douyin", "xiaohongshu"):
    cls = load_adapter(name)
    cls.validate(PlatformPost(platform=name, account="a", kind=Kind.NOTE, title="t", images=[Path("1.jpg")]))
    cls.doctor(load_settings())
assert "patchright" not in sys.modules, "适配器模块导入了 patchright"
assert "multipub._sau.uploader.douyin_uploader.main" not in sys.modules
from multipub._sau import conf
conf.configure(load_settings(), headless=True)
from multipub._sau.uploader.douyin_uploader import main as dy
from multipub._sau.uploader.xiaohongshu_uploader import main as xhs
from multipub._sau.utils import login_qrcode
dy.douyin_logger.success("x"); xhs.xiaohongshu_logger.warning("y")
login_qrcode.print_terminal_qrcode("https://example.com", Path("qr.png"), "抖音APP")
"""
    env = dict(os.environ, MULTIPUB_HOME=str(tmp_path / "h"), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout == b""
    err = proc.stderr.decode("utf-8")
    assert "请使用抖音APP扫描" in err and "\x1b[" not in err  # 二维码走 stderr，日志无颜色


def test_patches_md_and_license():
    text = (SAU_DIR / "PATCHES.md").read_text(encoding="utf-8")
    assert UPSTREAM_SHA in text
    for letter in "abcdefghi":
        assert re.search(rf"^### {letter}\. ", text, re.M), f"PATCHES.md 缺少第 {letter} 条"
    assert "MIT License" in (SAU_DIR / "LICENSE").read_text(encoding="utf-8")
    from multipub import _sau

    assert _sau.UPSTREAM_COMMIT == UPSTREAM_SHA
    for rel in ("uploader/douyin_uploader/main.py", "uploader/xiaohongshu_uploader/main.py",
                "utils/log.py", "utils/login_qrcode.py", "utils/base_social_media.py"):
        assert "multipub patch:" in (SAU_DIR / rel).read_text(encoding="utf-8"), rel


def _vendored_py() -> list[Path]:
    return sorted(SAU_DIR.rglob("*.py"))


def test_vendored_source_invariants():
    dy_src = (SAU_DIR / "uploader/douyin_uploader/main.py").read_text(encoding="utf-8")
    xhs_src = (SAU_DIR / "uploader/xiaohongshu_uploader/main.py").read_text(encoding="utf-8")
    # a. 不再默认 AI 声明
    assert 'self.declaration = "内容由AI生成"' not in dy_src
    assert "self.declaration = DOUYIN_AI_DECLARATION" not in dy_src
    # b. 头条/西瓜开关只在 sync_toutiao 时打开
    assert "if self.sync_toutiao and await page.locator(third_part_element).count()" in dy_src
    # c. 来源转载只在 apply_declarations 里有条件调用
    assert xhs_src.count("await self.check_original_declaration(page)") == 1
    assert "if self.repost_source:\n            await self.check_original_declaration(page)" in xhs_src
    # d. 最终发布按钮只经 _click_publish（PublishGuard）点击
    for src in (dy_src, xhs_src):
        assert "publish_button.click(" not in src
        assert src.count("await self._click_publish(publish_button") >= 1
    for path in _vendored_py():
        src = path.read_text(encoding="utf-8")
        # d. 每个 while True 都有对应的 Deadline
        loops = re.findall(r"^\s*while True:", src, re.M)
        assert len(loops) <= len(re.findall(r"= Deadline\(", src)), path
        # e. 浏览器启动统一走 conf.launch_kwargs()（不再写死 channel="chromium"）
        for m in re.finditer(r"\.launch\(", src):
            assert src[m.end():].startswith(("**sau_conf.launch_kwargs(", "**launch_kwargs)")), path
        if "**launch_kwargs)" in src:
            assert "launch_kwargs = sau_conf.launch_kwargs(" in src, path
        # f/h/i. 不 print 到 stdout、不碰 sys.stdout、不读 stdin、不用上游的顶层包导入
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "input", path
                if node.func.id == "print":
                    assert any(k.arg == "file" for k in node.keywords), f"{path}:{node.lineno}"
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys":
                assert node.attr not in ("stdout", "stdin"), f"{path}:{node.lineno}"
            if isinstance(node, (ast.Import, ast.ImportFrom)) and not getattr(node, "level", 0):
                mod = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                assert (mod or "").split(".")[0] not in ("conf", "uploader", "utils"), f"{path}:{node.lineno}"


def test_vendored_defaults(sau):
    _, dy, xhs = sau
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json")
    assert v.declaration is None and v.sync_toutiao is False
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json")
    assert note.ai_generated is False and note.repost_source == ""
    assert note.title == "正文"  # 未填标题时用正文前 20 字（上游行为）


# ------------------------------------------------------------------ runtime 助手

async def test_deadline_and_fail_step(tmp_path: Path):
    from multipub._sau.runtime import (CaptchaRequired, Deadline, Hooks, LoginPageDetected,
                                       StepTimeout, fail_step)

    d = Deadline(0, "等待")
    assert d.expired and d.remaining() == 0
    d2 = Deadline(60, "等待")
    assert not d2.expired and d2.remaining() > 50

    evidence: list[Path] = []
    hooks = Hooks(evidence_dir=tmp_path, on_evidence=evidence.append, platform="douyin")
    with pytest.raises(StepTimeout) as info:
        await fail_step(FakePage(), hooks, d, after_submit=True, detail="平台返回: 频繁")
    exc = info.value
    assert exc.after_submit and exc.step == "等待" and "频繁" in str(exc)
    assert exc.screenshot and Path(exc.screenshot).is_file() and evidence == [Path(exc.screenshot)]

    with pytest.raises(CaptchaRequired):
        await fail_step(FakePage({"text=请完成安全验证": True}), hooks, d, after_submit=False)
    with pytest.raises(LoginPageDetected):
        await fail_step(FakePage({"text=扫码登录": True}), hooks, d, after_submit=False)
    with pytest.raises(StepTimeout) as info:
        await fail_step(None, Hooks(), d, after_submit=False)  # 没有页面也不能卡住
    assert info.value.screenshot is None and not info.value.after_submit


def test_hooks_call_swallows_callback_errors():
    from multipub._sau.runtime import Hooks

    def boom() -> None:
        raise RuntimeError("x")

    Hooks(before_publish_click=boom).call("before_publish_click")  # 不抛出
    Hooks().call("before_publish_click")


async def test_sms_code_wait_times_out(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import Hooks, SmsCodeTimeout

    asked: list[Path] = []
    hooks = Hooks(sms_code_required=asked.append, sms_timeout=0.2, poll_interval=0.02,
                  evidence_dir=tmp_path, platform="douyin")
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=hooks)
    code_file = tmp_path / "verify_code.txt"
    with pytest.raises(SmsCodeTimeout) as info:
        await v._wait_verify_code(FakePage(), code_file)
    assert asked == [code_file]
    assert info.value.screenshot and not info.value.after_submit

    code_file.write_text("\ufeff 123456 \n", encoding="utf-8")  # 记事本 BOM
    assert await v._wait_verify_code(FakePage(), code_file) == "123456"


async def test_sms_dialog_uses_fresh_code_file(sau, tmp_path: Path):
    conf, dy, _ = sau
    from multipub._sau.runtime import Hooks

    code_file = conf.verify_code_file()
    code_file.write_text("000000", encoding="utf-8")  # 上一次残留

    def user_writes_code(path: Path) -> None:
        assert not path.exists()  # 残留的旧验证码已被清掉
        path.write_text("654321", encoding="utf-8")

    hooks = Hooks(sms_code_required=user_writes_code, sms_timeout=2, poll_interval=0.01)
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=hooks)
    verify_btn = 'div.uc-ui-verify_sms-verify_button:has-text("验证")'
    page = FakePage({v.SMS_INPUT_SELECTOR: True, "text=获取验证码": True, verify_btn: True})
    assert await v._handle_sms_verification(page) is True
    assert page.filled[v.SMS_INPUT_SELECTOR] == "654321"
    assert page.clicks[:1] == ["text=获取验证码"] and verify_btn in page.clicks
    assert not code_file.exists()  # 用过即删
    assert await v._handle_sms_verification(FakePage()) is False  # 没有弹窗
    # 图文流程用 strict：只有一个 tel 输入框、没有验证组件特征时不当成短信弹窗
    assert await v._handle_sms_verification(FakePage({v.SMS_INPUT_SELECTOR: True}), strict=True) is False


async def test_xhs_publish_loop_marks_submit_and_is_bounded(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import Hooks, StepTimeout

    clicked: list[int] = []
    button = 'button:has-text("发布")'
    hooks = Hooks(before_publish_click=lambda: clicked.append(1), publish_timeout=5)
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=hooks)
    await note._publish_until_success(FakePage({button: True}, success_after_click=button), "图文")
    assert clicked == [1] and note._publish_clicked

    # 找不到发布按钮：不标记提交，截止时间到了就失败
    clicked.clear()
    hooks = Hooks(before_publish_click=lambda: clicked.append(1), publish_timeout=0.3, evidence_dir=tmp_path)
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=hooks)
    with pytest.raises(StepTimeout) as info:
        await note._publish_until_success(FakePage(), "图文")
    assert not info.value.after_submit and clicked == []


# ------------------------------------------------------------------ 防重复点击「发布」

DY_CREATE = "https://creator.douyin.com/web/api/media/aweme/create_v2/?read_aid=2906"
XHS_CREATE = "https://edith.xiaohongshu.com/web_api/sns/v2/note"
DY_PUBLISH = "role=button:发布"
XHS_PUBLISH = 'button:has-text("发布")'


def test_publish_guard_rules():
    from multipub._sau.runtime import PublishGuard

    now = [100.0]
    g = PublishGuard(reclick_gap=20, max_clicks=3, clock=lambda: now[0])
    assert g.may_click()
    g.clicked()
    assert not g.may_click()  # 刚点过
    now[0] += 21
    assert g.may_click()  # 没有任何请求、超过间隔：上次点击没生效
    g.request_sent()
    now[0] += 100
    assert not g.may_click()  # 请求已发出：只等结果
    g.request_done(None)
    assert not g.may_click()  # 结果不明也不再点
    g.blocked_by_platform()
    assert g.may_click()  # 平台弹窗拦下了：允许再点一次
    g.clicked()
    g.click_returned()
    assert not g.may_click()
    g.blocked_by_platform()
    g.clicked()
    assert g.clicks == 3 and not g.may_click()  # 次数上限
    g.blocked_by_platform()
    assert not g.may_click()

    ok = PublishGuard(clock=lambda: now[0])
    ok.clicked()
    ok.request_sent()
    ok.request_done(True)
    ok.blocked_by_platform()
    assert ok.succeeded and not ok.may_click()  # 拿到成功回执后绝不再点

    bad = PublishGuard(clock=lambda: now[0])
    bad.clicked()
    bad.request_sent()
    bad.request_done(False, "标题违规")
    assert bad.rejection_settled(15) is None  # 宽限期内（可能马上弹短信验证）
    now[0] += 16
    assert bad.rejection_settled(15) == "标题违规"
    bad.blocked_by_platform()  # 弹出了验证：之前的失败不算数
    assert bad.rejection_settled(0) is None


def _fast_hooks(tmp_path: Path, **kw: Any):
    from multipub._sau.runtime import Hooks

    base = dict(publish_timeout=1.2, reclick_gap=0, reject_grace=0, evidence_dir=tmp_path,
                sms_timeout=2, poll_interval=0.01, platform="douyin")
    base.update(kw)
    return Hooks(**base)


async def test_douyin_no_reclick_while_create_request_in_flight(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import StepTimeout

    marked: list[int] = []
    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                         hooks=_fast_hooks(tmp_path, before_publish_click=lambda: marked.append(1)))
    page = FakePage({DY_PUBLISH: True})
    page.on_click[DY_PUBLISH] = lambda p: p.emit("request", FakeRequest(DY_CREATE))  # 请求发出后一直没回
    with pytest.raises(StepTimeout) as info:
        await note._publish_until_success(page, "图文", "**/manage**")
    assert page.clicks.count(DY_PUBLISH) == 1 and marked == [1]
    assert info.value.after_submit and info.value.screenshot


async def test_douyin_reclick_only_when_click_had_no_effect(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import StepTimeout

    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                         hooks=_fast_hooks(tmp_path, reclick_gap=0.3, max_publish_clicks=2))
    page = FakePage({DY_PUBLISH: True})  # 点击没有任何反应（没请求、不跳转）
    with pytest.raises(StepTimeout):
        await note._publish_until_success(page, "图文", "**/manage**")
    assert page.clicks.count(DY_PUBLISH) == 2  # 隔一段时间补点一次，且不超过上限


async def test_douyin_success_receipt_records_item_id(sau, tmp_path: Path):
    _, dy, _ = sau
    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=_fast_hooks(tmp_path))
    page = FakePage({DY_PUBLISH: True})

    def submit(p: FakePage) -> None:
        req = FakeRequest(DY_CREATE)
        p.emit("request", req)
        p.emit("request", FakeRequest("https://creator.douyin.com/web/api/media/aweme/other/"))  # 无关请求
        p.emit("response", FakeResponse(req, {"status_code": 0, "item_id": "7400000000000000001"}))
        p.succeeded = True

    page.on_click[DY_PUBLISH] = submit
    await note._publish_until_success(page, "图文", "**/manage**")
    assert note.published_item_id == "7400000000000000001"
    assert note.publish_guard.succeeded and note.publish_guard.inflight == 0
    assert page.clicks.count(DY_PUBLISH) == 1


async def test_douyin_explicit_rejection_fails_fast(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import PublishRejected

    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                         hooks=_fast_hooks(tmp_path, publish_timeout=30))
    page = FakePage({DY_PUBLISH: True})

    def submit(p: FakePage) -> None:
        req = FakeRequest(DY_CREATE)
        p.emit("request", req)
        p.emit("response", FakeResponse(req, {"status_code": 8, "status_msg": "标题含违规内容"}))

    page.on_click[DY_PUBLISH] = submit
    with pytest.raises(PublishRejected) as info:
        await note._publish_until_success(page, "图文", "**/manage**")
    assert "标题含违规内容" in str(info.value) and info.value.screenshot
    assert page.clicks.count(DY_PUBLISH) == 1


async def test_douyin_sms_after_click_allows_one_reclick(sau, tmp_path: Path):
    conf, dy, _ = sau
    sms_input = dy.DouYinBaseUploader.SMS_INPUT_SELECTOR
    verify_btn = 'div.uc-ui-verify_sms-verify_button:has-text("验证")'
    asked: list[Path] = []

    def user_writes_code(path: Path) -> None:
        asked.append(path)
        path.write_text("123456", encoding="utf-8")

    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json",
                           hooks=_fast_hooks(tmp_path, publish_timeout=10, sms_code_required=user_writes_code))
    page = FakePage({DY_PUBLISH: True, verify_btn: True})
    publish_clicks = [0]

    def on_publish(p: FakePage) -> None:
        publish_clicks[0] += 1
        if publish_clicks[0] == 1:  # 第一次点击 → 弹短信验证
            p.visible.update({sms_input: True, "text=获取验证码": True})
        else:
            p.succeeded = True

    def on_verify(p: FakePage) -> None:
        p.visible[sms_input] = False
        p.visible["text=获取验证码"] = False

    page.on_click.update({DY_PUBLISH: on_publish, verify_btn: on_verify})
    await video._publish_until_success(page, "视频", "**/manage**")
    assert publish_clicks[0] == 2 and asked == [conf.verify_code_file()]
    assert page.filled[sms_input] == "123456" and video._sms_rounds == 1


async def test_douyin_sms_rounds_are_bounded(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import SmsVerifyFailed

    sms_input = dy.DouYinBaseUploader.SMS_INPUT_SELECTOR
    asked: list[Path] = []

    def user_writes_code(path: Path) -> None:
        asked.append(path)
        path.write_text("000000", encoding="utf-8")

    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json",
                           hooks=_fast_hooks(tmp_path, publish_timeout=10, sms_code_required=user_writes_code))
    page = FakePage({DY_PUBLISH: True, sms_input: True, "text=验证": True})  # 验证码一直不通过
    with pytest.raises(SmsVerifyFailed) as info:
        await video._publish_until_success(page, "视频", "**/manage**")
    assert len(asked) == video.MAX_SMS_ROUNDS and info.value.screenshot
    assert page.clicks.count(DY_PUBLISH) <= video.hooks.max_publish_clicks


async def test_xhs_no_reclick_while_request_in_flight_and_note_id(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import StepTimeout

    hooks = _fast_hooks(tmp_path, platform="xiaohongshu")
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=hooks)
    page = FakePage({XHS_PUBLISH: True})
    page.on_click[XHS_PUBLISH] = lambda p: p.emit("request", FakeRequest(XHS_CREATE))
    with pytest.raises(StepTimeout) as info:
        await note._publish_until_success(page, "图文")
    assert page.clicks.count(XHS_PUBLISH) == 1 and info.value.after_submit

    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=_fast_hooks(tmp_path))
    page = FakePage({XHS_PUBLISH: True})

    def submit(p: FakePage) -> None:
        req = FakeRequest(XHS_CREATE)
        p.emit("request", req)
        p.emit("response", FakeResponse(req, {"success": True, "code": 0, "data": {"id": "64f0c0ffee0000000000abcd"}}))
        p.succeeded = True

    page.on_click[XHS_PUBLISH] = submit
    await note._publish_until_success(page, "图文")
    assert note.published_note_id == "64f0c0ffee0000000000abcd"


async def test_xhs_ai_declaration_failure_is_reported(sau, tmp_path: Path):
    _, _, xhs = sau
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", ai_generated=True)
    await note.apply_declarations(FakePage())  # 页面上没有「添加内容类型声明」入口
    assert note.declaration_warnings and xhs.XHS_AI_DECLARATION in note.declaration_warnings[0]
    plain = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题")
    page = FakePage({"text=添加内容类型声明": True})
    await plain.apply_declarations(page)  # 默认：不碰任何声明
    assert page.clicks == [] and plain.declaration_warnings == []


async def test_cleanup_helpers_never_raise():
    from multipub._sau.runtime import close_quietly, save_state_quietly

    class Broken:
        async def storage_state(self, path: str) -> None:
            raise RuntimeError("context closed")

        async def close(self) -> None:
            raise RuntimeError("browser crashed")

    assert await save_state_quietly(Broken(), "x.json") is False
    await close_quietly(Broken(), None, Broken())


async def test_douyin_qrcode_fallback_screenshot(sau, tmp_path: Path):
    _, dy, _ = sau
    reported: list[dict[str, Any]] = []
    info = await dy._save_douyin_qrcode(FakePage(), str(tmp_path / "acct.json"), qrcode_callback=reported.append)
    assert info["fallback"] and Path(info["image_path"]).is_file()
    assert reported == [info]


def test_login_qrcode_without_opencv(monkeypatch, tmp_path: Path, capsys):
    from multipub._sau.utils import login_qrcode

    monkeypatch.setattr(login_qrcode, "cv2", None)
    assert not login_qrcode.terminal_qr_available()
    assert login_qrcode.decode_qrcode_from_path(tmp_path / "nope.png") is None
    login_qrcode.print_terminal_qrcode("hello", tmp_path / "qr.png", "小红书APP")
    out = capsys.readouterr()
    assert out.out == "" and "小红书APP" in out.err


# ------------------------------------------------------------------ validate()

def _future(hours: float = 3) -> datetime:
    return (datetime.now() + timedelta(hours=hours)).replace(second=0, microsecond=0)


def _levels(issues, field: str) -> set[str]:
    return {i.level for i in issues if i.field == field}


def dy_video(media: Path, **kw: Any) -> PlatformPost:
    base = dict(platform="douyin", account="main", kind=Kind.VIDEO, title="标题", body="简介",
                tags=["科技"], video=media / "video.mp4", source=str(media / "post.yaml"))
    base.update(kw)
    return PlatformPost(**base)


def dy_note(media: Path, **kw: Any) -> PlatformPost:
    base = dict(platform="douyin", account="main", kind=Kind.NOTE, title="标题", body="正文",
                images=[media / "images" / "1.jpg"], source=str(media / "post.yaml"))
    base.update(kw)
    return PlatformPost(**base)


def test_douyin_validate_ok(media: Path):
    assert DouyinAdapter.validate(dy_video(media, schedule=_future(5))) == []
    assert DouyinAdapter.validate(dy_note(media, options={"bgm": "晴天"})) == []


def test_douyin_validate_video_rules(media: Path):
    issues = DouyinAdapter.validate(dy_video(
        media, title="长" * 31, video=media / "clip.txt", tags=["a b", "c"],
        schedule=_future(1),
        options={"thumbnail_portrait": "missing.jpg", "sync_toutiao": "yes", "product_link": "https://x",
                 "typo_option": 1, "declaration": True},
    ))
    assert _levels(issues, "title") == {"error"}
    assert _levels(issues, "video") == {"error"}
    assert _levels(issues, "schedule") == {"error"}
    assert _levels(issues, "tags") == {"warning"}
    assert _levels(issues, "options.thumbnail_portrait") == {"error"}
    assert any(str(media / "missing.jpg") in i.message for i in issues)  # 相对 post.yaml 解析
    assert _levels(issues, "options.sync_toutiao") == {"error"}
    assert _levels(issues, "options.product_link") == {"warning"}
    assert _levels(issues, "options.typo_option") == {"warning"}
    assert _levels(issues, "options.declaration") == {"error"}

    far = DouyinAdapter.validate(dy_video(media, schedule=_future(24 * 20), title=""))
    assert _levels(far, "schedule") == {"warning"} and _levels(far, "title") == {"error"}
    near = DouyinAdapter.validate(dy_video(media, schedule=_future(2.1)))
    assert _levels(near, "schedule") == {"warning"}
    both = DouyinAdapter.validate(dy_video(media, cover=media / "cover.jpg",
                                           options={"thumbnail_portrait": "images/1.jpg"}))
    assert _levels(both, "cover") == {"warning"}  # cover 被 thumbnail_* 覆盖


def test_douyin_validate_note_rules(media: Path):
    issues = DouyinAdapter.validate(dy_note(
        media, title="长" * 21, body="字" * 1001, images=[media / "a.gif"] * 36, ai_generated=True,
        options={"declaration": "内容由AI生成"},
    ))
    assert _levels(issues, "title") == {"error"}
    assert _levels(issues, "body") == {"error"}
    assert _levels(issues, "images") == {"error"}
    assert _levels(issues, "ai_generated") == {"warning"}
    assert _levels(issues, "options.declaration") == {"warning"}  # 图文不支持，忽略
    assert DouyinAdapter.validate(dy_note(media, images=[])) and \
        _levels(DouyinAdapter.validate(dy_note(media, images=[])), "images") == {"error"}


def xhs_post(media: Path, kind: Kind = Kind.NOTE, **kw: Any) -> PlatformPost:
    base = dict(platform="xiaohongshu", account="main", kind=kind, title="标题", body="正文",
                tags=["徒步"], source=str(media / "post.yaml"))
    if kind is Kind.NOTE:
        base["images"] = [media / "images" / "1.jpg"]
    else:
        base["video"] = media / "video.mp4"
    base.update(kw)
    return PlatformPost(**base)


def test_xiaohongshu_validate_rules(media: Path):
    assert XiaohongshuAdapter.validate(xhs_post(media)) == []
    assert XiaohongshuAdapter.validate(xhs_post(media, Kind.VIDEO, cover=media / "cover.jpg")) == []
    issues = XiaohongshuAdapter.validate(xhs_post(
        media, title="长" * 21, body="字" * 1001, tags=[f"t{i}" for i in range(11)],
        images=[media / "images" / "1.jpg"] * 19, schedule=_future(1.5),
    ))
    for field in ("title", "body", "tags", "images", "schedule"):
        assert _levels(issues, field) == {"error"}, field
    no_title = XiaohongshuAdapter.validate(xhs_post(media, title=""))
    assert _levels(no_title, "title") == {"warning"}
    nothing = XiaohongshuAdapter.validate(xhs_post(media, title="", body=""))
    assert _levels(nothing, "title") == {"error"}  # 没标题也没正文，无法兜底
    bmp = XiaohongshuAdapter.validate(xhs_post(media, images=[media / "a.bmp"]))
    assert _levels(bmp, "images") == {"warning"}
    no_title_video = XiaohongshuAdapter.validate(xhs_post(media, Kind.VIDEO, title=""))
    assert _levels(no_title_video, "title") == {"error"}
    both = XiaohongshuAdapter.validate(xhs_post(media, ai_generated=True, options={"repost_source": "某媒体"}))
    assert _levels(both, "options.repost_source") == {"warning"}
    article = XiaohongshuAdapter.validate(xhs_post(media, kind=Kind.ARTICLE))
    assert _levels(article, "kind") == {"error"}


# ------------------------------------------------------------------ 参数映射

class Recorder:
    """替代 SAU 的上传类：记录构造参数，模拟一次成功的发布。"""

    instances: list["Recorder"] = []
    item_id: str | None = "7400000000000000001"
    raise_exc: BaseException | None = None

    note_id: str | None = None
    warnings: list[str] = []

    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.hooks = kwargs["hooks"]
        self.published_item_id = None
        self.published_note_id = None
        self.declaration_warnings: list[str] = []
        Recorder.instances.append(self)

    async def _run(self) -> None:
        self.hooks.call("before_publish_click")
        if Recorder.raise_exc is not None:
            raise Recorder.raise_exc
        self.published_item_id = Recorder.item_id
        self.published_note_id = Recorder.note_id
        self.declaration_warnings = list(Recorder.warnings)

    douyin_upload_video = douyin_upload_note = main = _run


@pytest.fixture
def recorder(sau, monkeypatch):
    _, dy, xhs = sau
    Recorder.instances, Recorder.raise_exc, Recorder.item_id = [], None, "7400000000000000001"
    Recorder.note_id, Recorder.warnings = None, []
    for mod, names in ((dy, ("DouYinVideo", "DouYinNote")), (xhs, ("XiaoHongShuVideo", "XiaoHongShuNote"))):
        for name in names:
            monkeypatch.setattr(mod, name, type(name, (Recorder,), {}))
    return Recorder


async def test_douyin_video_option_mapping(settings, media: Path, tmp_path: Path, recorder):
    (media / "portrait.png").write_bytes((media / "cover.jpg").read_bytes())
    ctx, events = make_ctx(settings, "douyin", tmp_path)
    login_file(ctx)
    post = dy_video(media, ai_generated=True, cover=media / "cover.jpg",
                    options={"collection": "合集A", "product_link": "https://p", "product_title": "商品"})
    result = await DouyinAdapter(ctx).publish(post)
    kw = recorder.instances[-1].kwargs
    assert type(recorder.instances[-1]).__name__ == "DouYinVideo"
    assert kw["title"] == "标题" and kw["desc"] == "简介" and kw["tags"] == ["科技"]
    assert kw["file_path"] == str(media / "video.mp4")
    assert kw["publish_date"] == 0 and kw["publish_strategy"] == "immediate"
    assert kw["account_file"] == str(settings.account_file("douyin", "main"))
    assert kw["declaration"] == "内容由AI生成"  # ai_generated 且未显式声明
    assert kw["sync_toutiao"] is False
    assert kw["collection_name"] == "合集A" and kw["productLink"] == "https://p" and kw["productTitle"] == "商品"
    # cover.jpg 是 1280x720 横图 → 横封面
    assert kw["thumbnail_landscape_path"] == str(media / "cover.jpg") and kw["thumbnail_portrait_path"] is None
    assert kw["headless"] is True and kw["debug"] is False
    hooks = kw["hooks"]
    assert hooks.evidence_dir == ctx.run_dir and hooks.platform == "douyin"
    assert hooks.upload_timeout == settings.timeout_for("publish_video") - 600

    assert result.status is Status.SUBMITTED and result.submit_started
    assert result.item_id == "7400000000000000001"
    assert result.url == "https://www.douyin.com/video/7400000000000000001"
    assert any(e["event"] == "submit_started" for e in events)

    # 显式参数：相对 post.yaml 的竖封面、关闭声明、同步头条、定时
    when = _future(5)
    recorder.item_id = None
    post = dy_video(media, schedule=when, ai_generated=True,
                    options={"thumbnail_portrait": "portrait.png", "declaration": False, "sync_toutiao": True})
    result = await DouyinAdapter(ctx).publish(post)
    kw = recorder.instances[-1].kwargs
    assert kw["thumbnail_portrait_path"] == str((media / "portrait.png").resolve())
    assert kw["thumbnail_landscape_path"] is None
    assert kw["declaration"] is None and kw["sync_toutiao"] is True
    assert kw["publish_date"] == when and kw["publish_strategy"] == "scheduled"
    assert result.status is Status.SCHEDULED and result.url is None and "作品管理" in result.message


async def test_douyin_note_option_mapping(settings, media: Path, tmp_path: Path, recorder):
    ctx, _ = make_ctx(settings, "douyin", tmp_path)
    login_file(ctx)
    imgs = [media / "images" / f"{i}.jpg" for i in (1, 2, 3)]
    result = await DouyinAdapter(ctx).publish(dy_note(media, images=imgs, tags=["徒步"], options={"bgm": "晴天"}))
    kw = recorder.instances[-1].kwargs
    assert type(recorder.instances[-1]).__name__ == "DouYinNote"
    assert kw["image_paths"] == [str(p) for p in imgs]
    assert kw["note"] == "正文" and kw["title"] == "标题" and kw["bgm"] == "晴天" and kw["tags"] == ["徒步"]
    assert kw["hooks"].upload_timeout == max(180, settings.timeout_for("publish_note") - 600)
    assert result.url == "https://www.douyin.com/note/7400000000000000001"


async def test_xiaohongshu_option_mapping(settings, media: Path, tmp_path: Path, recorder):
    ctx, _ = make_ctx(settings, "xiaohongshu", tmp_path)
    login_file(ctx)
    post = xhs_post(media, Kind.VIDEO, cover=media / "cover.jpg", ai_generated=True,
                    options={"repost_source": "某媒体"})
    result = await XiaohongshuAdapter(ctx).publish(post)
    kw = recorder.instances[-1].kwargs
    assert type(recorder.instances[-1]).__name__ == "XiaoHongShuVideo"
    assert kw["thumbnail_path"] == str(media / "cover.jpg") and kw["desc"] == "正文"
    assert kw["ai_generated"] is True and kw["repost_source"] == "某媒体"
    assert result.status is Status.SUBMITTED and result.url is None and result.submit_started

    when = _future(6)
    result = await XiaohongshuAdapter(ctx).publish(xhs_post(media, title="", schedule=when))
    kw = recorder.instances[-1].kwargs
    assert type(recorder.instances[-1]).__name__ == "XiaoHongShuNote"
    assert kw["title"] is None and kw["note"] == "正文" and kw["repost_source"] is None
    assert kw["ai_generated"] is False
    assert kw["publish_strategy"] == "scheduled" and kw["publish_date"] == when
    assert result.status is Status.SCHEDULED and result.item_id is None

    # 拿到笔记 ID、AI 声明没选上：都写进结果
    recorder.note_id, recorder.warnings = "64f0c0ffee0000000000abcd", ["未能自动选择「笔记含AI合成内容」声明"]
    result = await XiaohongshuAdapter(ctx).publish(xhs_post(media, ai_generated=True))
    assert result.item_id == "64f0c0ffee0000000000abcd" and result.url is None
    assert "64f0c0ffee0000000000abcd" in result.message and "笔记含AI合成内容" in result.message


async def test_publish_prechecks_before_browser(settings, media: Path, tmp_path: Path, recorder):
    ctx, _ = make_ctx(settings, "douyin", tmp_path)
    with pytest.raises(LoginRequired) as info:
        await DouyinAdapter(ctx).publish(dy_video(media))
    assert "mpub login douyin --account main" in str(info.value)
    login_file(ctx)
    with pytest.raises(ValidationFailed):
        await DouyinAdapter(ctx).publish(dy_video(media, title="长" * 31))
    assert recorder.instances == []


@pytest.mark.parametrize("exc_name,after,expected", [
    ("StepTimeout", False, ("failed", ErrorCode.TIMEOUT, True)),
    ("StepTimeout", True, ("failed", ErrorCode.UNKNOWN_OUTCOME, False)),
    ("SmsCodeTimeout", True, ("needs_action", ErrorCode.SMS_CODE_REQUIRED, False)),
    ("CaptchaRequired", False, ("needs_action", ErrorCode.CAPTCHA, False)),
    ("LoginPageDetected", False, ("needs_action", ErrorCode.LOGIN_REQUIRED, False)),
    ("LoginPageDetected", True, ("failed", ErrorCode.UNKNOWN_OUTCOME, False)),
    ("CookieInvalid", False, ("needs_action", ErrorCode.LOGIN_REQUIRED, False)),
    ("UploadFailed", False, ("failed", ErrorCode.PLATFORM_ERROR, True)),
    ("PublishRejected", True, ("failed", ErrorCode.PLATFORM_ERROR, False)),
    ("DeclarationFailed", False, ("failed", ErrorCode.VALIDATION, False)),
    ("SmsVerifyFailed", True, ("needs_action", ErrorCode.SMS_CODE_REQUIRED, False)),
])
async def test_error_translation(settings, tmp_path: Path, exc_name, after, expected):
    from multipub._sau import runtime as rt

    ctx, _ = make_ctx(settings, "douyin", tmp_path)
    if after:
        ctx.mark_submit_started()
    cls = getattr(rt, exc_name)
    exc = cls("卡住了", step="x", after_submit=after) if issubclass(cls, rt.StepTimeout) else cls("卡住了")
    with pytest.raises(PublishError) as info:
        async with bridge.translate_errors(ctx):
            raise exc
    err = info.value
    assert (err.status.value, err.code, err.retryable) == expected


async def test_error_translation_validation_and_passthrough(settings, tmp_path: Path):
    ctx, _ = make_ctx(settings, "douyin", tmp_path)
    with pytest.raises(ValidationFailed):
        async with bridge.translate_errors(ctx):
            raise ValueError("标题不能超过20字符")
    with pytest.raises(RuntimeError):  # 其他异常交给 worker（INTERNAL / UNKNOWN_OUTCOME）
        async with bridge.translate_errors(ctx):
            raise RuntimeError("自主声明设置失败")
    ctx.mark_submit_started()
    with pytest.raises(ValueError):  # 已点发布后不再当成校验错误
        async with bridge.translate_errors(ctx):
            raise ValueError("x")


async def test_publish_after_submit_timeout_reports_unknown_outcome(settings, media, tmp_path, recorder):
    from multipub._sau.runtime import StepTimeout

    ctx, _ = make_ctx(settings, "xiaohongshu", tmp_path)
    login_file(ctx)
    recorder.raise_exc = StepTimeout("等待图文发布成功 超过 240 秒", step="x", after_submit=True)
    with pytest.raises(PublishError) as info:
        await XiaohongshuAdapter(ctx).publish(xhs_post(media))
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME and ctx.submit_started


# ------------------------------------------------------------------ login / check / 杂项

async def test_login_and_check_flow(settings, tmp_path: Path, sau, monkeypatch):
    _, dy, xhs = sau
    ctx, events = make_ctx(settings, "douyin", tmp_path, action="login")
    qr_src = tmp_path / "sau-qr.png"
    qr_src.write_bytes(b"\x89PNG qr")
    seen: dict[str, Any] = {}

    async def fake_setup(account_file, handle, return_detail, qrcode_callback, headless, login_timeout):
        seen.update(handle=handle, headless=headless, login_timeout=login_timeout)
        qrcode_callback({"image_path": str(qr_src), "image_data_url": ""})
        Path(account_file).write_text("{}", encoding="utf-8")
        return {"success": True, "status": "success", "message": "ok"}

    monkeypatch.setattr(dy, "douyin_setup", fake_setup)
    result = await DouyinAdapter(ctx).login()
    assert result.status is Status.OK and ctx.account_file.is_file()
    assert seen == {"handle": True, "headless": True, "login_timeout": settings.timeout_for("login") - 90}
    qr_events = [e for e in events if e["event"] == "qrcode"]
    assert qr_events and qr_events[0]["path"] == str(settings.qrcode_dir / "douyin-main.png")
    assert not (settings.qrcode_dir / "douyin-main.png").exists()  # 登录结束后清理

    async def timeout_setup(*args, **kwargs):
        return {"success": False, "status": "timeout", "message": "超时"}

    monkeypatch.setattr(xhs, "xiaohongshu_setup", timeout_setup)
    xctx, _ = make_ctx(settings, "xiaohongshu", tmp_path, action="login")
    with pytest.raises(PublishError) as info:
        await XiaohongshuAdapter(xctx).login()
    assert info.value.code is ErrorCode.TIMEOUT
    assert "没有拿到二维码图片" in str(info.value) and "--headless" in str(info.value)

    # check
    cctx, _ = make_ctx(settings, "xiaohongshu", tmp_path, action="check")
    with pytest.raises(LoginRequired):
        await XiaohongshuAdapter(cctx).check()
    login_file(cctx)
    valid = {"ok": False}

    async def fake_auth(path: str) -> bool:
        return valid["ok"]

    monkeypatch.setattr(xhs, "cookie_auth", fake_auth)
    with pytest.raises(LoginRequired) as info:
        await XiaohongshuAdapter(cctx).check()
    assert "mpub login xiaohongshu --account main" in str(info.value)
    valid["ok"] = True
    assert (await XiaohongshuAdapter(cctx).check()).status is Status.OK


def test_qrcode_callback_data_url(settings, tmp_path: Path):
    import base64

    ctx, events = make_ctx(settings, "xiaohongshu", tmp_path, action="login", account="中文号")
    callback, dest = bridge.qrcode_callback(ctx)
    callback({"image_path": "", "image_data_url": "data:image/png;base64," + base64.b64encode(b"PNGDATA").decode()})
    assert dest.read_bytes() == b"PNGDATA" and events[-1]["event"] == "qrcode"
    n = len(events)
    callback({"image_path": "", "image_data_url": ""})  # 没定位到二维码：不上报
    assert len(events) == n


def test_sau_log_forwarding(settings, tmp_path: Path, capsys):
    ctx, events = make_ctx(settings, "douyin", tmp_path)
    with bridge.sau_session(ctx, "douyin"):
        from multipub._sau.utils.log import douyin_logger, xiaohongshu_logger

        douyin_logger.info("🏃 小人正在努力上传视频")  # INFO 不转发
        douyin_logger.success("🥳 视频已经传完啦")
        douyin_logger.warning("😵 话题未出现候选")
        xiaohongshu_logger.warning("别的平台")
    douyin_logger.success("会话结束后不再转发")
    logs = [(e["level"], e["message"]) for e in events if e["event"] == "log"]
    assert logs == [("info", "视频已经传完啦"), ("warning", "话题未出现候选")]
    assert capsys.readouterr().out == ""
    log_file = settings.sau_home / "logs" / "douyin.log"
    assert "视频已经传完啦" in log_file.read_text(encoding="utf-8")


def test_doctor_and_cover_slots(settings, media: Path):
    items = {i["name"]: i for i in DouyinAdapter.doctor(settings)}
    assert items["terminal_qr"]["ok"] and items["stealth_js"]["ok"]
    assert {i["name"] for i in XiaohongshuAdapter.doctor(settings)} == {"terminal_qr", "stealth_js"}

    portrait_post = dy_video(media, cover=media / "images" / "1.jpg")  # 1080x1440
    assert cover_slots(portrait_post) == (media / "images" / "1.jpg", None)
    assert resolve_declaration(dy_video(media)) is None
    assert resolve_declaration(dy_video(media, options={"declaration": "内容为个人观点或见解"})) == "内容为个人观点或见解"
    assert resolve_declaration(dy_video(media, ai_generated=True, options={"declaration": "none"})) is None


def test_worker_json_channel_with_douyin_validation_failure(home: Path, media: Path):
    """经由真实 worker 子进程：预校验失败也只在 stdout 输出 JSON 事件。"""
    from multipub.runner import run_worker

    settings = load_settings()
    settings.ensure_dirs()
    acct = settings.account_file("douyin", "main")
    acct.parent.mkdir(parents=True, exist_ok=True)
    acct.write_text("{}", encoding="utf-8")
    post = dy_video(media, title="长" * 31)
    r = run_worker(settings, action="publish", platform="douyin", account="main", headless=True,
                   run_dir=settings.runs_dir / "t", post=post, timeout=60)
    assert r.status is Status.FAILED and r.error_code == "validation" and not r.submit_started
    json.dumps(r.to_dict())
