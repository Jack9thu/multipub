"""仿 biliup v1.3.1 命令行（模拟测试用）。

由测试生成的启动脚本以 `python -I fake_biliup.py <参数>` 运行（不导入仓库代码、不联网）。
场景参数读环境变量 MULTIPUB_SIM_BILI（JSON，键见 server.DEFAULTS 里 biliup* 开头的项），
收到的参数和登录文件检查结果写到 MULTIPUB_SIM_BILI_LOG。

输出格式照 biliup v1.3.1：
- tracing 默认格式（NO_COLOR）"<时间>  INFO <target>: <消息>"，Linux 多线程取不到本地时区时时间是 "<unknown time>"；
- stdout 不是终端时 indicatif 进度条不显示，所以上传期间没有输出；
- 出错时 main 打印 error-stack 报告（带 ANSI 粗体/斜体，NO_COLOR 不影响它）到 stderr，退出码 1；
- 参数解析照 clap：全局参数（--rust-log、-u/--user-cookie、-p/--proxy）只能写在子命令前面，
  未知参数退出码 2；登录文件按 serde 反序列化 LoginInfo（cookie_info、sso、token_info 必须有）。
"""

from __future__ import annotations

import json
import os
import sys
import time

DEFAULTS = {"biliup": "ok", "biliup_version": "ok", "biliup_upload_seconds": 1.0, "biliup_time": "unknown",
            "biliup_renew": "ok"}
BVID = "BV1Sm4y1c7Qa"
AID = 115404857754321
UPLOADER = "biliup_cli::uploader"
BILIBILI = "biliup::uploader::bilibili"
LINES = ("bldsa", "cnbldsa", "andsa", "atdsa", "bda2", "cnbd", "anbd", "atbd", "tx", "cntx", "antx", "attx", "txa",
         "alia", "estx", "akbd")
SUBMITS = ("app", "web", "b-cut-android")
UPLOAD_VALUE_OPTS = {"--title", "--desc", "--tid", "--tag", "--copyright", "--source", "--cover", "--dtime",
                     "--dynamic", "--tid-v2", "--no-reprint", "--is-only-self", "--extra-fields", "--line", "-l",
                     "--limit", "--submit", "--interactive", "--mission-id", "--dolby", "--hires", "--charging-pay",
                     "--config", "-c"}
UPLOAD_FLAGS = {"--up-selection-reply", "--up-close-reply", "--up-close-danmu"}
INT_LIMITS = {"--tid": 65535, "--copyright": 255, "--dtime": 2**32 - 1, "--tid-v2": 2**32 - 1, "--no-reprint": 255,
              "--is-only-self": 255, "--limit": 2**63, "--interactive": 255, "--dolby": 255, "--hires": 255,
              "--charging-pay": 255, "--mission-id": 2**32 - 1}


def cfg() -> dict:
    out = dict(DEFAULTS)
    raw = os.environ.get("MULTIPUB_SIM_BILI", "")
    if raw:
        out.update({k: v for k, v in json.loads(raw).items() if k.startswith("biliup")})
    return out


def log(kind: str, **data) -> None:
    path = os.environ.get("MULTIPUB_SIM_BILI_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": kind, "t": round(time.time(), 3), "pid": os.getpid(), **data},
                                ensure_ascii=False) + "\n")


C = cfg()


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S") if C["biliup_time"] == "local" else "<unknown time>"


def info(target: str, msg: str) -> None:
    sys.stdout.write(f"{ts()}  INFO {target}: {msg}\n")
    sys.stdout.flush()


def warn(target: str, msg: str) -> None:
    sys.stdout.write(f"{ts()}  WARN {target}: {msg}\n")
    sys.stdout.flush()


