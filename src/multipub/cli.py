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
from .config import ConfigError, Settings, load_settings
from .manifest import (PLATFORMS, ManifestError, common_issues, load_manifest,
                       normalize_platform, parse_platform_list)
from .models import (ErrorCode, Issue, Kind, PlatformPost, Result, Status,
                     now_iso)
from .platforms import DISPLAY_NAMES, available, load_adapter
from .runner import WorkerInterrupted, run_worker

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NEEDS_ACTION = 0, 1, 2, 3
EXIT_INTERRUPTED = 130


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
                      + (f"（运行 mpub verify-code douyin <验证码>，或写入文件 {event['path']}）" if event.get("path") else ""))
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


def _new_run_dir(settings: Settings, suffix: str = "") -> tuple[str, Path]:
    """新建本次运行的证据目录。同一秒里启动的两次运行不能共用目录（日志会互相覆盖）。"""
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    base = _run_id()
    for n in range(1, 1000):
        run_id = base if n == 1 else f"{base}-{n}"
        path = settings.runs_dir / f"{run_id}{suffix}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return run_id, path
    raise RuntimeError(f"无法在 {settings.runs_dir} 下创建运行目录")


def _accounts(settings: Settings, platform: str) -> list[str]:
    d = settings.accounts_dir / platform
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def _busy(platform: str, account: str, action: str, message: str, kind: str = "") -> Result:
    return Result(platform=platform, account=account, action=action, status=Status.FAILED, kind=kind,
                  message=message, error_code=ErrorCode.INTERNAL.value, retryable=True, finished_at=now_iso())


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
    steps: list[dict[str, Any]] = []
    if not args.skip_browser and settings.browser.channel == "chromium" and not settings.browser.executable_path:
        out.info("安装 patchright Chromium……（下载失败时可改用本机 Chrome：config.toml 里 [browser] channel = \"chrome\"，见 docs/windows.md）")
        # 子进程输出进度条；--json 时转到 stderr，保证 stdout 只有最终 JSON
        proc = subprocess.run([sys.executable, "-m", "patchright", "install", "chromium"],
                              stdout=sys.stderr if out.as_json else None)
        steps.append({"name": "browser", "ok": proc.returncode == 0,
                      "detail": "Chromium 已安装" if proc.returncode == 0 else f"Chromium 安装失败（退出码 {proc.returncode}）"})
    biliup_step = _setup_biliup_choice(args, settings, out)
    for name in PLATFORMS:
        if name == "bilibili" and biliup_step is not None:
            steps.append(biliup_step)  # 还没选 biliup 来源：先不下载，等用户选
            continue
        for item in load_adapter(name).setup(settings):
            steps.append({"name": f"{name}.{item['name']}", "ok": item["ok"], "detail": item.get("detail", "")})
    ok = all(s["ok"] for s in steps)
    if out.as_json:
        out.emit_json({"ok": ok, "steps": steps})
    else:
        for s in steps:
            print(f"  {'✔' if s['ok'] else '✘'} {s['name']}: {s['detail']}")
    return EXIT_OK if ok else EXIT_FAILED


