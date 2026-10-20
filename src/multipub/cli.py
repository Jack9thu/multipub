"""命令行入口：mpub。

常用：
    mpub doctor                               # 环境自检
    mpub setup                                # 安装浏览器内核、下载 biliup
    mpub login douyin --account main          # 扫码登录（默认弹出浏览器窗口）
    mpub check --all                          # 检查所有已登录账号是否有效
    mpub new posts/ep12 --kind video          # 生成 post.yaml 模板
    mpub publish posts/ep12 --dry-run         # 只校验不发布
    mpub publish posts/ep12 --to bilibili,douyin --yes --json

退出码：0 全部成功；1 有失败；2 参数/清单错误；3 需要人处理（登录过期、验证码）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, history
from .config import Settings, load_settings
from .manifest import (PLATFORMS, ManifestError, common_issues, load_manifest,
                       normalize_platform, parse_platform_list)
from .models import (ErrorCode, Issue, Kind, PlatformPost, Result, Status,
                     now_iso)
from .platforms import DISPLAY_NAMES, available, load_adapter
from .runner import run_worker

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NEEDS_ACTION = 0, 1, 2, 3


# ------------------------------------------------------------------ output

class Printer:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def info(self, msg: str) -> None:
        """进度信息一律写 stderr；--json 模式下 stdout 只留最终 JSON。"""
        print(msg, file=sys.stderr, flush=True)

    def event(self, event: dict[str, Any]) -> None:
        if self.as_json:
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
            return
        who = f"[{DISPLAY_NAMES.get(event.get('platform', ''), event.get('platform', ''))}/{event.get('account', '')}]"
        kind = event.get("event")
        if kind == "qrcode":
            self.info(f"{who} 请用手机 App 扫码登录，二维码图片: {event.get('path')} {event.get('note') or ''}")
        elif kind == "needs_input":
            self.info(f"{who} 需要输入: {event.get('message')}"
                      + (f"（写入文件 {event['path']}，或运行 mpub verify-code）" if event.get("path") else ""))
        elif kind == "submit_started":
            self.info(f"{who} 已点击发布……")
        elif kind == "log":
            if event.get("level") != "debug":
                self.info(f"{who} {event.get('message')}")
        else:
            self.info(f"{who} {json.dumps(event, ensure_ascii=False)}")

    def emit_json(self, payload: dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


STATUS_ZH = {
    Status.OK: "正常", Status.PUBLISHED: "已发布", Status.SUBMITTED: "已提交",
    Status.SCHEDULED: "已定时", Status.DRAFT: "已存草稿", Status.DRY_RUN: "校验通过",
    Status.SKIPPED: "跳过", Status.NEEDS_ACTION: "需要处理", Status.FAILED: "失败",
}


def format_result(r: Result) -> str:
    name = DISPLAY_NAMES.get(r.platform, r.platform)
    line = f"  {'✔' if r.ok else '✘'} {name}/{r.account}: {STATUS_ZH.get(r.status, r.status.value)}"
    if r.url:
        line += f"  {r.url}"
    if r.message:
        line += f"  — {r.message}"
    return line


def exit_code_for(results: list[Result]) -> int:
    if any(r.status is Status.FAILED for r in results):
        return EXIT_FAILED
    if any(r.status is Status.NEEDS_ACTION for r in results):
        return EXIT_NEEDS_ACTION
    return EXIT_OK


def _run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _accounts(settings: Settings, platform: str) -> list[str]:
    d = settings.accounts_dir / platform
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def _skip(post: PlatformPost, message: str, code: ErrorCode = ErrorCode.UNSUPPORTED) -> Result:
    return Result(platform=post.platform, account=post.account, action="publish",
                  status=Status.SKIPPED, kind=post.kind.value, message=message,
                  error_code=code.value, finished_at=now_iso())


# ------------------------------------------------------------------ commands

def cmd_doctor(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    checks: list[dict[str, Any]] = []
    checks.append({"name": "python", "ok": sys.version_info >= (3, 11), "detail": sys.version.split()[0]})
    checks.append({"name": "data_home", "ok": True, "detail": str(settings.home)})
    try:
        import patchright  # noqa: F401
        from importlib.metadata import version
        checks.append({"name": "patchright", "ok": True, "detail": version("patchright")})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "patchright", "ok": False, "detail": f"未安装: {exc}"})
    browser_ok, browser_detail = _browser_status(settings)
    checks.append({"name": "browser", "ok": browser_ok, "detail": browser_detail})
    for name in PLATFORMS:
        cls = load_adapter(name)
        for item in cls.doctor(settings):
            checks.append({"name": f"{name}.{item['name']}", "ok": item["ok"], "detail": item.get("detail", "")})
        accts = _accounts(settings, name)
        checks.append({"name": f"{name}.accounts", "ok": bool(accts),
                       "detail": ", ".join(accts) if accts else "未登录（mpub login %s --account <名字>）" % name})
    if out.as_json:
        out.emit_json({"ok": all(c["ok"] for c in checks if not c["name"].endswith(".accounts")), "checks": checks})
    else:
        for c in checks:
            print(f"  {'✔' if c['ok'] else '✘'} {c['name']}: {c['detail']}")
    core_ok = all(c["ok"] for c in checks if not c["name"].endswith(".accounts"))
    return EXIT_OK if core_ok else EXIT_FAILED


def _browser_status(settings: Settings) -> tuple[bool, str]:
    b = settings.browser
    if b.executable_path:
        ok = Path(b.executable_path).is_file()
        return ok, f"executable_path={b.executable_path}" + ("" if ok else "（文件不存在）")
    if b.channel in ("chrome", "msedge"):
        return True, f"channel={b.channel}（使用本机已安装的浏览器）"
    try:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        ok = Path(exe).exists()
        return ok, exe if ok else f"未安装 Chromium，运行: mpub setup（或 patchright install chromium）"
    except Exception as exc:  # noqa: BLE001
        return False, f"无法检测: {exc}"


def cmd_setup(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    settings.ensure_dirs()
    rc = EXIT_OK
    if not args.skip_browser and settings.browser.channel == "chromium" and not settings.browser.executable_path:
        out.info("安装 patchright Chromium（国内网络慢可先设置 PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright）……")
        proc = subprocess.run([sys.executable, "-m", "patchright", "install", "chromium"])
        if proc.returncode != 0:
            out.info("Chromium 安装失败")
            rc = EXIT_FAILED
    for name in PLATFORMS:
        for item in load_adapter(name).setup(settings):
            out.info(f"  {'✔' if item['ok'] else '✘'} {name}.{item['name']}: {item.get('detail', '')}")
            if not item["ok"]:
                rc = EXIT_FAILED
    return rc


def cmd_login(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    settings.ensure_dirs()
    platform = normalize_platform(args.platform)
    cls = load_adapter(platform)
    if cls.interactive_login:
        return cls.run_interactive_login(settings, args.account)
    headless = bool(args.headless)  # 登录默认有头，方便直接在浏览器窗口里扫码
    with history.account_lock(settings, platform, args.account):
        result = run_worker(settings, action="login", platform=platform, account=args.account,
                            headless=headless, run_dir=settings.runs_dir / f"{_run_id()}-login",
                            on_event=out.event)
    if out.as_json:
        out.emit_json({"ok": result.ok, "result": result.to_dict()})
    else:
        print(format_result(result))
    return exit_code_for([result])


def cmd_check(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    settings.ensure_dirs()
    platforms = parse_platform_list(args.platforms) or list(PLATFORMS)
    run_dir = settings.runs_dir / f"{_run_id()}-check"
    results: list[Result] = []
    for platform in platforms:
        accounts = [args.account] if args.account else _accounts(settings, platform)
        if not accounts:
            results.append(Result(platform=platform, account="-", action="check", status=Status.NEEDS_ACTION,
                                  message=f"没有已登录账号，运行 mpub login {platform} --account <名字>",
                                  error_code=ErrorCode.LOGIN_REQUIRED.value, finished_at=now_iso()))
            continue
        for account in accounts:
            with history.account_lock(settings, platform, account):
                results.append(run_worker(settings, action="check", platform=platform, account=account,
                                          headless=True, run_dir=run_dir, on_event=out.event))
    if out.as_json:
        out.emit_json({"ok": all(r.ok for r in results), "results": [r.to_dict() for r in results]})
    else:
        for r in results:
            print(format_result(r))
    return exit_code_for(results)


def cmd_accounts(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    rows = []
    for platform in PLATFORMS:
        for account in _accounts(settings, platform):
            f = settings.account_file(platform, account)
            rows.append({"platform": platform, "account": account, "file": str(f),
                         "updated_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")})
    if out.as_json:
        out.emit_json({"accounts": rows})
    elif not rows:
        print("还没有登录任何账号。示例: mpub login douyin --account main")
    else:
        for r in rows:
            print(f"  {DISPLAY_NAMES[r['platform']]}/{r['account']}  更新于 {r['updated_at']}  {r['file']}")
    return EXIT_OK


TEMPLATES = Path(__file__).parent / "templates"


def cmd_new(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    target = Path(args.dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    dest = target / "post.yaml"
    if dest.exists() and not args.force:
        out.info(f"{dest} 已存在（--force 覆盖）")
        return EXIT_USAGE
    shutil.copyfile(TEMPLATES / f"{args.kind}.yaml", dest)
    if args.kind == "article":
        md = target / "article.md"
        if not md.exists():
            md.write_text("# 标题会被忽略，以 post.yaml 的 title 为准\n\n正文，支持 **Markdown** 和本地图片：\n\n![说明](images/1.jpg)\n",
                          encoding="utf-8")
    print(str(dest))
    return EXIT_OK


def _collect(args: argparse.Namespace) -> tuple[Any, list[PlatformPost], list[Result], list[Issue]]:
    manifest = load_manifest(args.post)
    targets = manifest.targets(parse_platform_list(args.to))
    if not targets:
        raise ManifestError("没有目标平台：在 post.yaml 的 platforms 里列出，或用 --to bilibili,douyin")
    posts: list[PlatformPost] = []
    skipped: list[Result] = []
    issues: list[Issue] = []
    for platform in targets:
        post = manifest.for_platform(platform, args.account)
        cls = load_adapter(platform)
        if post.kind not in cls.kinds:
            supported = "/".join(k.value for k in cls.kinds)
            skipped.append(_skip(post, f"{DISPLAY_NAMES[platform]}暂不支持 {post.kind.value}（支持: {supported}）"))
            continue
        issues.extend(common_issues(post))
        issues.extend(cls.validate(post))
        posts.append(post)
    return manifest, posts, skipped, issues


def cmd_validate(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    manifest, posts, skipped, issues = _collect(args)
    errors = [i for i in issues if i.level == "error"]
    if out.as_json:
        out.emit_json({
            "ok": not errors,
            "kind": manifest.kind.value,
            "posts": [p.to_dict() for p in posts],
            "skipped": [r.to_dict() for r in skipped],
            "issues": [i.__dict__ for i in issues],
        })
    else:
        print(f"内容类型: {manifest.kind.value}，目标: {', '.join(DISPLAY_NAMES[p.platform] + '/' + p.account for p in posts) or '无'}")
        for r in skipped:
            print(format_result(r))
        for i in issues:
            print(f"  {'✘' if i.level == 'error' else '!'} {i}")
        if not errors:
            print("  ✔ 校验通过")
    return EXIT_USAGE if errors else EXIT_OK


def cmd_publish(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    manifest, posts, skipped, issues = _collect(args)
    errors = [i for i in issues if i.level == "error"]
    for i in issues:
        out.info(f"  {'✘' if i.level == 'error' else '!'} {i}")
    if errors:
        if out.as_json:
            out.emit_json({"ok": False, "error": "validation", "issues": [i.__dict__ for i in issues]})
        return EXIT_USAGE

    results: list[Result] = list(skipped)
    run_id = _run_id()

    if args.dry_run:
        for post in posts:
            results.append(Result(platform=post.platform, account=post.account, action="publish",
                                  status=Status.DRY_RUN, kind=post.kind.value,
                                  message="本地校验通过（未联网、未发布）", finished_at=now_iso()))
        return _finish(out, results, run_id, posts)

    # 查重：同一内容已经发过（或结果未知）的平台默认跳过
    pending: list[PlatformPost] = []
    for post in posts:
        prev = None if args.force else history.find_previous(settings, history.post_fingerprint(post))
        if prev:
            when = prev.get("finished_at", "")
            results.append(_skip(post, f"同一内容已于 {when} 发过（状态 {prev.get('status')}"
                                       f"{'，结果未知' if prev.get('error_code') == ErrorCode.UNKNOWN_OUTCOME.value else ''}）；"
                                       "确认要再发请加 --force", ErrorCode.VALIDATION))
        else:
            pending.append(post)

    if pending and not args.yes:
        if not sys.stdin.isatty():
            out.info("非交互环境需要加 --yes 才会真正发布（建议先 --dry-run 确认）")
            return EXIT_USAGE
        names = "、".join(f"{DISPLAY_NAMES[p.platform]}/{p.account}" for p in pending)
        answer = input(f"即将发布「{pending[0].title or manifest.path.parent.name}」到 {names}，确认? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            out.info("已取消")
            return EXIT_USAGE

    settings.ensure_dirs()
    run_dir = settings.runs_dir / run_id
    gap = settings.gap_seconds if args.gap is None else args.gap
    for index, post in enumerate(pending):
        if index > 0 and gap > 0:
            out.info(f"等待 {gap} 秒后发布下一个平台……")
            time.sleep(gap)
        out.info(f"→ 发布到 {DISPLAY_NAMES[post.platform]}/{post.account} ……")
        try:
            with history.account_lock(settings, post.platform, post.account):
                result = run_worker(settings, action="publish", platform=post.platform, account=post.account,
                                    headless=not args.headed and settings.browser.headless,
                                    run_dir=run_dir / post.platform, post=post,
                                    timeout=args.timeout, on_event=out.event)
        except RuntimeError as exc:  # 账号被占用
            result = Result(platform=post.platform, account=post.account, action="publish",
                            status=Status.FAILED, kind=post.kind.value, message=str(exc),
                            error_code=ErrorCode.INTERNAL.value, retryable=True, finished_at=now_iso())
        history.append(settings, result, run_id=run_id, fingerprint=history.post_fingerprint(post),
                       source=str(manifest.path), title=post.title)
        if not out.as_json:
            print(format_result(result))
        results.append(result)
    return _finish(out, results, run_id, posts, printed=not out.as_json)


def _finish(out: Printer, results: list[Result], run_id: str, posts: list[PlatformPost],
            printed: bool = False) -> int:
    code = exit_code_for(results)
    if out.as_json:
        out.emit_json({"ok": code == EXIT_OK, "run_id": run_id, "results": [r.to_dict() for r in results]})
    else:
        if not printed:
            for r in results:
                print(format_result(r))
        else:
            for r in results:
                if r.status in (Status.SKIPPED,):
                    print(format_result(r))
    return code


def cmd_history(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    rows = history.read(settings, args.limit)
    if out.as_json:
        out.emit_json({"history": rows})
    else:
        for r in rows:
            print(f"  {r.get('finished_at', '')}  {DISPLAY_NAMES.get(r.get('platform'), r.get('platform'))}/{r.get('account')}"
                  f"  {r.get('status')}  {r.get('title', '')}  {r.get('url') or ''}  {r.get('message') or ''}")
    return EXIT_OK


def cmd_verify_code(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    platform = normalize_platform(args.platform)
    if platform != "douyin":
        out.info("目前只有抖音发布时可能需要短信验证码")
        return EXIT_USAGE
    path = settings.sau_home / "verify_code.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args.code.strip(), encoding="utf-8")
    print(str(path))
    return EXIT_OK


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="数据目录（默认 %%LOCALAPPDATA%%\\multipub，或环境变量 MULTIPUB_HOME）")
    common.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON 结果（给脚本 / Claude Code 用）")

    # --home / --json 只挂在子命令上（挂在主解析器上会被子命令的默认值覆盖）
    p = argparse.ArgumentParser(prog="mpub", description="多平台发布：B站 / 抖音 / 小红书 / 知乎")
    p.add_argument("--version", action="version", version=f"multipub {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[common], help="环境自检")

    s = sub.add_parser("setup", parents=[common], help="安装浏览器内核、下载 biliup")
    s.add_argument("--skip-browser", action="store_true")

    s = sub.add_parser("login", parents=[common], help="扫码登录某个平台账号")
    s.add_argument("platform")
    s.add_argument("--account", default="default")
    s.add_argument("--headless", action="store_true", help="无头登录（只输出二维码图片，不弹窗口）")

    s = sub.add_parser("check", parents=[common], help="检查账号登录是否有效")
    s.add_argument("platforms", nargs="*", help="平台，省略表示全部")
    s.add_argument("--all", action="store_true", help="同省略平台")
    s.add_argument("--account")

    sub.add_parser("accounts", parents=[common], help="列出已登录账号")

    s = sub.add_parser("new", parents=[common], help="生成 post.yaml 模板")
    s.add_argument("dir")
    s.add_argument("--kind", choices=[k.value for k in Kind], required=True)
    s.add_argument("--force", action="store_true")

    for name, help_text in (("validate", "只校验清单（不联网）"), ("publish", "发布")):
        s = sub.add_parser(name, parents=[common], help=help_text)
        s.add_argument("post", help="post.yaml 或其所在目录")
        s.add_argument("--to", help="目标平台，逗号分隔；省略则用清单里的 platforms")
        s.add_argument("--account", help="所有平台统一使用的账号名（覆盖清单）")
        if name == "publish":
            s.add_argument("--dry-run", action="store_true", help="只做本地校验")
            s.add_argument("--yes", "-y", action="store_true", help="不再询问确认")
            s.add_argument("--headed", action="store_true", help="显示浏览器窗口")
            s.add_argument("--gap", type=int, help="平台之间间隔秒数（默认 30）")
            s.add_argument("--timeout", type=int, help="单个平台超时秒数")
            s.add_argument("--force", action="store_true", help="忽略查重，强制再发一次")

    s = sub.add_parser("history", parents=[common], help="查看发布记录")
    s.add_argument("--limit", type=int, default=30)

    s = sub.add_parser("verify-code", parents=[common], help="提交抖音发布时收到的短信验证码")
    s.add_argument("platform")
    s.add_argument("code")
    return p


COMMANDS = {
    "doctor": cmd_doctor, "setup": cmd_setup, "login": cmd_login, "check": cmd_check,
    "accounts": cmd_accounts, "new": cmd_new, "validate": cmd_validate, "publish": cmd_publish,
    "history": cmd_history, "verify-code": cmd_verify_code,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    out = Printer(args.json)
    settings = load_settings(args.home)
    try:
        return COMMANDS[args.command](args, settings, out)
    except ManifestError as exc:
        if out.as_json:
            out.emit_json({"ok": False, "error": "manifest", "message": str(exc)})
        else:
            out.info(f"清单错误: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        out.info("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