def report(chain: list[str], location: str = "crates/biliup-cli/src/uploader.rs:403:31",
           head: str = "Unknown Error") -> None:
    """error-stack 的报告格式（main 返回 Err 时打印），退出码 1。"""
    b, nb, i, ni = "\x1b[1m", "\x1b[22m", "\x1b[3m", "\x1b[23m"
    out = [f"Error: {b}{head}{nb}", f"├╴at {i}{location}{ni}", "│"]
    for n, item in enumerate(chain):
        last = n == len(chain) - 1
        out.append(f"{'╰' if last else '├'}─▶ {b}{item}{nb}")
        if last:
            out += [f"    ├╴at {i}{location}{ni}", "    ╰╴backtrace (1)"]
        else:
            out += [f"│   ╰╴at {i}{location}{ni}", "│"]
    out += ["", "━" * 40, "", "backtrace no. 1", "   0: <unknown>", "   1: __libc_start_main_impl", ""]
    sys.stderr.write("\n".join(out))
    sys.stderr.flush()
    sys.exit(1)


def clap_error(msg: str) -> None:
    sys.stderr.write(f"error: {msg}\n\nUsage: biliup [OPTIONS] <COMMAND>\n\nFor more information, try '--help'.\n")
    sys.exit(2)


def hang() -> None:
    while True:
        time.sleep(3600)


def take_value(argv: list[str], i: int) -> tuple[str, str, int]:
    arg = argv[i]
    if "=" in arg and arg.startswith("--"):
        name, value = arg.split("=", 1)
        return name, value, i + 1
    if i + 1 >= len(argv):
        clap_error(f"a value is required for '{arg} <VALUE>' but none was supplied")
    value = argv[i + 1]
    if value.startswith("-") and len(value) > 1:  # clap 不把以 - 开头的下一个参数当成值
        clap_error(f"a value is required for '{arg} <VALUE>' but none was supplied")
    return arg, value, i + 2


def load_login_info(path: str) -> dict:
    if not os.path.isfile(path):
        report(["No such file or directory (os error 2)"], location="crates/biliup-cli/src/uploader.rs:399:36",
               head=f"open cookies file: {path}")
    raw = open(path, "rb").read()
    try:
        data = json.loads(raw.decode("utf-8"))  # serde_json：不认 BOM
    except (UnicodeDecodeError, ValueError) as exc:
        report([f"expected value at line 1 column 1 ({type(exc).__name__})"])
    for key in ("cookie_info", "sso", "token_info"):
        if key not in data:
            report([f"missing field `{key}` at line 1 column {len(raw)}"])
    if not isinstance(data["sso"], list) or not all(isinstance(x, str) for x in data["sso"]):
        report(["invalid type: expected a sequence for `sso`"])
    token = data["token_info"]
    for key, typ in (("access_token", str), ("expires_in", int), ("mid", int), ("refresh_token", str)):
        if key not in token:
            report([f"missing field `{key}` at line 1 column {len(raw)}"])
        if not isinstance(token[key], typ) or isinstance(token[key], bool):
            report([f"invalid type: field `{key}` of token_info"])
    if token["expires_in"] < 0 or token["expires_in"] > 2**32 - 1 or token["mid"] < 0:
        report(["invalid value: integer out of range for token_info"])
    if data.get("platform") not in (None, "BiliTV", "Android"):
        report([f"未知平台 {data.get('platform')}"])
    cookies = (data["cookie_info"] or {}).get("cookies")
    if not isinstance(cookies, list):
        report(["credential cookies must be an array"])
    for n, c in enumerate(cookies):
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            report([f"credential cookie {n} has no name"])
        if not isinstance(c.get("value"), str):
            report([f"credential cookie {n} has no string value"])
    return data


