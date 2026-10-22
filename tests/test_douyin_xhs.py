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
                             PublishError, Result, Status, ValidationFailed)
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
    """按选择器字符串平铺查找：子定位器（locator / filter / nth）只看最后一级选择器。"""

    def __init__(self, page: "FakePage", key: str):
        self.page, self.key = page, key

    @property
    def first(self) -> "FakeLocator":
        return self

    last = first

    def nth(self, index: int) -> "FakeLocator":
        return self

    def filter(self, **_: Any) -> "FakeLocator":
        return self

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self.page, selector)

    def get_by_text(self, text: str, exact: bool = False) -> "FakeLocator":
        return FakeLocator(self.page, f"text={text}")

    def get_by_role(self, role: str, name: str = "", exact: bool = False) -> "FakeLocator":
        return FakeLocator(self.page, f"role={role}:{name}")

    async def count(self) -> int:
        return self.page.counts.get(self.key, 1) if self.page.visible.get(self.key) else 0

    async def is_visible(self) -> bool:
        return bool(self.page.visible.get(self.key))

    async def is_enabled(self) -> bool:
        return self.key not in self.page.disabled

    async def is_checked(self) -> bool:
        return self.page.attrs.get((self.key, "checked")) is not None

    async def click(self, trial: bool = False, **_: Any) -> None:
        if not self.page.visible.get(self.key):
            raise RuntimeError(f"找不到元素 {self.key}")
        if trial:  # 只做可点性检查，不点击
            return
        self.page.clicks.append(self.key)
        self.page.focused = self.key
        hook = self.page.on_click.get(self.key)
        if hook is not None:
            hook(self.page)

    async def fill(self, value: str, **_: Any) -> None:
        if self.key not in self.page.readonly:
            self.page.filled[self.key] = value

    async def input_value(self, **_: Any) -> str:
        return self.page.filled.get(self.key, self.page.attrs.get((self.key, "value"), ""))

    async def press(self, key: str, **_: Any) -> None:
        self.page.keyboard.pressed.append(key)

    async def screenshot(self, path: str, **_: Any) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    async def set_input_files(self, files: Any, **_: Any) -> None:
        self.page.uploads.append(self.key)

    async def wait_for(self, state: str = "visible", timeout: float | None = None) -> None:
        shown = bool(self.page.visible.get(self.key))
        if shown != (state in ("visible", "attached")):
            raise RuntimeError(f"等不到元素 {self.key} 变成 {state}")

    async def get_attribute(self, name: str) -> str | None:
        return self.page.attrs.get((self.key, name))

    async def evaluate(self, *args: Any) -> Any:
        self.page.evaluated.append(self.key)
        return self.page.attrs.get((self.key, "evaluate"))

    async def element_handle(self) -> "FakeLocator":
        return self

    async def scroll_into_view_if_needed(self, **_: Any) -> None:
        return None

    async def bounding_box(self) -> None:
        return None  # _native_click 退回到 locator.click()


class FakeRequest:
    def __init__(self, url: str, method: str = "POST"):
        self.url, self.method = url, method


class FakeResponse:
    def __init__(self, request: FakeRequest, body: Any, status: int = 200):
        self.request, self.url, self.body, self.status = request, request.url, body, status

    async def json(self) -> Any:
        return self.body


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def type(self, text: str, **_: Any) -> None:  # 输入到最后点过的元素（Ctrl+A 后整体替换）
        key = self.page.focused
        if key is not None and key not in self.page.readonly:
            self.page.filled[key] = text


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
        self.uploads: list[str] = []
        self.counts: dict[str, int] = {}  # 选择器命中多个元素
        self.disabled: set[str] = set()
        self.attrs: dict[tuple[str, str], Any] = {}  # (选择器, 属性) → 值
        self.shown_texts: set[str] = set()  # find_visible_text 能看到的文字（输入区以外）
        self.readonly: set[str] = set()  # 输入被组件吞掉的输入框
        self.focused: str | None = None
        self.evaluated: list[str] = []
        self.keyboard = FakeKeyboard(self)
        self.url = "https://creator.example/publish"

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, f"text={text}")

    def get_by_role(self, role: str, name: str = "", exact: bool = False) -> FakeLocator:
        return FakeLocator(self, f"role={role}:{name}")

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_placeholder(self, text: str) -> FakeLocator:
        return FakeLocator(self, f"placeholder={text}")

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event: str, arg: Any) -> None:
        for callback in self.listeners.get(event, []):
            callback(arg)

    async def evaluate(self, script: str = "", arg: Any = None) -> Any:
        if isinstance(arg, dict) and "texts" in arg:  # runtime.find_visible_text
            for want in arg["texts"]:
                for shown in sorted(self.shown_texts):
                    if (shown == want) if arg["exact"] else (want in shown):
                        return shown
            return ""
        return None

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def goto(self, url: str, **_: Any) -> None:
        self.url = url

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
    omnipost = (SAU_DIR / "LICENSE-omnipost").read_text(encoding="utf-8")  # 移植了 omnipost 的函数
    assert "Copyright (c) 2026 contributors of this repository" in omnipost and "LICENSE-omnipost" in text
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
    # b. 头条/西瓜开关只按显式的 sync_toutiao 设置（没写时不碰）
    assert "self.sync_toutiao = None if sync_toutiao is None else bool(sync_toutiao)" in dy_src
    assert "if self.sync_toutiao is None:\n            return" in dy_src
    assert "third_part_element" not in dy_src  # 上游无条件打开开关的那段已删除
    # c. 来源转载只在 apply_declarations 里有条件调用，设置不上就拒绝发布
    assert xhs_src.count("await self.check_original_declaration(page)") == 1
    assert "if self.repost_source and not await self.check_original_declaration(page):" in xhs_src
    # d. 抖音视频：等发布表单与等上传完成共用 upload_timeout（不再固定等 120 秒）
    steps = dy_src[dy_src.index("async def _upload_video_steps"):]
    assert steps.index("await self._wait_for_publish_form(page, deadline)") < steps.index(
        "await self.fill_title_and_description(")
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
    assert v.declaration is None and v.sync_toutiao is None
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


async def test_douyin_covered_publish_button_is_not_clicked(sau, tmp_path: Path):
    """「发布」被弹窗 / 遮罩盖住：force 点击会落在遮罩上，不能记作已点击（以前最后报 unknown_outcome）。"""
    _, dy, _ = sau
    from multipub._sau.runtime import StepTimeout

    marked: list[int] = []
    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json",
                           hooks=_fast_hooks(tmp_path, before_publish_click=lambda: marked.append(1)))
    page = FakePage({DY_PUBLISH: True})
    page.attrs[(DY_PUBLISH, "evaluate")] = "semi-modal-mask"  # 命中测试：中心点上是遮罩
    with pytest.raises(StepTimeout) as info:
        await video._publish_until_success(page, "视频", "**/manage**")
    assert DY_PUBLISH not in page.clicks and marked == [] and video.publish_guard.clicks == 0
    assert not info.value.after_submit and "没有点击" in str(info.value)

    # 弹窗关掉之后照常点击
    page.attrs[(DY_PUBLISH, "evaluate")] = ""
    page.on_click[DY_PUBLISH] = lambda p: setattr(p, "succeeded", True)
    await video._publish_until_success(page, "视频", "**/manage**")
    assert page.clicks.count(DY_PUBLISH) == 1 and marked == [1]


async def test_douyin_success_receipt_without_redirect_is_success(sau, tmp_path: Path):
    _, dy, _ = sau
    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                         hooks=_fast_hooks(tmp_path, publish_timeout=30, receipt_grace=0.2))
    page = FakePage({DY_PUBLISH: True})

    def submit(p: FakePage) -> None:  # 回执成功，但页面一直不跳转
        req = FakeRequest(DY_CREATE)
        p.emit("request", req)
        p.emit("response", FakeResponse(req, {"status_code": 0, "item_id": "7400000000000000002"}))

    page.on_click[DY_PUBLISH] = submit
    await note._publish_until_success(page, "图文", "**/manage**")
    assert note.published_item_id == "7400000000000000002" and page.clicks.count(DY_PUBLISH) == 1


