"""SAU 的 conf 模块替身（上游 conf.py 被 gitignore，安装包里没有）。

上游各模块在 import 时绑定这些值（包括函数默认参数），所以必须先调用
``configure(settings, headless)``，再导入 uploader 模块：

    from multipub._sau import conf
    conf.configure(settings, headless=True)
    from multipub._sau.uploader.douyin_uploader import main as douyin

浏览器启动参数由 ``launch_kwargs()`` 在调用时读取，因此 channel / executable_path
总是以最后一次 configure() 为准。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 默认值仅为占位：configure() 之前不应启动浏览器（launch_kwargs 会报错）
BASE_DIR: Path = Path.home() / ".multipub-sau-unconfigured"
LOCAL_CHROME_PATH: str = ""
LOCAL_CHROME_CHANNEL: str = "chromium"
LOCAL_CHROME_HEADLESS: bool = True
DEBUG_MODE: bool = False

_configured = False


def configure(settings: Any, headless: bool) -> None:
    """按 multipub 的 Settings 设置 SAU 运行环境（数据目录、浏览器、日志）。"""
    global BASE_DIR, LOCAL_CHROME_PATH, LOCAL_CHROME_CHANNEL, LOCAL_CHROME_HEADLESS, DEBUG_MODE, _configured

    BASE_DIR = Path(settings.sau_home)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_CHROME_PATH = str(settings.browser.executable_path or "")
    LOCAL_CHROME_CHANNEL = str(settings.browser.channel or "chromium")
    LOCAL_CHROME_HEADLESS = bool(headless)
    DEBUG_MODE = False
    _configured = True

    from .utils import log

    log.setup(BASE_DIR / "logs")


def is_configured() -> bool:
    return _configured


def verify_code_file() -> Path:
    """抖音发布时短信验证码的输入文件（mpub verify-code douyin <code> 写入这里）。"""
    return BASE_DIR / "verify_code.txt"


def launch_kwargs(headless: bool, args: list[str] | None = None) -> dict[str, Any]:
    """multipub patch: 上游写死 channel="chromium"、忽略 LOCAL_CHROME_PATH，这里按配置选择浏览器。"""
    if not _configured:
        raise RuntimeError("multipub._sau.conf.configure() 尚未调用")
    kwargs: dict[str, Any] = {"headless": headless}
    if args:
        kwargs["args"] = list(args)
    if LOCAL_CHROME_PATH:
        kwargs["executable_path"] = LOCAL_CHROME_PATH
    elif LOCAL_CHROME_CHANNEL:
        kwargs["channel"] = LOCAL_CHROME_CHANNEL
    return kwargs
