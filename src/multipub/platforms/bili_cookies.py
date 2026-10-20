"""B站登录文件（biliup 格式）的读取、转换与写入。

登录文件就是 biliup 的 cookies.json，视频（biliup）和图文（浏览器 / HTTP）共用一份：

    {"cookie_info": {"cookies": [{"name": "SESSDATA", "value": "...", "http_only": 1,
                                  "expires": 1700000000, "secure": 0}, ...],
                     "domains": [".bilibili.com", ...]},
     "sso": [...],
     "token_info": {"mid": 1, "access_token": "...", "refresh_token": "...", "expires_in": 15552000},
     "platform": "BiliTV"}

规则：
- 读取用 utf-8-sig（Windows 工具常写 BOM）；写入不带 BOM（biliup 的 serde_json 不认 BOM）。
- 只有 TV/APP 登录得到的文件才有 token_info，biliup 上传必须要它。
- 浏览器上下文绝不回写这个文件（会丢掉 token_info），cookie 只在内存里转换。

本模块只用标准库，主进程可以放心导入。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COOKIE_DOMAIN = ".bilibili.com"
REQUIRED_COOKIES = ("SESSDATA", "bili_jct")


class CredentialError(ValueError):
    """登录文件缺失或格式不对。"""


@dataclass
class BiliCredential:
    path: Path
    raw: dict[str, Any]
    cookies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cookie_dict(self) -> dict[str, str]:
        return {str(c["name"]): str(c.get("value", "")) for c in self.cookies}

    @property
    def csrf(self) -> str:
        return self.cookie_dict.get("bili_jct", "")

    @property
    def token_info(self) -> dict[str, Any]:
        info = self.raw.get("token_info")
        return info if isinstance(info, dict) else {}

    @property
    def has_token(self) -> bool:
        """biliup 上传需要 TV/APP 登录得到的 access_token / refresh_token。"""
        info = self.token_info
        return bool(info.get("access_token") and info.get("refresh_token") and info.get("mid"))

    @property
    def mid(self) -> int | None:
        for value in (self.token_info.get("mid"), self.cookie_dict.get("DedeUserID")):
            try:
                if value:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def cookie_expires(self, name: str = "SESSDATA") -> int | None:
        for c in self.cookies:
            if c.get("name") == name:
                try:
                    exp = int(c.get("expires") or 0)
                except (TypeError, ValueError):
                    return None
                return exp if exp > 0 else None
        return None

    def days_left(self, now: float | None = None) -> float | None:
        exp = self.cookie_expires()
        if exp is None:
            return None
        return (exp - (now if now is not None else time.time())) / 86400


def load_credential(path: Path) -> BiliCredential:
    """读取登录文件。兼容 biliup 格式（cookie_info.cookies）和 Playwright storage_state（cookies）。"""
    if not path.is_file():
        raise CredentialError(f"登录文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError(f"登录文件无法解析: {path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"登录文件格式不对: {path}")
    info = data.get("cookie_info")
    raw_cookies = info.get("cookies") if isinstance(info, dict) else data.get("cookies")
    if not isinstance(raw_cookies, list):
        raise CredentialError(f"登录文件里没有 cookie: {path}")
    cookies = [c for c in raw_cookies
               if isinstance(c, dict) and c.get("name") and isinstance(c.get("value"), (str, int))]
    cred = BiliCredential(path=path, raw=data, cookies=cookies)
    missing = [n for n in REQUIRED_COOKIES if not cred.cookie_dict.get(n)]
    if missing:
        raise CredentialError(f"登录文件缺少 cookie {', '.join(missing)}: {path}")
    return cred


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def to_playwright_cookie(c: dict[str, Any]) -> dict[str, Any]:
    """biliup / API 返回的 cookie -> Playwright cookie（int 的 secure/http_only 转成 bool）。"""
    try:
        expires = float(c.get("expires") or -1)
    except (TypeError, ValueError):
        expires = -1
    same_site = str(c.get("sameSite") or "Lax")
    if same_site not in ("Strict", "Lax", "None"):
        same_site = "Lax"
    secure = _bool(c.get("secure"), True)
    if same_site == "None":
        secure = True  # Chrome 要求 SameSite=None 必须 Secure
    return {
        "name": str(c["name"]),
        "value": str(c.get("value", "")),
        "domain": str(c.get("domain") or COOKIE_DOMAIN),
        "path": str(c.get("path") or "/"),
        "expires": expires if expires > 0 else -1,
        "httpOnly": _bool(c.get("httpOnly", c.get("http_only")), False),
        "secure": secure,
        "sameSite": same_site,
    }


def to_storage_state(cred: BiliCredential, extra: dict[str, str] | None = None) -> dict[str, Any]:
    """在内存里生成 Playwright storage_state；extra 是补充的 cookie（如 buvid3）。"""
    cookies = [to_playwright_cookie(c) for c in cred.cookies]
    names = {c["name"] for c in cookies}
    year = time.time() + 365 * 86400
    for name, value in (extra or {}).items():
        if name not in names:
            cookies.append(to_playwright_cookie({"name": name, "value": value, "expires": year,
                                                 "http_only": 0, "secure": 0}))
    return {"cookies": cookies, "origins": []}


def login_info_from_tv_poll(data: dict[str, Any]) -> dict[str, Any]:
    """TV 扫码登录成功的 data -> biliup LoginInfo（字段与 biliup 保存的一致）。"""
    token = data.get("token_info") or {k: data.get(k) for k in
                                         ("mid", "access_token", "refresh_token", "expires_in")}
    cookie_info = data.get("cookie_info")
    if not isinstance(cookie_info, dict) or not cookie_info.get("cookies"):
        raise CredentialError("扫码登录返回的数据里没有 cookie_info")
    if not (token.get("access_token") and token.get("refresh_token") and token.get("mid")):
        raise CredentialError("扫码登录返回的数据里没有 token_info")
    return {
        "cookie_info": cookie_info,
        "sso": list(data.get("sso") or []),
        "token_info": {
            "access_token": str(token["access_token"]),
            "expires_in": int(token.get("expires_in") or 0),
            "mid": int(token["mid"]),
            "refresh_token": str(token["refresh_token"]),
        },
        "platform": "BiliTV",
    }


def write_login_info(path: Path, info: dict[str, Any]) -> None:
    """原子写入登录文件：UTF-8 无 BOM，POSIX 上权限 600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(info, ensure_ascii=False, indent=2) + "\n"
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