async def test_douyin_gateway_error_detail(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import StepTimeout

    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=_fast_hooks(tmp_path))
    page = FakePage({DY_PUBLISH: True})

    class HtmlResponse(FakeResponse):
        async def json(self) -> Any:
            raise ValueError("not json")

    def submit(p: FakePage) -> None:
        req = FakeRequest(DY_CREATE)
        p.emit("request", req)
        p.emit("response", HtmlResponse(req, "<html>Bad Gateway</html>", status=502))

    page.on_click[DY_PUBLISH] = submit
    with pytest.raises(StepTimeout) as info:
        await note._publish_until_success(page, "图文", "**/manage**")
    assert info.value.after_submit and "HTTP 502" in str(info.value)
    assert page.clicks.count(DY_PUBLISH) == 1  # 结果不明：不补点


async def test_douyin_title_read_back(sau, tmp_path: Path):
    """网页按 UTF-16 截断标题（emoji 算 2 个字）：点「发布」之前拒绝，而不是发出被截断的标题。"""
    _, dy, _ = sau
    from multipub._sau.runtime import DeclarationFailed

    title_sel, desc_sel = 'input[placeholder*="填写作品标题"]', 'div.zone-container[contenteditable="true"]'
    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=_fast_hooks(tmp_path))
    want = "一" * 29 + "😀"
    page = FakePage({title_sel: True, desc_sel: True})
    page.readonly.add(title_sel)
    page.attrs[(title_sel, "value")] = "一" * 29  # 页面上的值：emoji 被 maxlength 截掉了
    with pytest.raises(DeclarationFailed) as info:
        await video.fill_title_and_description(page, want, "简介", [])
    assert "UTF-16" in str(info.value) and "没有发出" in str(info.value)

    page = FakePage({title_sel: True, desc_sel: True})
    await video.fill_title_and_description(page, "正常标题", "第一行\n第二行", ["科技"])
    assert page.filled[title_sel] == "正常标题"


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


async def test_publish_click_waits_for_enabled_unique_button(sau, tmp_path: Path):
    """按钮不可用 / 不唯一时不点、不记账、不标记已提交（这样的点击不会生效）。"""
    import asyncio

    _, dy, xhs = sau
    from multipub._sau.runtime import StepTimeout

    # 小红书：按钮一开始不可用，过一会儿才可点 → 只点一次且成功；不会把次数花在点不动的按钮上
    marked: list[int] = []
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                               hooks=_fast_hooks(tmp_path, publish_timeout=5, reclick_gap=0.05,
                                                 before_publish_click=lambda: marked.append(1)))
    page = FakePage({XHS_PUBLISH: True}, success_after_click=XHS_PUBLISH)
    page.disabled.add(XHS_PUBLISH)

    async def enable_later() -> None:
        await asyncio.sleep(0.4)
        page.disabled.discard(XHS_PUBLISH)

    task = asyncio.ensure_future(enable_later())
    await note._publish_until_success(page, "图文")
    await task
    assert page.clicks.count(XHS_PUBLISH) == 1 and note.publish_guard.clicks == 1 and marked == [1]

    # 一直不可用：截止时间到按「发布前超时」处理（可重试），不是 unknown_outcome
    marked.clear()
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                               hooks=_fast_hooks(tmp_path, publish_timeout=0.3,
                                                 before_publish_click=lambda: marked.append(1)))
    page = FakePage({XHS_PUBLISH: True})
    page.disabled.add(XHS_PUBLISH)
    with pytest.raises(StepTimeout) as info:
        await note._publish_until_success(page, "图文")
    assert not info.value.after_submit and page.clicks == [] and marked == []

    # 子串选择器命中多个按钮：改点文字完全一致的那个；仍不唯一就不点
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", hooks=_fast_hooks(tmp_path))
    page = FakePage({XHS_PUBLISH: True, "button": True}, success_after_click="button")
    page.counts[XHS_PUBLISH] = 2
    await note._publish_until_success(page, "图文")
    assert page.clicks == ["button"]
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                               hooks=_fast_hooks(tmp_path, publish_timeout=0.3))
    page = FakePage({XHS_PUBLISH: True, "button": True})
    page.counts.update({XHS_PUBLISH: 2, "button": 2})
    with pytest.raises(StepTimeout) as info:
        await note._publish_until_success(page, "图文")
    assert not info.value.after_submit and page.clicks == [] and not note._publish_clicked

    # 抖音视频用 force 点击：disabled / aria-disabled 的按钮不点（force 点上去什么也不会发生）
    for attr in ("disabled", "aria-disabled"):
        video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=_fast_hooks(tmp_path, publish_timeout=0.3))
        page = FakePage({DY_PUBLISH: True})
        page.attrs[(DY_PUBLISH, attr)] = "true"
        with pytest.raises(StepTimeout) as info:
            await video._publish_until_success(page, "视频", "**/manage**")
        assert not info.value.after_submit and page.clicks == [] and video.publish_guard.clicks == 0


async def test_douyin_form_wait_uses_upload_deadline(sau, tmp_path: Path):
    """version_2 发布页要等视频传完才出表单：等表单用 upload_timeout，不再固定 120 秒。"""
    _, dy, _ = sau
    from multipub._sau.runtime import Deadline, StepTimeout, UploadFailed

    title = 'input[placeholder*="填写作品标题"]'
    failed = 'div.progress-div > div:has-text("上传失败")'
    hooks = _fast_hooks(tmp_path, poll_interval=0.01)
    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=hooks)
    await video._wait_for_publish_form(FakePage({title: True}), Deadline(5, "等待视频上传完成"))

    with pytest.raises(StepTimeout) as info:
        await video._wait_for_publish_form(FakePage(), Deadline(0.2, "等待视频上传完成"))
    assert info.value.step == "等待视频上传完成" and not info.value.after_submit

    # 表单出来之前检测到上传失败：照常重传，次数用完报 UploadFailed
    page = FakePage({failed: True})
    with pytest.raises(UploadFailed):
        await video._wait_for_publish_form(page, Deadline(5, "等待视频上传完成"))
    assert len(page.uploads) == video.MAX_UPLOAD_RETRIES


def _declaration_page(*, option_works: bool = True, confirm_closes: bool = True,
                      radio: bool = True, shown: bool = False) -> FakePage:
    dialog, option, confirm = ".semi-modal-content", "label.semi-radio", "button.semi-button-primary"
    page = FakePage({"text=请选择自主声明": True, dialog: True, confirm: True, "text=内容由AI生成": True})
    if radio:
        page.visible[option] = True
        page.attrs[(option, "class")] = "semi-radio"
    if option_works:
        page.on_click[option] = lambda p: p.attrs.__setitem__((option, "class"), "semi-radio semi-radio-checked")
    if confirm_closes:
        page.on_click[confirm] = lambda p: p.visible.__setitem__(dialog, False)
    if shown:
        page.shown_texts.add("内容由AI生成")
    return page


async def test_douyin_self_declaration_is_verified(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import DeclarationFailed

    def video() -> Any:
        return dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", declaration="内容由AI生成",
                              hooks=_fast_hooks(tmp_path))

    v = video()
    page = _declaration_page()
    assert await v.set_self_declaration(page, "内容由AI生成") is True
    assert page.clicks[-1] == "button.semi-button-primary"

    # 点击被组件吞掉（选项没变成选中）：返回 False，apply_self_declaration 拒绝发布并截图
    v = video()
    page = _declaration_page(option_works=False)
    assert await v.set_self_declaration(page, "内容由AI生成") is False
    assert "button.semi-button-primary" not in page.clicks
    with pytest.raises(DeclarationFailed) as info:
        await v.apply_self_declaration(page)
    assert "platforms.douyin.declaration" in str(info.value)
    assert (tmp_path / "douyin-declaration.png").is_file()

    # 选中了但点「确定」弹窗不关
    assert await video().set_self_declaration(_declaration_page(confirm_closes=False), "内容由AI生成") is False

    # 没有 label.semi-radio 结构：只能看关弹窗后入口处是否显示了所选声明
    assert await video().set_self_declaration(_declaration_page(radio=False), "内容由AI生成") is False
    assert await video().set_self_declaration(_declaration_page(radio=False, shown=True), "内容由AI生成") is True

    # 弹窗根本没打开
    assert await video().set_self_declaration(FakePage(), "内容由AI生成") is False