def _setup_biliup_choice(args: argparse.Namespace, settings: Settings, out: Printer) -> dict[str, Any] | None:
    """mpub setup 里 biliup 的来源由用户决定。返回 None 表示已经有来源（或刚选好），照常执行 B站 setup；
    返回一个步骤表示还需要用户选择（非交互环境不擅自下载）。"""
    from .platforms import bili_locate

    if args.biliup:
        choice = args.biliup.strip()
        if choice in ("auto", "download"):
            ok, msg, _ = bili_locate.use(settings, mode=choice)
        else:
            ok, msg, _ = bili_locate.use(settings, choice)
        out.info(f"biliup：{msg}")
        return None if ok else {"name": "bilibili.biliup", "ok": False, "detail": msg}
    if not bili_locate.needs_choice(settings):
        return None
    cands = bili_locate.discover(settings)
    if not (sys.stdin.isatty() and not out.as_json):
        return {"name": "bilibili.biliup", "ok": False, "needs_choice": True,
                "candidates": [c.to_dict(i) for i, c in enumerate(cands, 1)],
                "detail": "还没选 biliup 来源（B站视频上传需要），没有下载。请选择："
                          "mpub setup --biliup <路径> / --biliup auto（用本机找到的兼容版本） / "
                          f"--biliup download（下载固定版本 {settings.biliup_version}）"}
    print("\nB站视频上传需要 biliup。", file=sys.stderr)
    if cands:
        print("本机找到这些 biliup：", file=sys.stderr)
        for i, c in enumerate(cands, 1):
            state = "兼容" if c.compatible else f"不兼容：{c.reason}"
            print(f"  [{i}] {c.path}（{c.version or '版本未知'}，{c.source}，{state}）", file=sys.stderr)
    usable = any(c.compatible for c in cands)
    print("怎么找到 biliup？\n  1) 我直接输入 biliup.exe 的路径"
          + ("\n  2) 用上面找到的（输入编号；直接回车 = 自动挑最合适的兼容版本）" if usable else "")
          + f"\n  3) 下载经过验证的固定版本 {settings.biliup_version}（约 40 MB）", file=sys.stderr)
    default = "2" if usable else "3"
    while True:
        try:
            ans = input(f"请选择 [{default}]: ").strip() or default
        except EOFError:
            ans = default
        if ans == "1":
            path = input("biliup 路径: ").strip()
            ok, msg, _ = bili_locate.use(settings, path)
        elif ans == "2" and usable:
            pick = input("编号（回车 = 自动）: ").strip()
            ok, msg, _ = bili_locate.use(settings, pick) if pick else bili_locate.use(settings, mode="auto")
        elif ans == "3":
            ok, msg, _ = bili_locate.use(settings, mode="download")
        else:
            continue
        print(f"  {msg}", file=sys.stderr)
        if ok:
            return None