def write_login_info(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def cmd_version() -> None:
    mode = C["biliup_version"]
    log("biliup", argv=sys.argv[1:], cmd="version")
    if mode == "hang":
        hang()
    if mode == "fail":
        sys.stderr.write("biliup: error while loading shared libraries: libsim.so.1: cannot open shared object file\n")
        sys.exit(127)
    print("biliup-cli 1.3.1")
    sys.exit(0)


def cmd_renew(cookie: str) -> None:
    data = load_login_info(cookie)
    log("biliup", argv=sys.argv[1:], cmd="renew", cwd=os.getcwd())
    if C["biliup_renew"] != "ok":
        report(["credential refresh failed with Bilibili code -101"])
    for c in data["cookie_info"]["cookies"]:
        if c["name"] == "SESSDATA":
            c["value"] = "renewed-sessdata"
    data["token_info"]["access_token"] = "renewed-access"
    data["token_info"]["refresh_token"] = "renewed-refresh"
    write_login_info(cookie, data)
    info("biliup::uploader::credential", "更新cookie")
    info(UPLOADER, "登录凭据刷新成功")
    sys.exit(0)


def cmd_upload(cookie: str, argv: list[str]) -> None:
    opts: dict[str, str] = {}
    flags: list[str] = []
    videos: list[str] = []
    i = 0
    positional_only = False
    while i < len(argv):
        arg = argv[i]
        if positional_only or not arg.startswith("-") or arg == "-":
            videos.append(arg)
            i += 1
            continue
        if arg == "--":
            positional_only = True
            i += 1
            continue
        name = arg.split("=", 1)[0]
        if name in UPLOAD_FLAGS:
            if "=" in arg:
                clap_error(f"unexpected value for '{name}'")
            flags.append(name)
            i += 1
            continue
        if name not in UPLOAD_VALUE_OPTS:
            clap_error(f"unexpected argument '{arg}' found")
        name, value, i = take_value(argv, i)
        if name in opts:
            clap_error(f"the argument '{name} <{name.strip('-').upper()}>' cannot be used multiple times")
        opts[name] = value
    for name, limit in INT_LIMITS.items():
        if name in opts:
            v = opts[name]
            if not v.isdigit() or int(v) > limit:
                clap_error(f"invalid value '{v}' for '{name} <{name.strip('-').upper()}>': number too large or invalid")
    line = opts.get("--line", opts.get("-l"))
    if line is not None and line not in LINES:
        clap_error(f"invalid value '{line}' for '--line <LINE>'")
    if "--submit" in opts and opts["--submit"] not in SUBMITS:
        clap_error(f"invalid value '{opts['--submit']}' for '--submit <SUBMIT>'")
    if "--extra-fields" in opts:
        try:
            extra = json.loads(opts["--extra-fields"])
            assert isinstance(extra, dict)
        except (ValueError, AssertionError):
            clap_error(f"invalid value '{opts['--extra-fields']}' for '--extra-fields <EXTRA_FIELDS>'")
    log("biliup", argv=sys.argv[1:], cmd="upload", opts=opts, flags=flags, videos=videos, cwd=os.getcwd(),
        no_color=os.environ.get("NO_COLOR"))
    if not videos:
        report(["No video files specified. Please provide at least one video file path."],
               location="crates/biliup-cli/src/uploader.rs:177:20", head="No video files specified. Please provide "
                                                                     "at least one video file path.")
    data = load_login_info(cookie)
    mode = C["biliup"]
    if mode == "network_error":
        report(["error sending request", "client error (Connect)", "tcp connect error",
                "Network is unreachable (os error 101)"])
    token = data["token_info"]["access_token"]
    if mode == "login_error" or token not in ("sim-access", "renewed-access", "tvqr-access"):
        report(["credential validation failed with Bilibili code -101"])
    info(UPLOADER, "user: 模拟UP主")
    cover = opts.get("--cover", "")
    if cover:
        if not os.path.isfile(os.path.expanduser(cover)):
            report([f"cover: {cover}", "No such file or directory (os error 2)"],
                   location="crates/biliup-cli/src/uploader.rs:425:56")
        info(UPLOADER, "https://archive.biliimg.com/bfs/archive/0123456789abcdef0123456789abcdef01234567.jpg")
    info(UPLOADER, f"number of concurrent futures: {opts.get('--limit', '3')}")
    info(UPLOADER, "No checkpoint found, starting fresh upload")
    for video in videos:
        if not os.path.isfile(video):
            report([f"file {video}", "No such file or directory (os error 2)"],
                   location="crates/biliup-cli/src/uploader.rs:503:66")
        name = os.path.basename(video)
        size = os.path.getsize(video)
        info(UPLOADER, 'Line { os: Upos, query: "probe_version=20221109&upcdn=bda2&zone=cs", '
                       'probe_url: "//upos-cs-upcdnbda2.bilivideo.com/OK", cost: 61 }')
        info("biliup::uploader::line::upos", "pre_upload: " + json.dumps(
            {"name": name, "r": "upos", "profile": "ugcfx/bup", "ssl": 0, "version": "2.14.0", "build": 2140000,
             "size": size}, ensure_ascii=False))
        if mode == "hang_upload":
            hang()
        time.sleep(float(C["biliup_upload_seconds"]))
        secs = max(float(C["biliup_upload_seconds"]), 0.01)
        info(UPLOADER, f"Upload completed: {name} => cost {secs:.2f}s, {size / 1000 / secs / 1000:.2f} MB/s.")
        info(UPLOADER, f"Checkpoint saved: {videos.index(video) + 1} files uploaded")
    info(UPLOADER, "All files uploaded successfully, checkpoint removed")
    log("biliup_uploaded")
    if mode == "crash_after_upload":
        sys.stderr.write("thread 'main' panicked at crates/biliup/src/uploader/bilibili.rs:530:14:\n"
                         "called `Result::unwrap()` on an `Err` value: reqwest::Error { kind: Decode }\n")
        sys.exit(101)
    if mode == "hang_after_upload":
        hang()
    submit = opts.get("--submit", "app")
    dtime = int(opts["--dtime"]) if "--dtime" in opts else None
    if mode == "reject" or (dtime is not None and dtime < time.time() + 7200):
        code, msg = (21070, "定时发布时间需晚于当前时间2小时") if mode != "reject" else (21012, "标题包含敏感词，请修改后重试")
        resp = f'ResponseData {{ code: {code}, data: None, message: "{msg}", ttl: Some(1) }}'
        info(BILIBILI, resp)
        report([resp], location="crates/biliup-cli/src/uploader.rs:205:14")
    if mode == "server_error":
        resp = 'ResponseData { code: -500, data: None, message: "服务器错误", ttl: Some(1) }'
        info(BILIBILI, resp)
        report([resp], location="crates/biliup-cli/src/uploader.rs:205:14")
    if mode == "no_bvid":
        info(BILIBILI, 'ResponseData { code: 0, data: Some(Null), message: "0", ttl: Some(1) }')
    else:
        info(BILIBILI, f'ResponseData {{ code: 0, data: Some(Object {{"aid": Number({AID}), "bvid": '
                       f'String("{BVID}")}}), message: "0", ttl: Some(1) }}')
    info(BILIBILI, {"app": "APP接口投稿成功", "web": "Web 接口投稿成功", "b-cut-android": "BCUT接口投稿成功"}[submit])
    log("biliup_submitted", bvid=None if mode == "no_bvid" else BVID)
    sys.exit(0)


TOP_HELP = """Upload video to bilibili.

Usage: biliup [OPTIONS] <COMMAND>

Commands:
  login     登录B站并保存登录信息
  renew     手动验证并刷新登录信息
  upload    上传视频
  append    是否要对某稿件追加视频
  show      打印视频详情
  dump-flv  输出flv元数据
  download  下载视频
  server    启动web服务，默认端口19159
  list      列出所有已上传的视频
  help      Print this message or the help of the given subcommand(s)

Options:
  -p, --proxy <PROXY>              配置代理
  -u, --user-cookie <USER_COOKIE>  登录信息文件 [default: cookies.json]
      --rust-log <RUST_LOG>        [default: tower_http=debug,info]
  -h, --help                       Print help
  -V, --version                    Print version
"""

UPLOAD_HELP = """上传视频

Usage: biliup upload [OPTIONS] [VIDEO_PATH]...

Arguments:
  [VIDEO_PATH]...  需要上传的视频路径,若指定配置文件投稿不需要此参数

Options:
  -c, --config <FILE>                  Sets a custom config file
  -l, --line <LINE>                    选择上传线路 [possible values: bldsa, cnbldsa, andsa, atdsa, bda2, cnbd, anbd, atbd, tx, cntx, antx, attx, txa, alia, estx, akbd]
      --limit <LIMIT>                  单视频文件最大并发数 [default: 3]
      --submit <SUBMIT>                提交接口 [default: app] [possible values: app, web, b-cut-android]
      --copyright <COPYRIGHT>          是否转载, 1-自制 2-转载 [default: 1]
      --source <SOURCE>                转载来源 [default: ]
      --tid <TID>                      投稿分区 [default: 171]
      --cover <COVER>                  视频封面 [default: ]
      --title <TITLE>                  视频标题 [default: ]
      --desc <DESC>                    视频简介 [default: ]
      --dynamic <DYNAMIC>              空间动态 [default: ]
      --tag <TAG>                      视频标签，逗号分隔多个tag [default: ]
      --dtime <DTIME>                  延时发布时间，距离提交大于4小时，格式为10位时间戳
      --interactive <INTERACTIVE>      [default: 0]
      --mission-id <MISSION_ID>
      --dolby <DOLBY>                  是否开启杜比音效, 0-关闭 1-开启 [default: 0]
      --hires <HIRES>                  是否开启 Hi-Res, 0-关闭 1-开启 [default: 0]
      --no-reprint <NO_REPRINT>        0-允许转载，1-禁止转载 [default: 0]
      --charging-pay <CHARGING_PAY>    是否开启充电面板, 0-关闭 1-开启 [default: 0]
      --up-selection-reply             是否开启精选评论，仅提交接口为app时可用
      --up-close-reply                 是否关闭评论，仅提交接口为app时可用
      --up-close-danmu                 是否关闭弹幕，仅提交接口为app时可用
      --is-only-self <IS_ONLY_SELF>    是否仅自己可见 [default: 0]
      --extra-fields <EXTRA_FIELDS>    自定义提交参数
      --tid-v2 <TID_V2>                新版分区 id
  -h, --help                           Print help
"""


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        cmd_version()
    if argv and argv[0] in ("--help", "-h", "help"):
        log("biliup", argv=argv, cmd="help")
        sys.stdout.write(TOP_HELP)
        sys.exit(0)
    cookie = "cookies.json"
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        name = argv[i].split("=", 1)[0]
        if name in ("--rust-log",):
            _, _, i = take_value(argv, i)
        elif name in ("-u", "--user-cookie"):
            _, cookie, i = take_value(argv, i)
        elif name in ("-p", "--proxy"):
            _, _, i = take_value(argv, i)
        else:
            clap_error(f"unexpected argument '{argv[i]}' found")
    if i >= len(argv):
        # 不带子命令：真 biliup 会起 Web 服务，这里直接报错让测试看到
        log("biliup", argv=sys.argv[1:], cmd="server")
        clap_error("'biliup' requires a subcommand but one was not provided")
    sub, rest = argv[i], argv[i + 1:]
    if sub == "upload" and rest[:1] in (["--help"], ["-h"]):
        log("biliup", argv=sys.argv[1:], cmd="help")
        sys.stdout.write(UPLOAD_HELP)
        sys.exit(0)
    if sub == "upload":
        cmd_upload(os.path.expanduser(cookie), rest)
    elif sub == "renew":
        if rest:
            clap_error(f"unexpected argument '{rest[0]}' found")
        cmd_renew(os.path.expanduser(cookie))
    else:
        log("biliup", argv=sys.argv[1:], cmd=sub)
        clap_error(f"unrecognized subcommand '{sub}'")


if __name__ == "__main__":
    main()