async def test_douyin_sync_toutiao_tristate(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import DeclarationFailed

    switch, native = dy.DouYinVideo.SYNC_SWITCH_SELECTOR, "input.semi-switch-native-control"

    def page_with(on: bool, *, works: bool = True, label: str = "同步到今日头条/西瓜视频") -> FakePage:
        page = FakePage({switch: True, native: True})
        page.attrs[(switch, "class")] = "semi-switch" + (" semi-switch-checked" if on else "")
        page.attrs[(switch, "evaluate")] = label
        if works:
            def toggle(p: FakePage) -> None:
                cls = p.attrs[(switch, "class")]
                p.attrs[(switch, "class")] = ("semi-switch" if "checked" in cls else "semi-switch semi-switch-checked")
            page.on_click[native] = toggle
        return page

    def video(sync: bool | None) -> Any:
        return dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", sync_toutiao=sync, hooks=_fast_hooks(tmp_path))

    page = page_with(True)
    await video(None).apply_sync_toutiao(page)  # 没写：不碰
    assert page.clicks == []

    page = page_with(True)
    await video(False).apply_sync_toutiao(page)  # 显式 false：把开着的开关关掉
    assert page.clicks == [native] and "checked" not in page.attrs[(switch, "class")]

    page = page_with(False)
    await video(False).apply_sync_toutiao(page)  # 本来就关着
    assert page.clicks == []

    with pytest.raises(DeclarationFailed) as info:  # 关不掉：拒绝发布（不能违背明确选择去同步）
        await video(False).apply_sync_toutiao(page_with(True, works=False))
    assert "sync_toutiao" in str(info.value)

    page = page_with(True, label="允许他人保存视频")  # 改版后选择器命中了别的开关：不动它
    await video(False).apply_sync_toutiao(page)
    assert page.clicks == []

    page = page_with(False)
    await video(True).apply_sync_toutiao(page)
    assert "semi-switch-checked" in page.attrs[(switch, "class")]
    await video(True).apply_sync_toutiao(page_with(False, works=False))  # 打不开只告警


def _repost_page(*, closes: bool = True, shown: bool = True, entry: bool = True) -> FakePage:
    source_input, confirm = "placeholder=请输入媒体名称", "role=button:确认"
    page = FakePage({"text=添加内容类型声明": entry, "#publish-container div": True,
                     source_input: True, confirm: True})

    def on_confirm(p: FakePage) -> None:
        if closes:
            p.visible[source_input] = False
        if shown:
            p.shown_texts.add("来源：某日报")

    page.on_click[confirm] = on_confirm
    return page


async def test_xhs_repost_declaration_verified_or_refused(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import DeclarationFailed

    def note() -> Any:
        return xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", repost_source="某日报",
                                   hooks=_fast_hooks(tmp_path, platform="xiaohongshu"))

    n = note()
    page = _repost_page()
    await n.apply_declarations(page)
    assert page.filled["placeholder=请输入媒体名称"] == "某日报" and n.declaration_warnings == []

    for bad in (_repost_page(entry=False), _repost_page(closes=False), _repost_page(shown=False)):
        n = note()
        with pytest.raises(DeclarationFailed) as info:  # 设置不上：拒绝发布，而不是当原创发出去
            await n.apply_declarations(bad)
        assert "repost_source" in str(info.value)
    assert (tmp_path / "xiaohongshu-declaration.png").is_file()
    assert await note().check_original_declaration(FakePage()) is False


async def test_xhs_ai_declaration_is_verified(sau, tmp_path: Path):
    _, _, xhs = sau
    option = f"text={xhs.XHS_AI_DECLARATION}"

    def note() -> Any:
        return xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", ai_generated=True)

    def page_with(closes: bool) -> FakePage:
        page = FakePage({"text=添加内容类型声明": True, option: True})

        def pick(p: FakePage) -> None:
            p.shown_texts.add(xhs.XHS_AI_DECLARATION)
            if closes:
                p.visible[option] = False  # 面板收起

        page.on_click[option] = pick
        return page

    n = note()
    await n.apply_declarations(page_with(closes=True))
    assert n.declaration_warnings == []

    n = note()  # 点了但面板没收起：看到的文字可能只是选项本身，不能当成已选
    await n.apply_declarations(page_with(closes=False))
    assert n.declaration_warnings and xhs.XHS_AI_DECLARATION in n.declaration_warnings[0]

    n = note()  # 已经显示为已选（例如恢复的草稿）：不再点，免得把它点掉
    page = page_with(closes=True)
    page.shown_texts.add(xhs.XHS_AI_DECLARATION)
    await n.apply_declarations(page)
    assert n.declaration_warnings == [] and option not in page.clicks


async def test_cleanup_helpers_never_raise():
    from multipub._sau.runtime import close_quietly, save_state_quietly

    class Broken:
        async def storage_state(self, path: str) -> None:
            raise RuntimeError("context closed")

        async def close(self) -> None:
            raise RuntimeError("browser crashed")

    assert await save_state_quietly(Broken(), "x.json") is False
    await close_quietly(Broken(), None, Broken())


async def test_douyin_qrcode_fallback_screenshot(sau, tmp_path: Path, monkeypatch):
    _, dy, _ = sau
    monkeypatch.setattr(dy, "DOUYIN_QRCODE_WAIT", 0.2)
    reported: list[dict[str, Any]] = []
    info = await dy._save_douyin_qrcode(FakePage(), str(tmp_path / "acct.json"), qrcode_callback=reported.append)
    assert info["fallback"] and Path(info["image_path"]).is_file()
    assert reported == [info]

    # 刷新二维码时用户已扫码、页面跳到了登录后页面：立即返回，不把首页截图当二维码上报
    page = FakePage()
    page.url = "https://creator.douyin.com/creator-micro/home"
    reported.clear()
    old = tmp_path / "old.png"
    info = await dy._save_douyin_qrcode(page, str(tmp_path / "acct.json"), old, qrcode_callback=reported.append)
    assert reported == [] and info["image_path"] == str(old) and not info.get("fallback")

    # 二维码 src 是普通图片地址：截取二维码元素（上游 save_data_url_image 会直接报错、登录失败）
    sel = dy.DOUYIN_QRCODE_SELECTORS[-1]
    page = FakePage({sel: True})
    page.attrs[(sel, "src")] = "https://p3.douyinpic.com/qrcode.png"

    async def element_png(**_: Any) -> bytes:
        return b"\x89PNG element"

    monkeypatch.setattr(FakeLocator, "screenshot", lambda self, **kw: element_png(**kw))
    info = await dy._save_douyin_qrcode(page, str(tmp_path / "acct.json"), qrcode_callback=reported.append)
    assert Path(info["image_path"]).read_bytes() == b"\x89PNG element" and not info.get("fallback")


def test_login_qrcode_without_opencv(monkeypatch, tmp_path: Path, capsys):
    from multipub._sau.utils import login_qrcode

    monkeypatch.setattr(login_qrcode, "cv2", None)
    assert not login_qrcode.terminal_qr_available()
    assert login_qrcode.decode_qrcode_from_path(tmp_path / "nope.png") is None
    login_qrcode.print_terminal_qrcode("hello", tmp_path / "qr.png", "小红书APP")
    out = capsys.readouterr()
    assert out.out == "" and "小红书APP" in out.err


# ------------------------------------------------------------------ 短信验证码文件 / 窗口里手动验证

@pytest.mark.parametrize("data", [
    "123456\r\n".encode("utf-16"),  # Windows PowerShell 5.1：echo 123456 > 文件（UTF-16LE + BOM）
    b"\xfe\xff" + "123456".encode("utf-16-be"),
    "123456\r\n".encode("utf-16-le"),  # 没有 BOM 的 UTF-16LE
    "﻿123456\n".encode("utf-8"),  # 记事本
    "验证码：１２３ ４５６".encode("gbk"),  # cmd 的本地代码页、全角数字、夹杂文字
    "验证码：123456".encode("utf-8"),
])
def test_verify_code_file_encodings(sau, tmp_path: Path, data: bytes, monkeypatch):
    _, dy, _ = sau
    import locale

    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a: "gbk")  # 中文 Windows
    path = tmp_path / "verify_code.txt"
    path.write_bytes(data)
    assert dy._read_verify_code(path) == "123456"