def cmd_biliup(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    """mpub biliup find / use / status：选择 B站视频上传用哪个 biliup。"""
    from .platforms import bili_locate

    if args.biliup_cmd == "find":
        cands = bili_locate.discover(settings)
        res = bili_locate.resolve(settings)
        if out.as_json:
            out.emit_json({"ok": True, "in_use": res.to_dict(),
                           "candidates": [c.to_dict(i) for i, c in enumerate(cands, 1)]})
            return EXIT_OK
        print(f"当前使用：{res.detail}" + (f"（{res.problem}）" if res.problem else ""))
        if not cands:
            print("本机没找到 biliup（查找了 PATH、social-auto-upload 工具目录、下载文件夹、当前目录、multipub 下载目录）")
        for i, c in enumerate(cands, 1):
            state = "兼容" if c.compatible else f"不兼容：{c.reason}"
            print(f"  [{i}] {c.path}\n      {c.version or '版本未知'} · {c.source} · {state}")
            for ck in c.cookies:
                print(f"      旁边有登录文件 {ck}（可复制成 <数据目录>\\accounts\\bilibili\\<账号>.json 复用）")
        print("选择：mpub biliup use <编号或路径> / mpub biliup use --auto / mpub biliup use --download")
        return EXIT_OK
    if args.biliup_cmd == "use":
        if args.auto or args.download:
            ok, msg, res = bili_locate.use(settings, mode="auto" if args.auto else "download")
        else:
            ok, msg, res = bili_locate.use(settings, args.target, force=args.force)
        if out.as_json:
            out.emit_json({"ok": ok, "message": msg, "in_use": res.to_dict() if res else None})
        else:
            print(msg)
        return EXIT_OK if ok else EXIT_USAGE
    res = bili_locate.resolve(settings)  # status
    choice = bili_locate.read_choice(settings)
    if out.as_json:
        out.emit_json({**res.to_dict(), "choice": choice})
    else:
        print(f"{'✔' if res.ok else '✘'} {res.detail}" + (f"\n  {res.problem}" if res.problem else ""))
        print(f"  来源：{bili_locate.MODE_ZH.get(res.mode, res.mode)}")
    return EXIT_OK if res.ok else EXIT_FAILED


def cmd_login(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    settings.ensure_dirs()
    platform = normalize_platform(args.platform)
    cls = load_adapter(platform)
    if cls.interactive_login or args.interactive:
        try:
            return cls.run_interactive_login(settings, args.account)
        except NotImplementedError:
            out.info(f"{DISPLAY_NAMES[platform]}没有终端交互式登录，去掉 --interactive 即可")
            return EXIT_USAGE
    headless = bool(args.headless)  # 登录默认有头，方便直接在浏览器窗口里扫码
    try:
        with history.account_lock(settings, platform, args.account):
            result = run_worker(settings, action="login", platform=platform, account=args.account,
                                headless=headless, run_dir=_new_run_dir(settings, "-login")[1],
                                on_event=out.event)
    except history.AccountBusy as exc:
        result = _busy(platform, args.account, "login", str(exc))
    if out.as_json:
        out.emit_json({"ok": result.ok, "result": result.to_dict()})
    else:
        print(format_result(result))
    return exit_code_for([result])


def cmd_check(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    settings.ensure_dirs()
    platforms = parse_platform_list(args.platforms) or list(PLATFORMS)
    run_dir = _new_run_dir(settings, "-check")[1]
    results: list[Result] = []
    for platform in platforms:
        accounts = [args.account] if args.account else _accounts(settings, platform)
        if not accounts:
            results.append(Result(platform=platform, account="-", action="check", status=Status.NEEDS_ACTION,
                                  message=f"没有已登录账号，运行 mpub login {platform} --account <名字>",
                                  error_code=ErrorCode.LOGIN_REQUIRED.value, finished_at=now_iso()))
            continue
        for account in accounts:
            try:
                with history.account_lock(settings, platform, account):
                    # 每个账号单独的目录：同一平台几个账号的日志、截图不会互相覆盖
                    results.append(run_worker(settings, action="check", platform=platform, account=account,
                                              headless=True, run_dir=run_dir / f"{platform}-{account}",
                                              on_event=out.event))
            except history.AccountBusy as exc:
                results.append(_busy(platform, account, "check", str(exc)))
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
        if out.as_json:
            out.emit_json({"ok": False, "error": "exists", "message": f"{dest} 已存在（--force 覆盖）"})
        else:
            out.info(f"{dest} 已存在（--force 覆盖）")
        return EXIT_USAGE
    shutil.copyfile(TEMPLATES / f"{args.kind}.yaml", dest)
    if args.kind == "article":
        md = target / "article.md"
        if not md.exists():
            md.write_text("# 标题会被忽略，以 post.yaml 的 title 为准\n\n正文，支持 **Markdown** 和本地图片：\n\n![说明](images/1.jpg)\n",
                          encoding="utf-8")
    if out.as_json:
        out.emit_json({"ok": True, "path": str(dest.resolve()), "kind": args.kind})
    else:
        print(str(dest))
    return EXIT_OK


def _login_issue(settings: Settings, post: PlatformPost) -> Issue | None:
    """本地看一眼登录文件在不在（不联网）：最常见的是用 --account main 登录了，清单却没写 account。"""
    if post.platform not in PLATFORMS or settings.account_file(post.platform, post.account).is_file():
        return None
    others = _accounts(settings, post.platform)
    login = f"mpub login {post.platform} --account {post.account}"
    if others:
        msg = (f"账号 {post.account} 还没有登录（这个平台已登录: {', '.join(others)}）：要用已登录的账号就在 post.yaml "
               f"写 account: {others[0]} 或加 --account {others[0]}，否则运行 {login}")
    else:
        msg = f"账号 {post.account} 还没有登录：运行 {login}"
    return Issue("warning", "account", msg, post.platform)


def _collect(args: argparse.Namespace, settings: Settings | None = None
             ) -> tuple[Any, list[PlatformPost], list[Result], list[Issue]]:
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
        login = _login_issue(settings, post) if settings is not None else None
        if login is not None:
            issues.append(login)
        posts.append(post)
    return manifest, posts, skipped, issues


def cmd_validate(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    manifest, posts, skipped, issues = _collect(args, settings)
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


def _dedupe(settings: Settings, post: PlatformPost, force: bool) -> Result | None:
    if force:
        return None
    prev = history.find_previous(settings, history.post_fingerprint(post))
    return _skip(post, history.describe_previous(prev), ErrorCode.VALIDATION) if prev else None


def cmd_publish(args: argparse.Namespace, settings: Settings, out: Printer) -> int:
    manifest, posts, skipped, issues = _collect(args, settings)
    history.settle_pending(settings)
    errors = [i for i in issues if i.level == "error"]
    for i in issues:
        out.info(f"  {'✘' if i.level == 'error' else '!'} {i}")
    if errors:
        if out.as_json:
            out.emit_json({"ok": False, "error": "validation", "issues": [i.__dict__ for i in issues]})
        return EXIT_USAGE

    results: list[Result] = list(skipped)

    if args.dry_run:
        # 只读本地发布记录，提前告诉用户哪些平台真正发布时会被查重跳过
        for post in posts:
            dup = _dedupe(settings, post, args.force)
            results.append(dup or Result(platform=post.platform, account=post.account, action="publish",
                                         status=Status.DRY_RUN, kind=post.kind.value,
                                         message="本地校验通过（未联网、未发布）", finished_at=now_iso()))
        return _finish(out, results, _run_id(), issues)

    # 查重：同一内容已经发过（或结果未知）的平台默认跳过
    pending: list[PlatformPost] = []
    for post in posts:
        dup = _dedupe(settings, post, args.force)
        if dup:
            results.append(dup)
        else:
            pending.append(post)

    if pending and not args.yes:
        if not sys.stdin.isatty():
            msg = "非交互环境需要加 --yes 才会真正发布（建议先 --dry-run 确认）"
            if out.as_json:
                out.emit_json({"ok": False, "error": "confirmation_required", "message": msg})
            else:
                out.info(msg)
            return EXIT_USAGE
        names = "、".join(f"{DISPLAY_NAMES[p.platform]}/{p.account}" for p in pending)
        try:
            answer = input(f"即将发布「{pending[0].title or manifest.path.parent.name}」到 {names}，确认? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out.info("已取消")
            return EXIT_USAGE

    settings.ensure_dirs()
    run_id, run_dir = _new_run_dir(settings)
    gap = settings.gap_seconds if args.gap is None else args.gap
    started_any = False
    try:
        for post in pending:
            fingerprint = history.post_fingerprint(post)
            record = {"run_id": run_id, "fingerprint": fingerprint, "source": str(manifest.path), "title": post.title}
            if started_any and gap > 0:
                out.info(f"等待 {gap} 秒后发布下一个平台……")
                time.sleep(gap)
            try:
                with history.account_lock(settings, post.platform, post.account):
                    # 拿到账号锁之后再查一次：另一个 mpub 可能刚刚把同一内容发完（两个终端、计划任务同时跑）
                    dup = _dedupe(settings, post, args.force)
                    if dup:
                        results.append(dup)
                        continue
                    started_any = True
                    out.info(f"→ 发布到 {DISPLAY_NAMES[post.platform]}/{post.account} ……")
                    result = run_worker(settings, action="publish", platform=post.platform, account=post.account,
                                        headless=not args.headed and settings.browser.headless,
                                        run_dir=run_dir / post.platform, post=post, timeout=args.timeout,
                                        on_event=_publish_events(out, settings, post, record))
            except WorkerInterrupted as exc:
                # 被中断也要记账：已触发发布的记为结果未知，查重会拦住下一次
                history.append(settings, exc.result, **record)
                results.append(exc.result)
                if not out.as_json:
                    print(format_result(exc.result))
                raise
            except history.AccountBusy as exc:
                result = _busy(post.platform, post.account, "publish", str(exc), post.kind.value)
            history.append(settings, result, **record)
            if not out.as_json:
                print(format_result(result))
            results.append(result)
    except KeyboardInterrupt:
        done = {(r.platform, r.account) for r in results}
        for post in pending:
            if (post.platform, post.account) not in done:
                results.append(Result(platform=post.platform, account=post.account, action="publish",
                                      status=Status.FAILED, kind=post.kind.value, error_code=ErrorCode.INTERNAL.value,
                                      message="已中断，这个平台还没开始发布", retryable=True, finished_at=now_iso()))
        if out.as_json:
            out.emit_json({"ok": False, "error": "interrupted", "run_id": run_id,
                           "results": [r.to_dict() for r in results], "issues": [i.__dict__ for i in issues]})
        else:
            for r in results:
                if r.status is Status.SKIPPED or r.message == "已中断，这个平台还没开始发布":
                    print(format_result(r))
        out.info("已中断")
        return EXIT_INTERRUPTED
    return _finish(out, results, run_id, issues, printed=not out.as_json)


def _publish_events(out: Printer, settings: Settings, post: PlatformPost, record: dict[str, Any]):
    """转发 worker 事件；一看到 submit_started 就先记一笔「结果待定」（见 history.mark_pending）。"""
    marked = False

    def handle(event: dict[str, Any]) -> None:
        nonlocal marked
        if event.get("event") == "submit_started" and not marked:
            marked = True
            pending = Result(platform=post.platform, account=post.account, action="publish", status=Status.FAILED,
                             kind=post.kind.value, error_code=ErrorCode.UNKNOWN_OUTCOME.value, submit_started=True,
                             message="已点发布，结果待定（发布进程中途被结束时以此为准：请先到平台后台确认，不要直接重发）",
                             started_at=now_iso(), finished_at=now_iso())
            try:
                history.mark_pending(settings, pending, **record)
            except Exception as exc:  # noqa: BLE001 - 记账失败不能打断发布
                out.info(f"写发布记录失败: {exc}")
        out.event(event)

    return handle


def _finish(out: Printer, results: list[Result], run_id: str, issues: list[Issue],
            printed: bool = False) -> int:
    code = exit_code_for(results)
    if out.as_json:
        out.emit_json({"ok": code == EXIT_OK, "run_id": run_id, "results": [r.to_dict() for r in results],
                       "issues": [i.__dict__ for i in issues]})
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
    history.settle_pending(settings)
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
        msg = "目前只有抖音发布时可能需要短信验证码"
        if out.as_json:
            out.emit_json({"ok": False, "error": "unsupported", "message": msg})
        else:
            out.info(msg)
        return EXIT_USAGE
    # 与 multipub._sau.conf.verify_code_file() 一致：<数据目录>/sau/verify_code.txt
    path = settings.sau_home / "verify_code.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args.code.strip(), encoding="utf-8")
    if out.as_json:
        out.emit_json({"ok": True, "path": str(path)})
    else:
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

    s = sub.add_parser("setup", parents=[common], help="安装浏览器内核、准备 biliup")
    s.add_argument("--skip-browser", action="store_true")
    s.add_argument("--biliup", metavar="auto|download|<路径>",
                   help="biliup 来源：auto 用本机找到的兼容版本；download 下载固定版本；或直接给路径")

    s = sub.add_parser("biliup", parents=[common], help="选择 B站视频上传用的 biliup")
    bsub = s.add_subparsers(dest="biliup_cmd", required=True)
    bsub.add_parser("find", parents=[common], help="列出本机找到的 biliup 及是否兼容")
    bsub.add_parser("status", parents=[common], help="当前使用哪个 biliup、为什么")
    u = bsub.add_parser("use", parents=[common], help="选择：<路径>、find 列表里的 <编号>、--auto 或 --download")
    u.add_argument("target", nargs="?", help="biliup.exe 的路径（或所在目录），或 find 列表里的编号")
    g = u.add_mutually_exclusive_group()
    g.add_argument("--auto", action="store_true", help="每次自动挑本机最合适的兼容版本")
    g.add_argument("--download", action="store_true", help="用 mpub setup 下载的固定版本")
    u.add_argument("--force", action="store_true", help="兼容性检查不通过也要用")

    s = sub.add_parser("login", parents=[common], help="扫码登录某个平台账号")
    s.add_argument("platform")
    s.add_argument("--account", default="default")
    s.add_argument("--headless", action="store_true", help="无头登录（只输出二维码图片，不弹窗口）")
    s.add_argument("--interactive", action="store_true",
                   help="在当前终端里跑平台自带的交互式登录（目前仅 B站：biliup login）")

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
    "history": cmd_history, "verify-code": cmd_verify_code, "biliup": cmd_biliup,
}


def _install_signal_handlers() -> None:
    """把 SIGTERM（Windows 上还有 Ctrl+Break）当作 Ctrl+C 处理，保证子进程树被清理、中断被记账。"""
    import signal

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # 非主线程等情况
                pass


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    _install_signal_handlers()
    args = build_parser().parse_args(argv)
    out = Printer(args.json)
    try:
        settings = load_settings(args.home)
    except ConfigError as exc:
        if out.as_json:
            out.emit_json({"ok": False, "error": "config", "message": str(exc)})
        else:
            out.info(f"配置错误: {exc}")
        return EXIT_USAGE
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
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
