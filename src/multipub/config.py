"""运行配置与数据目录。

数据目录（账号 cookie、浏览器工具、发布记录、证据截图）默认放在用户目录下，
不放在仓库里，避免误提交登录凭据：

- Windows: %LOCALAPPDATA%\\multipub
- 其他:    $XDG_DATA_HOME/multipub 或 ~/.local/share/multipub

可用环境变量 MULTIPUB_HOME 覆盖。目录下可选的 config.toml 见 docs/config.example.toml。
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "multipub"

# 各动作默认超时（秒）。上传大视频可能很久，按需在 config.toml 的 [timeouts] 里调大。
DEFAULT_TIMEOUTS: dict[str, int] = {
    "login": 300,
    "check": 180,
    "publish_video": 3600,
    "publish_note": 900,
    "publish_article": 900,
}


def default_home() -> Path:
    env = os.environ.get("MULTIPUB_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


@dataclass
class BrowserSettings:
    # chromium = patchright 自带的 Chromium（`patchright install chromium` 安装）；
    # chrome / msedge = 本机安装的浏览器（指纹更像真人，但 patchright 有时解析不到，可改填 executable_path）
    channel: str = "chromium"
    executable_path: str = ""
    # 发布默认无头；登录默认有头（需要扫码），由 CLI 的 --headed / --headless 覆盖
    headless: bool = True


@dataclass
class Settings:
    home: Path
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    # B站视频走 biliup 命令行；固定版本，避免"首次运行拿到的 latest 不一样"
    biliup_version: str = "v1.3.1"
    biliup_path: str = ""  # 手动指定 biliup.exe 路径时优先使用
    github_mirror: str = ""  # 例如 "https://ghfast.top/"，下载 biliup 时拼在 GitHub URL 前
    gap_seconds: int = 30  # 同一次 publish 里两个平台之间的间隔，降低风控
    timeouts: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    raw: dict[str, Any] = field(default_factory=dict)  # config.toml 原文，平台私有配置从这里取

    # ---- 派生路径 ----
    @property
    def accounts_dir(self) -> Path:
        return self.home / "accounts"

    @property
    def sau_home(self) -> Path:
        """vendored social-auto-upload 的 BASE_DIR（日志、verify_code.txt）。"""
        return self.home / "sau"

    @property
    def tools_dir(self) -> Path:
        return self.home / "tools"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def locks_dir(self) -> Path:
        return self.home / "locks"

    @property
    def qrcode_dir(self) -> Path:
        return self.home / "qrcode"

    @property
    def history_file(self) -> Path:
        return self.home / "history.jsonl"

    def account_file(self, platform: str, account: str) -> Path:
        return self.accounts_dir / platform / f"{account}.json"

    def platform_config(self, platform: str) -> dict[str, Any]:
        value = self.raw.get("platforms", {}).get(platform, {})
        return value if isinstance(value, dict) else {}

    def timeout_for(self, action: str) -> int:
        return int(self.timeouts.get(action, DEFAULT_TIMEOUTS.get(action, 900)))

    def ensure_dirs(self) -> None:
        for d in (self.home, self.accounts_dir, self.sau_home, self.tools_dir,
                  self.runs_dir, self.locks_dir, self.qrcode_dir):
            d.mkdir(parents=True, exist_ok=True)


class ConfigError(ValueError):
    pass


def _read_text_any(path: Path) -> str:
    """Windows 上常见：记事本带 BOM 的 UTF-8、PowerShell 5.1 写出的 UTF-16。"""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def load_settings(home: Path | str | None = None) -> Settings:
    home_path = Path(home).expanduser().resolve() if home else default_home()
    raw: dict[str, Any] = {}
    cfg = home_path / "config.toml"
    if cfg.is_file():
        try:
            raw = tomllib.loads(_read_text_any(cfg))
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{cfg} 编码无法识别，请另存为 UTF-8：{exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg} 格式错误：{exc}（Windows 路径请用单引号，如 'D:\\tools\\biliup.exe'）") from exc

    browser_raw = raw.get("browser", {}) or {}
    browser = BrowserSettings(
        channel=str(browser_raw.get("channel", "chromium")),
        executable_path=str(browser_raw.get("executable_path", "")),
        headless=bool(browser_raw.get("headless", True)),
    )
    timeouts = dict(DEFAULT_TIMEOUTS)
    timeouts.update({k: int(v) for k, v in (raw.get("timeouts", {}) or {}).items()})

    bili = (raw.get("platforms", {}) or {}).get("bilibili", {}) or {}
    return Settings(
        home=home_path,
        browser=browser,
        biliup_version=str(bili.get("biliup_version", "v1.3.1")),
        biliup_path=str(bili.get("biliup_path", "")),
        github_mirror=str(raw.get("github_mirror", "")),
        gap_seconds=int(raw.get("gap_seconds", 30)),
        timeouts=timeouts,
        raw=raw,
    )