def test_verify_code_file_unreadable(sau, tmp_path: Path, monkeypatch):
    _, dy, _ = sau
    import locale

    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a: "utf-8")
    path = tmp_path / "verify_code.txt"
    assert dy._read_verify_code(path) == ""  # 还没写
    path.write_bytes(b"")
    assert dy._read_verify_code(path) == ""
    path.write_bytes("\r\n".encode("utf-16"))  # 只有 BOM 和换行（写了一半）
    assert dy._read_verify_code(path) == ""
    for bad in (b"\xff\x00\xc3", "abc".encode("utf-8")):
        path.write_bytes(bad)
        with pytest.raises(ValueError):
            dy._read_verify_code(path)


async def test_sms_wait_survives_bad_code_file(sau, tmp_path: Path, monkeypatch):
    """写错编码 / 写错内容不再中断发布：告警一次，继续等到读出验证码。"""
    import asyncio

    _, dy, _ = sau
    from multipub._sau.runtime import Hooks

    warnings: list[str] = []
    monkeypatch.setattr(dy.douyin_logger, "warning", lambda m, *a, **k: warnings.append(str(m)))
    code_file = tmp_path / "verify_code.txt"
    hooks = Hooks(sms_timeout=5, poll_interval=0.01, evidence_dir=tmp_path, platform="douyin")
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=hooks)
    code_file.write_text("abc", encoding="utf-8")

    async def fix_later() -> None:
        await asyncio.sleep(0.2)
        code_file.write_bytes("654321\r\n".encode("utf-16"))

    task = asyncio.ensure_future(fix_later())
    assert await v._wait_verify_code(FakePage(), code_file) == "654321"
    await task
    assert sum("读不出验证码" in w for w in warnings) == 1  # 同样的内容只告警一次


async def test_sms_stale_file_that_cannot_be_deleted(sau, tmp_path: Path, monkeypatch):
    conf, dy, _ = sau
    from multipub._sau.runtime import Hooks

    code_file = conf.verify_code_file()
    code_file.write_text("000000", encoding="utf-8")
    real_unlink = Path.unlink

    def locked(self: Path, *a: Any, **k: Any) -> None:
        if self == code_file:
            raise PermissionError("文件被占用")
        real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)

    def user_writes(path: Path) -> None:
        path.write_text("246810", encoding="utf-8")

    hooks = Hooks(sms_code_required=user_writes, sms_timeout=2, poll_interval=0.01)
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", hooks=hooks)
    page = FakePage({v.SMS_INPUT_SELECTOR: True, "text=验证": True})
    assert await v._handle_sms_verification(page) is True
    assert page.filled[v.SMS_INPUT_SELECTOR] == "246810"  # 不是残留的 000000

    # 旧内容一直没变：不提交过期验证码，按超时处理
    code_file.write_text("000000", encoding="utf-8")
    v = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json",
                       hooks=Hooks(sms_timeout=0.2, poll_interval=0.01, evidence_dir=tmp_path))
    from multipub._sau.runtime import SmsCodeTimeout

    with pytest.raises(SmsCodeTimeout):
        await v._handle_sms_verification(FakePage({v.SMS_INPUT_SELECTOR: True}))


async def test_sms_completed_in_headed_window(sau, tmp_path: Path):
    """--headed：用户直接在窗口里输入验证码，弹窗关闭后继续发布，不再空等 sms_timeout。"""
    conf, dy, _ = sau
    sms_input = dy.DouYinBaseUploader.SMS_INPUT_SELECTOR
    asked: list[Path] = []

    def user_types_in_window(path: Path) -> None:
        asked.append(path)
        page.visible[sms_input] = False
        page.visible["text=获取验证码"] = False

    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", headless=False,
                           hooks=_fast_hooks(tmp_path, publish_timeout=10, sms_timeout=30,
                                             sms_code_required=user_types_in_window))
    page = FakePage({DY_PUBLISH: True})
    publish_clicks = [0]

    def on_publish(p: FakePage) -> None:
        publish_clicks[0] += 1
        if publish_clicks[0] == 1:
            p.visible.update({sms_input: True, "text=获取验证码": True})
        else:
            p.succeeded = True

    page.on_click[DY_PUBLISH] = on_publish
    await video._publish_until_success(page, "视频", "**/manage**")
    assert publish_clicks[0] == 2 and asked == [conf.verify_code_file()]
    assert sms_input not in page.filled and video._sms_rounds == 1  # 没有往输入框里填任何东西

    # 验证完成后页面自己重新提交了：不再补点（避免重复发布）
    video = dy.DouYinVideo("t", "a.mp4", [], 0, "x.json", headless=False,
                           hooks=_fast_hooks(tmp_path, publish_timeout=10, sms_timeout=30))

    def resubmits(path: Path) -> None:
        page.visible[sms_input] = False
        req = FakeRequest(DY_CREATE)
        page.emit("request", req)
        page.emit("response", FakeResponse(req, {"status_code": 0, "item_id": "7400000000000000002"}))
        page.succeeded = True

    video.hooks.sms_code_required = resubmits
    page = FakePage({DY_PUBLISH: True})
    page.on_click[DY_PUBLISH] = lambda p: p.visible.update({sms_input: True})
    await video._publish_until_success(page, "视频", "**/manage**")
    assert page.clicks.count(DY_PUBLISH) == 1 and video.published_item_id == "7400000000000000002"


def test_publish_guard_no_reclick_after_page_resubmits():
    from multipub._sau.runtime import PublishGuard

    g = PublishGuard(reclick_gap=0)
    g.clicked()
    g.request_sent()
    g.request_done(False, "需要验证")
    g.blocked_by_platform()  # 弹出短信验证：允许再点一次
    assert g.may_click() and g.rejection is None
    g.request_sent()  # 验证后页面（或用户）自己又提交了
    g.request_done(None)
    assert not g.may_click()
    g.request_done(False, "需要验证")
    g.clear_rejection()  # 弹出人机验证：之前的失败不算数，但也不额外允许再点
    assert g.rejection_settled(0) is None and not g.may_click()


# ------------------------------------------------------------------ 有头模式下的人机验证

CAPTCHA = "text=请完成安全验证"


def _captcha_after_click(page: FakePage) -> None:
    req = FakeRequest(DY_CREATE)
    page.emit("request", req)
    page.emit("response", FakeResponse(req, {"status_code": 2190008, "status_msg": "需要完成验证"}))
    page.visible[CAPTCHA] = True


