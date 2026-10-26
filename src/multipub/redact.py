"""日志脱敏：写进运行目录的日志常被发给别人帮忙排查，存盘前把等同密码的值遮掉。

worker 启动时用 RedactingStream 包住 sys.stderr，于是 ctx.log、loguru（抖音/小红书）、
biliup 输出、异常堆栈都经过这里。直接继承文件描述符的子进程（浏览器自身的输出）不经过。
"""

from __future__ import annotations

import re
from typing import Any, TextIO

MASK = "***"

# 登录凭据类的 cookie / 参数名：B站、知乎、抖音、小红书，以及通用的 token
SECRET_NAMES = (
    "SESSDATA", "bili_jct", "DedeUserID__ckMd5", "ac_time_value",
    "z_c0", "_xsrf", "d_c0", "q_c1", "KLBRSID",
    "sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt", "uid_tt_ss",
    "passport_csrf_token", "msToken", "ttwid",
    "web_session", "a1", "webId", "gid",
    "access_token", "refresh_token", "access_key", "csrf", "csrf_token",
)

# name=value / name: value / "name": "value"；值到空白、引号、分号、逗号、& 为止
_NAMED = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in SECRET_NAMES) + r")"
    r"""(["']?\s*[:=]\s*["']?)([^\s"';,&]+)""",
    re.IGNORECASE,
)
# 整个 Cookie / Authorization 头：值一直遮到行尾或引号
_HEADER = re.compile(r"""(?<![A-Za-z0-9_])(cookie|authorization)(["']?\s*[:=]\s*["']?)([^\r\n"']+)""",
                     re.IGNORECASE)


def redact(text: str) -> str:
    text = _HEADER.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    return _NAMED.sub(lambda m: m.group(1) + m.group(2) + MASK, text)


class RedactingStream:
    """包一层文本流，write 前脱敏；其余属性（flush、fileno、encoding……）原样转给底层流。

    按每次 write 处理、不做行缓冲：进程被杀时不会丢掉最后几行。print / loguru 都是整条消息一次写入，
    值不会被拆在两次 write 之间。"""

    def __init__(self, stream: TextIO):
        self._stream = stream

    def write(self, s: str) -> int:
        self._stream.write(redact(s))
        return len(s)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)