async def test_douyin_headed_captcha_waits_for_user(sau, tmp_path: Path):
    import asyncio

    _, dy, _ = sau
    from multipub._sau.runtime import CaptchaRequired

    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", headless=False,
                         hooks=_fast_hooks(tmp_path, publish_timeout=10, reclick_gap=0.05))
    page = FakePage({DY_PUBLISH: True})
    page.on_click[DY_PUBLISH] = _captcha_after_click

    async def user_solves() -> None:
        await asyncio.sleep(0.6)  # 远超 reject_grace（0）：以前这里早就报「平台拒绝」了
        page.visible[CAPTCHA] = False
        page.succeeded = True

    task = asyncio.ensure_future(user_solves())
    await note._publish_until_success(page, "图文", "**/manage**")
    await task
    assert page.clicks.count(DY_PUBLISH) == 1  # 验证显示期间不点「发布」

    # 无头模式：用户看不到窗口，照旧很快按人机验证报错（已点击发布 → unknown_outcome）
    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", headless=True,
                         hooks=_fast_hooks(tmp_path, publish_timeout=10))
    page = FakePage({DY_PUBLISH: True})
    page.on_click[DY_PUBLISH] = _captcha_after_click
    with pytest.raises(CaptchaRequired) as info:
        await note._publish_until_success(page, "图文", "**/manage**")
    assert info.value.after_submit

    # 有头模式下用户一直没完成：最多再等一个 publish_timeout，然后按人机验证报错
    note = dy.DouYinNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", headless=False,
                         hooks=_fast_hooks(tmp_path, publish_timeout=0.4, reclick_gap=0.01))
    page = FakePage({DY_PUBLISH: True})
    page.on_click[DY_PUBLISH] = _captcha_after_click
    with pytest.raises(CaptchaRequired) as info:
        await note._publish_until_success(page, "图文", "**/manage**")
    assert info.value.after_submit and page.clicks.count(DY_PUBLISH) == 1


async def test_xhs_headed_captcha_waits_for_user(sau, tmp_path: Path):
    import asyncio

    _, _, xhs = sau
    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题", headless=False,
                               hooks=_fast_hooks(tmp_path, publish_timeout=10, platform="xiaohongshu"))
    page = FakePage({XHS_PUBLISH: True})

    def captcha(p: FakePage) -> None:
        req = FakeRequest(XHS_CREATE)
        p.emit("request", req)
        p.emit("response", FakeResponse(req, {"success": False, "code": -1, "msg": "请完成验证"}))
        p.visible[".red-captcha"] = True

    page.on_click[XHS_PUBLISH] = captcha

    async def user_solves() -> None:
        await asyncio.sleep(0.5)
        page.visible[".red-captcha"] = False
        page.succeeded = True

    task = asyncio.ensure_future(user_solves())
    await note._publish_until_success(page, "图文")
    await task
    assert page.clicks.count(XHS_PUBLISH) == 1


# ------------------------------------------------------------------ 定时发布核实

def _when() -> datetime:
    return datetime(2026, 10, 23, 8, 5)


DY_SCHEDULE = "[class^='radio']:has-text('定时发布')"
DY_DATE = '.semi-input[placeholder="日期和时间"]'


def _douyin_schedule_page(*, state: bool | None = False, click_works: bool = True, date_input: bool = True,
                          typing_works: bool = True) -> FakePage:
    page = FakePage({DY_SCHEDULE: True, DY_DATE: date_input})
    page.attrs[(DY_SCHEDULE, "evaluate")] = state
    page.attrs[(DY_DATE, "value")] = "2026-10-22 18:00"  # 页面默认时间
    if click_works:
        page.on_click[DY_SCHEDULE] = lambda p: p.attrs.__setitem__((DY_SCHEDULE, "evaluate"), True)
    if not typing_works:
        page.readonly.add(DY_DATE)
    return page


async def test_douyin_schedule_is_verified(sau, tmp_path: Path):
    _, dy, _ = sau
    from multipub._sau.runtime import ScheduleFailed

    def note() -> Any:
        return dy.DouYinNote(["1.jpg"], "正文", [], _when(), "x.json", title="标题",
                             publish_strategy="scheduled", hooks=_fast_hooks(tmp_path))

    page = _douyin_schedule_page()
    await note().set_schedule_time_douyin(page, _when())
    assert page.clicks[0] == DY_SCHEDULE and page.filled[DY_DATE] == "2026-10-23 08:05"
    assert "Enter" in page.keyboard.pressed

    page = _douyin_schedule_page(state=True)  # 已经是定时（如恢复的草稿）：不再点
    await note().set_schedule_time_douyin(page, _when())
    assert DY_SCHEDULE not in page.clicks

    # 看不出选中状态：日期框出现且值正确就算设置上了
    page = _douyin_schedule_page(state=None, click_works=False)
    await note().set_schedule_time_douyin(page, _when())

    bad_pages = {
        "没有变成选中状态": _douyin_schedule_page(click_works=False),  # 点击被吞掉：以前会直接立即发布
        "日期和时间」输入框": _douyin_schedule_page(state=None, click_works=False, date_input=False),
        "应为「2026-10-23 08:05」": _douyin_schedule_page(typing_works=False),
    }
    for reason, page in bad_pages.items():
        n = note()
        with pytest.raises(ScheduleFailed) as info:
            await n.set_schedule_time_douyin(page, _when())
        assert reason in str(info.value) and "作品没有发出" in str(info.value)
        assert not n._publish_clicked and n.publish_guard.clicks == 0
    assert (tmp_path / "douyin-schedule.png").is_file()


XHS_SWITCH = ".d-switch"
XHS_DATE = ".d-datepicker-input-filter input.d-text"


def _xhs_schedule_page(*, state: bool | None = False, click_works: bool = True, date_input: bool = True,
                       fill_works: bool = True) -> FakePage:
    page = FakePage({XHS_SWITCH: True})
    page.attrs[(XHS_SWITCH, "evaluate")] = state
    page.attrs[(XHS_DATE, "value")] = "2026-10-22 18:00"
    if state:
        page.visible[XHS_DATE] = date_input

    def toggle(p: FakePage) -> None:
        on = not p.attrs.get((XHS_SWITCH, "evaluate"))
        if p.attrs[(XHS_SWITCH, "evaluate")] is not None:
            p.attrs[(XHS_SWITCH, "evaluate")] = on
        p.visible[XHS_DATE] = date_input and (on or p.attrs[(XHS_SWITCH, "evaluate")] is None)

    if click_works:
        page.on_click[XHS_SWITCH] = toggle
    if not fill_works:
        page.readonly.add(XHS_DATE)
    return page


async def test_xhs_schedule_is_verified(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import ScheduleFailed

    def note() -> Any:
        return xhs.XiaoHongShuNote(["1.jpg"], "正文", [], _when(), "x.json", title="标题",
                                   publish_strategy="scheduled", hooks=_fast_hooks(tmp_path, platform="xiaohongshu"))

    page = _xhs_schedule_page()
    await note().set_schedule_time_xiaohongshu(page, _when())
    assert page.clicks == [XHS_SWITCH] and page.filled[XHS_DATE] == "2026-10-23 08:05"
    assert XHS_DATE in page.evaluated  # 失焦提交输入
    assert "Enter" not in page.keyboard.pressed  # 不按回车（可能触发表单隐式提交）

    page = _xhs_schedule_page(state=True)  # 开关已经打开：再点会把它关掉
    await note().set_schedule_time_xiaohongshu(page, _when())
    assert XHS_SWITCH not in page.clicks

    page = _xhs_schedule_page(state=None)  # 看不出开关状态，日期框还没出现：照上游点一下
    await note().set_schedule_time_xiaohongshu(page, _when())
    assert page.clicks == [XHS_SWITCH]
    page = _xhs_schedule_page(state=None)
    page.visible[XHS_DATE] = True  # 看不出开关状态，但日期框已经在了：不点
    await note().set_schedule_time_xiaohongshu(page, _when())
    assert XHS_SWITCH not in page.clicks

    for reason, page in {"没有打开": _xhs_schedule_page(click_works=False),
                         "应为「2026-10-23 08:05」": _xhs_schedule_page(fill_works=False)}.items():
        n = note()
        with pytest.raises(ScheduleFailed) as info:
            await n.set_schedule_time_xiaohongshu(page, _when())
        assert reason in str(info.value) and "笔记没有发出" in str(info.value) and not n._publish_clicked
    assert (tmp_path / "xiaohongshu-schedule.png").is_file()


def test_schedule_value_matches():
    from multipub._sau.runtime import schedule_value_matches

    when = _when()
    for ok in ("2026-10-23 08:05", "2026/10/23 8:05", "2026年10月23日 08:05", " 2026-10-23 08:05:00 "):
        assert schedule_value_matches(ok, when), ok
    for bad in ("", None, "2026-10-23 08:06", "2026-10-22 08:05", "08:05"):
        assert not schedule_value_matches(bad, when), bad


# ------------------------------------------------------------------ 小红书登录兜底

XHS_LOGIN_BOX = "div[class*='login-box']"


async def test_xhs_qrcode_fallback_screenshot(sau, tmp_path: Path, monkeypatch):
    _, _, xhs = sau
    monkeypatch.setattr(xhs, "XHS_QR_SWITCH_WAIT", 0.6)
    reported: list[dict[str, Any]] = []
    page = FakePage({XHS_LOGIN_BOX: True})  # 登录框在，但切换按钮（哈希类名）和二维码都找不到
    info = await xhs._save_xhs_qrcode(page, str(tmp_path / "acct.json"), qrcode_callback=reported.append)
    assert info["fallback"] and Path(info["image_path"]).is_file() and reported == [info]
    assert page.clicks == []  # 试过的切换方式都不存在

    # 「扫码登录」文字可点：切到扫码面板
    page = FakePage({XHS_LOGIN_BOX: True, "text=扫码登录": True})
    # （扫码面板以二维码图片可见为准：只是隐藏在 DOM 里的扫码面板不算）
    page.on_click["text=扫码登录"] = lambda p: p.visible.__setitem__("xpath=..//following-sibling::div//img", True)
    assert await xhs._open_xhs_qrcode_panel(page) is True and page.clicks == ["text=扫码登录"]


class _FakeContext:
    def __init__(self, page: FakePage):
        self.page, self.closed = page, False

    async def add_init_script(self, **_: Any) -> None:
        return None

    async def new_page(self) -> FakePage:
        return self.page

    async def storage_state(self, path: str) -> None:
        Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser(_FakeContext):
    async def new_context(self, **_: Any) -> _FakeContext:
        return self.context


def _fake_playwright(page: FakePage) -> tuple[Any, _FakeBrowser]:
    browser = _FakeBrowser(page)
    browser.context = _FakeContext(page)

    class Chromium:
        async def launch(self, **_: Any) -> _FakeBrowser:
            return browser

    class Manager:
        chromium = Chromium()

        async def __aenter__(self) -> "Manager":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    return Manager, browser


async def test_xhs_login_without_qrcode_still_waits_for_manual_login(sau, tmp_path: Path, monkeypatch):
    """二维码定位不到时不再立刻失败关窗口：上报登录页截图，继续等用户在窗口里登录。"""
    _, _, xhs = sau
    monkeypatch.setattr(xhs, "XHS_QR_SWITCH_WAIT", 0.6)
    page = FakePage({XHS_LOGIN_BOX: True})
    manager, browser = _fake_playwright(page)
    monkeypatch.setattr(xhs, "async_playwright", manager)

    async def valid(path: str) -> bool:
        return True

    monkeypatch.setattr(xhs, "cookie_auth", valid)
    reported: list[dict[str, Any]] = []
    polls = [0]
    real_check = xhs._is_xhs_login_completed

    async def check(p: FakePage) -> bool:
        polls[0] += 1
        if polls[0] == 3:  # 用户在窗口里登录成功：离开登录页
            p.url = "https://creator.xiaohongshu.com/new/home"
            p.visible[XHS_LOGIN_BOX] = False
        return await real_check(p)

    monkeypatch.setattr(xhs, "_is_xhs_login_completed", check)
    acct = tmp_path / "acct.json"
    result = await xhs.xiaohongshu_cookie_gen(str(acct), qrcode_callback=reported.append,
                                              poll_interval=0, max_checks=10, headless=False)
    assert result["success"] and acct.is_file(), result
    assert polls[0] == 3 and reported and reported[0]["fallback"]  # 一直轮询到用户登录成功
    assert not Path(reported[0]["image_path"]).exists()  # 登录结束后清理截图
    assert browser.closed and browser.context.closed


def test_qrcode_relay_fallback_note(settings, tmp_path: Path):
    shot = tmp_path / "page.png"
    shot.write_bytes(b"\x89PNG page")
    for headless, window_hint in ((True, False), (False, True)):
        ctx, events = make_ctx(settings, "xiaohongshu", tmp_path, action="login")
        ctx.headless = headless
        callback, _ = bridge.qrcode_callback(ctx)
        callback({"image_path": str(shot), "fallback": True})
        note = events[-1]["note"]
        assert "登录页截图" in note and ("浏览器窗口里扫码或登录" in note) is window_hint
        assert ("去掉 --headless" in note) is headless  # 无头时截图里可能根本没有二维码


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


def test_douyin_validate_title_with_emoji_near_limit(media: Path):
    # 30 个字符没超限，但按 UTF-16 是 31：网页可能截断 → 提醒（发布前还会读回核对）
    assert _levels(DouyinAdapter.validate(dy_video(media, title="一" * 29 + "😀")), "title") == {"warning"}
    assert _levels(DouyinAdapter.validate(dy_video(media, title="一" * 28 + "😀")), "title") == set()
    assert _levels(DouyinAdapter.validate(dy_note(media, title="一" * 19 + "😀")), "title") == {"warning"}
    # 正文没超 1000，但加上「 #话题」超了
    assert _levels(DouyinAdapter.validate(dy_note(media, body="字" * 997, tags=["徒步"])), "tags") == {"warning"}
    assert _levels(DouyinAdapter.validate(dy_note(media, body="字" * 990, tags=["徒步"])), "tags") == set()


def test_douyin_success_message_lists_notes(settings, tmp_path: Path, media: Path):
    adapter = DouyinAdapter(make_ctx(settings, "douyin", tmp_path)[0])
    r = adapter._success(dy_video(media), "7400000000000000003", notes=["自定义封面没能设置上，抖音会用推荐封面"])
    assert r.url == "https://www.douyin.com/video/7400000000000000003" and "推荐封面" in r.message


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
    assert kw["sync_toutiao"] is None  # 没写：不碰开关
    assert kw["collection_name"] == "合集A" and kw["productLink"] == "https://p" and kw["productTitle"] == "商品"
    # cover.jpg 是 1280x720 横图 → 横封面
    assert kw["thumbnail_landscape_path"] == str(media / "cover.jpg") and kw["thumbnail_portrait_path"] is None
    assert kw["headless"] is True and kw["debug"] is False
    hooks = kw["hooks"]
    assert hooks.evidence_dir == ctx.run_dir and hooks.platform == "douyin"
    assert hooks.upload_timeout == settings.timeout_for("publish_video") - 600
    assert hooks.sms_code_required is not None

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

    # 显式 false 要传下去（上传器会关掉开着的开关），不能和"没写"混为一谈
    await DouyinAdapter(ctx).publish(dy_video(media, options={"sync_toutiao": False}))
    assert recorder.instances[-1].kwargs["sync_toutiao"] is False

    # 本次任务的总超时（--timeout）决定上传等待
    long_ctx, _ = make_ctx(settings, "douyin", tmp_path)
    long_ctx.timeout = 10800
    await DouyinAdapter(long_ctx).publish(dy_video(media))
    assert recorder.instances[-1].kwargs["hooks"].upload_timeout == 10800 - 600


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
    ("SmsCodeTimeout", False, ("needs_action", ErrorCode.SMS_CODE_REQUIRED, False)),
    # 点过发布之后的人机验证 / 短信验证：结果未知，必须是 unknown_outcome（发布记录据此拦住重发）
    ("SmsCodeTimeout", True, ("failed", ErrorCode.UNKNOWN_OUTCOME, False)),
    ("CaptchaRequired", False, ("needs_action", ErrorCode.CAPTCHA, False)),
    ("CaptchaRequired", True, ("failed", ErrorCode.UNKNOWN_OUTCOME, False)),
    ("LoginPageDetected", False, ("needs_action", ErrorCode.LOGIN_REQUIRED, False)),
    ("LoginPageDetected", True, ("failed", ErrorCode.UNKNOWN_OUTCOME, False)),
    ("CookieInvalid", False, ("needs_action", ErrorCode.LOGIN_REQUIRED, False)),
    ("UploadFailed", False, ("failed", ErrorCode.PLATFORM_ERROR, True)),
    ("PublishRejected", True, ("failed", ErrorCode.PLATFORM_ERROR, False)),
    ("DeclarationFailed", False, ("failed", ErrorCode.VALIDATION, False)),
    ("ScheduleFailed", False, ("failed", ErrorCode.VALIDATION, False)),  # 定时没设上：发布前拒绝
    ("SmsVerifyFailed", False, ("needs_action", ErrorCode.SMS_CODE_REQUIRED, False)),
    ("SmsVerifyFailed", True, ("failed", ErrorCode.UNKNOWN_OUTCOME, False)),
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


async def test_after_submit_verification_blocks_republish(settings, tmp_path: Path):
    """点过发布后弹出人机/短信验证：结果未知，发布记录必须拦住不带 --force 的重发。"""
    from multipub import history
    from multipub._sau import runtime as rt

    for i, exc in enumerate((rt.CaptchaRequired("等待视频发布成功 超过 240 秒（已点击发布，结果未知）；页面停在人机验证",
                                                step="x", after_submit=True),
                             rt.SmsCodeTimeout("300 秒内没有收到短信验证码", step="x", after_submit=True),
                             rt.SmsVerifyFailed("验证码错误", step="x", after_submit=True))):
        ctx, _ = make_ctx(settings, "douyin", tmp_path)
        ctx.mark_submit_started()
        with pytest.raises(PublishError) as info:
            async with bridge.translate_errors(ctx):
                raise exc
        err = info.value
        msg = str(err)
        assert "后台确认" in msg and msg.count("已点击") == 1
        if isinstance(exc, rt.SmsCodeTimeout):
            assert "发布进行中" in msg and "mpub verify-code douyin" in msg
        else:
            assert "--headed" in msg
        result = Result(platform="douyin", account="main", action="publish", status=err.status,
                        error_code=err.code.value, submit_started=True, message=msg)
        history.append(settings, result, run_id=f"r{i}", fingerprint=f"fp{i}")
        assert history.find_previous(settings, f"fp{i}") is not None

    # 点发布之前的短信超时：提示在发布进行中提交验证码（重新发布会重新发短信）
    ctx, _ = make_ctx(settings, "douyin", tmp_path)
    with pytest.raises(PublishError) as info:
        async with bridge.translate_errors(ctx):
            raise rt.SmsCodeTimeout("300 秒内没有收到短信验证码", step="x")
    assert info.value.code is ErrorCode.SMS_CODE_REQUIRED and "发布进行中" in str(info.value)
    assert "并重新发布" not in str(info.value)


def test_sau_timeouts_follow_job_timeout(settings, tmp_path: Path):
    assert bridge.sau_timeouts(settings, "douyin", Kind.VIDEO)["upload_timeout"] == \
        settings.timeout_for("publish_video") - 600
    assert bridge.sau_timeouts(settings, "douyin", Kind.VIDEO, 10800)["upload_timeout"] == 10200
    assert bridge.sau_timeouts(settings, "douyin", Kind.NOTE, 300)["upload_timeout"] == 180  # 下限
    ctx, events = make_ctx(settings, "douyin", tmp_path)
    ctx.timeout = 7200
    hooks = bridge.make_hooks(ctx, Kind.VIDEO, sms_message="抖音要求短信验证")
    assert hooks.upload_timeout == 6600
    hooks.sms_code_required(tmp_path / "verify_code.txt")
    need = [e for e in events if e["event"] == "needs_input"][-1]
    assert need["kind"] == "sms_code" and "600 秒内有效" in need["message"]
    assert hooks.sms_timeout == 600 and "浏览器窗口" not in need["message"]  # 无头：窗口里输入不了


def test_sms_timeout_default_and_job_budget(settings, tmp_path: Path):
    """默认等 600 秒（Claude Code 要先看到 needs_input 再去问用户）；但不超过任务剩下的时间。"""
    from multipub._sau.runtime import Hooks

    assert Hooks().sms_timeout == 600
    assert bridge.sau_timeouts(settings, "douyin", Kind.NOTE)["sms_timeout"] == 600
    settings.raw.setdefault("platforms", {})["douyin"] = {"sms_timeout": 120}  # config.toml 可调
    assert bridge.sau_timeouts(settings, "douyin", Kind.NOTE)["sms_timeout"] == 120
    del settings.raw["platforms"]["douyin"]

    ctx, events = make_ctx(settings, "douyin", tmp_path)
    ctx.timeout, ctx.headless = 400, False  # 任务只剩约 400 秒：再留 60 秒给提交后的发布
    hooks = bridge.make_hooks(ctx, Kind.NOTE, sms_message="抖音要求短信验证")
    hooks.sms_code_required(tmp_path / "verify_code.txt")
    need = [e for e in events if e["event"] == "needs_input"][-1]
    assert 330 <= hooks.sms_timeout <= 340 and f"{int(hooks.sms_timeout)} 秒内有效" in need["message"]
    assert "浏览器窗口里输入验证码" in need["message"]  # --headed：也可以直接在窗口里输入
    ctx.timeout = 30  # 几乎没时间了也至少给 60 秒
    hooks.sms_code_required(tmp_path / "verify_code.txt")
    assert hooks.sms_timeout == 60


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
    # 扫码窗口 = 任务超时（worker 拿到的是 timeouts.login − 30）− 120 秒，留出 cookie 校验、抓二维码的时间
    assert seen == {"handle": True, "headless": True, "login_timeout": settings.timeout_for("login") - 30 - 120}
    qr_events = [e for e in events if e["event"] == "qrcode"]
    assert qr_events and qr_events[0]["path"] == str(settings.qrcode_dir / "douyin-main.png")
    assert not (settings.qrcode_dir / "douyin-main.png").exists()  # 登录结束后清理

    # ctx.timeout（worker 的总超时）决定扫码窗口；二维码图片删不掉（如被看图软件占用）不影响登录结果
    lctx, lev = make_ctx(settings, "douyin", tmp_path, action="login", account="alt")
    lctx.timeout = 600
    (settings.qrcode_dir / "douyin-alt.png").mkdir(parents=True)
    (settings.qrcode_dir / "douyin-alt.png" / "x").write_text("占用", encoding="utf-8")
    result = await DouyinAdapter(lctx).login()
    assert result.status is Status.OK and seen["login_timeout"] == 600 - 120
    assert any("删除二维码图片失败" in e.get("message", "") for e in lev if e["event"] == "log")

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


async def test_goto_resilient_retries_once_on_timeout():
    from multipub._sau.runtime import goto_resilient

    class PWTimeout(Exception):
        pass

    PWTimeout.__name__ = "TimeoutError"

    class Page:
        def __init__(self, fails):
            self.fails, self.calls = fails, []

        async def goto(self, url, **kw):
            self.calls.append(kw)
            if len(self.calls) <= self.fails:
                raise PWTimeout("Timeout 60000ms exceeded")

    ok = Page(fails=1)
    await goto_resilient(ok, "https://example.com")
    assert len(ok.calls) == 2 and all(c["wait_until"] == "domcontentloaded" for c in ok.calls)

    bad = Page(fails=5)
    with pytest.raises(PWTimeout):
        await goto_resilient(bad, "https://example.com")
    assert len(bad.calls) == 2


# ------------------------------------------------------------------ 小红书：本地仿真测试发现的问题（tests/sim/test_sim_xhs.py）

class _CoveredLocator(FakeLocator):
    """可见、可用，但被弹窗遮罩挡住：Playwright 的可点性检查（trial）会超时。"""

    async def click(self, trial: bool = False, **kw: Any) -> None:
        raise RuntimeError("<div class=\"d-modal-mask\"> intercepts pointer events")


class _CoveredPage(FakePage):
    def locator(self, selector: str) -> FakeLocator:
        if selector == XHS_PUBLISH:
            return _CoveredLocator(self, selector)
        return super().locator(selector)


async def test_xhs_covered_publish_button_is_not_clicked(sau, tmp_path: Path):
    """封面弹窗等挡住「发布」时不算点击、不标记已提交：超时报 timeout（可重试），不是 unknown_outcome。"""
    _, _, xhs = sau
    from multipub._sau.runtime import StepTimeout

    clicked: list[int] = []
    hooks = _fast_hooks(tmp_path, platform="xiaohongshu", publish_timeout=0.5,
                        before_publish_click=lambda: clicked.append(1))
    video = xhs.XiaoHongShuVideo("标题", "v.mp4", [], 0, "x.json", hooks=hooks)
    page = _CoveredPage({XHS_PUBLISH: True})
    with pytest.raises(StepTimeout) as info:
        await video._publish_until_success(page, "视频")
    assert not info.value.after_submit and clicked == [] and not video._publish_clicked
    assert video.publish_guard.clicks == 0 and "Escape" in page.keyboard.pressed


async def test_xhs_cover_failure_closes_modal_and_warns(sau, tmp_path: Path):
    _, _, xhs = sau
    video = xhs.XiaoHongShuVideo("标题", "v.mp4", [], 0, "x.json", thumbnail_path="c.png",
                                 hooks=_fast_hooks(tmp_path, platform="xiaohongshu"))
    # 弹窗打开了，但没有「上传封面」页签、也没有上传框；弹窗不响应 Escape，点「取消」才关
    page = FakePage({"div.upload-cover": True, xhs.XHS_MODAL_SELECTOR: True, "text=取消": True})
    page.on_click["text=取消"] = lambda p: p.visible.update({xhs.XHS_MODAL_SELECTOR: False})
    await video.set_thumbnail(page, "c.png")
    assert "text=取消" in page.clicks and not page.visible[xhs.XHS_MODAL_SELECTOR]
    assert video.declaration_warnings and "封面" in video.declaration_warnings[0]
    assert (tmp_path / "xiaohongshu-cover.png").is_file()


async def test_xhs_title_read_back(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import DeclarationFailed

    key = 'input[placeholder*="填写标题"]'
    want = "三分钟学会做饭🔥超简单的家常菜做法分享"
    hooks = _fast_hooks(tmp_path, platform="xiaohongshu")
    ok = xhs.XiaoHongShuVideo(want, "v.mp4", [], 0, "x.json", hooks=hooks)
    page = FakePage({key: True})
    await ok.fill_title(page)
    assert page.filled[key] == want and ok.declaration_warnings == []

    # 标题框按 UTF-16 计数，把末尾截掉了：重填一次仍然如此 → 拒绝发布
    cut = xhs.XiaoHongShuVideo(want, "v.mp4", [], 0, "x.json", hooks=hooks)
    page = FakePage({key: True})
    page.readonly.add(key)
    page.attrs[(key, "value")] = want[:-1]
    with pytest.raises(DeclarationFailed, match="缩短标题"):
        await cut.fill_title(page)

    # 读回来完全不一样：只告警（写进结果消息）
    odd = xhs.XiaoHongShuVideo(want, "v.mp4", [], 0, "x.json", hooks=hooks)
    page.attrs[(key, "value")] = "别的标题"
    await odd.fill_title(page)
    assert odd.declaration_warnings and "别的标题" in odd.declaration_warnings[0]


async def test_xhs_desc_escapes_suggestions_before_newline(sau):
    """正文里的 #话题 / @某人 会弹出候选框，候选框开着时回车会选中第一个候选：这样的行回车前先按 Escape。"""
    _, _, xhs = sau
    video = xhs.XiaoHongShuVideo("标题", "v.mp4", [], 0, "x.json", desc="去爬山 #徒步\n第二行\n@小明 你好")
    page = FakePage({xhs.XHS_DESC_SELECTOR: True})
    await video.fill_desc(page)
    assert page.keyboard.pressed[3:] == ["Escape", "Enter", "Enter", "Escape", "Enter"]


class _UploadStatePage(FakePage):
    def __init__(self, states: list[dict[str, str]], **kw: Any):
        super().__init__(**kw)
        self.states = states

    async def evaluate(self, script: str = "", arg: Any = None) -> Any:
        from multipub._sau.uploader.xiaohongshu_uploader import main as xhs

        if script == xhs._UPLOAD_STATE_JS:
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return await super().evaluate(script, arg)


async def test_xhs_failed_image_upload_is_refused_before_publish(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import Deadline, UploadFailed

    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                               hooks=_fast_hooks(tmp_path, platform="xiaohongshu"))
    busy = {"busy": "上传中", "failed": ""}
    page = _UploadStatePage([busy, {"busy": "", "failed": "上传失败"}])
    with pytest.raises(UploadFailed, match="笔记没有发出"):
        await note._wait_uploads_settled(page, Deadline(30, "等待图片上传完成"))
    assert (tmp_path / "xiaohongshu-upload-failed.png").is_file()
    await note._wait_uploads_settled(_UploadStatePage([busy, {"busy": "", "failed": ""}]),
                                     Deadline(30, "等待图片上传完成"))  # 传完了：正常返回


class _GatewayResponse(FakeResponse):
    async def json(self) -> Any:
        raise ValueError("not json")


async def test_xhs_gateway_error_detail(sau, tmp_path: Path):
    _, _, xhs = sau
    from multipub._sau.runtime import StepTimeout

    video = xhs.XiaoHongShuVideo("标题", "v.mp4", [], 0, "x.json",
                                 hooks=_fast_hooks(tmp_path, platform="xiaohongshu", publish_timeout=0.5))
    page = FakePage({XHS_PUBLISH: True})

    def submit(p: FakePage) -> None:
        req = FakeRequest(XHS_CREATE)
        p.emit("request", req)
        p.emit("response", _GatewayResponse(req, None, status=502))

    page.on_click[XHS_PUBLISH] = submit
    with pytest.raises(StepTimeout, match="HTTP 502") as info:
        await video._publish_until_success(page, "视频")
    assert info.value.after_submit and page.clicks.count(XHS_PUBLISH) == 1


async def test_xhs_classify_failure(sau, tmp_path: Path):
    """填表时的 Playwright 超时：页面停在登录页 / 人机验证时改报对应的错误（适配器给 login_required / captcha）。"""
    _, _, xhs = sau
    from multipub._sau.runtime import CaptchaRequired, LoginPageDetected

    note = xhs.XiaoHongShuNote(["1.jpg"], "正文", [], 0, "x.json", title="标题",
                               hooks=_fast_hooks(tmp_path, platform="xiaohongshu"))
    err = TimeoutError("Locator.fill: Timeout 30000ms exceeded.")
    login = FakePage()
    login.url = "https://creator.xiaohongshu.com/login?redirectReason=401"
    with pytest.raises(LoginPageDetected) as info:
        await note._classify_failure(login, err)
    assert not info.value.after_submit
    with pytest.raises(CaptchaRequired):
        await note._classify_failure(FakePage({"text=请完成验证": True}), err)
    await note._classify_failure(FakePage(), err)  # 看不出原因：交给调用方原样抛出


def test_xiaohongshu_validate_emoji_title(media: Path):
    issues = XiaohongshuAdapter.validate(xhs_post(media, title="三分钟学会做饭🔥超简单的家常菜做法分享啊"))
    assert _levels(issues, "title") == {"warning"}
    assert XiaohongshuAdapter.validate(xhs_post(media, title="三分钟学会做饭🔥")) == []
