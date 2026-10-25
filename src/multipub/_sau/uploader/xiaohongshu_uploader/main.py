# -*- coding: utf-8 -*-
# vendored from social-auto-upload 0012d2c (MIT)；改动见 multipub/_sau/PATCHES.md，代码中以 "# multipub patch:" 标出
from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

# multipub patch: 包内绝对导入；conf 由 multipub._sau.conf.configure() 预先设置
from multipub._sau import conf as sau_conf
from multipub._sau.conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from multipub._sau.runtime import (CaptchaRequired, CaptchaWatch, goto_resilient, CookieInvalid, Deadline, DeclarationFailed,
                                   Hooks, SauError, diagnose, LoginPageDetected, ScheduleFailed, UploadFailed, close_quietly, fail_rejected,
                                   fail_step, find_visible_text,
                                   input_value, publish_button_ready, save_screenshot, save_state_quietly,
                                   schedule_value_matches, toggle_state)
from multipub._sau.uploader.base_video import BaseVideoUploader
from multipub._sau.utils.base_social_media import set_init_script
from multipub._sau.utils.login_qrcode import build_login_qrcode_path
from multipub._sau.utils.login_qrcode import decode_qrcode_from_path
from multipub._sau.utils.login_qrcode import print_terminal_qrcode
from multipub._sau.utils.login_qrcode import remove_qrcode_file
from multipub._sau.utils.login_qrcode import save_data_url_image
from multipub._sau.utils.log import xiaohongshu_logger

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
# multipub patch: 成功页地址带不带查询串都认（上游的 glob 要求 success 后面有 ?…）
XHS_PUBLISH_SUCCESS_URL_RE = re.compile(r"/publish/success(?:[/?#]|$)")
# multipub patch: 视频预览区出现这些字 = 上传失败（失败时也有「重新上传」，上游把它当成功关键字）
XHS_UPLOAD_FAILED_TEXTS = ("上传失败", "上传出错", "上传异常")
XHS_UPLOAD_SUCCESS_TEXTS = ('上传成功', '分辨率', '重新上传', '编辑封面', '已上传', '已选择', '100%')
XHS_VIDEO_INPUT_SELECTOR = "div[class^='upload-content'] input[class='upload-input']"
XHS_IMAGE_INPUT_SELECTOR = 'input[type="file"][accept*="image"], ' + XHS_VIDEO_INPUT_SELECTOR
# 正文编辑器：空的时候第一段带 data-placeholder；页面恢复了草稿时没有，退回到可编辑区本身
XHS_DESC_SELECTOR = 'p[data-placeholder*="输入正文描述"]'
XHS_EDITOR_FALLBACK_SELECTOR = '.tiptap[contenteditable="true"], .ProseMirror[contenteditable="true"], div[contenteditable="true"]'
# 页面上的上传状态：可见元素的直接文字含「上传中 / 正在上传」（「上传中心」之类的菜单名除外）= 还在传；
# 含「上传失败」等 = 有素材传失败了。返回 {busy, failed}（各为第一处文字，没有为空串）
_UPLOAD_STATE_JS = """(failedTexts) => {
    const out = {busy: '', failed: ''};
    for (const el of document.querySelectorAll('body *')) {
        if (el.closest('[contenteditable="true"], input, textarea')) continue;
        const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
        if (!own || own.includes('上传中心')) continue;
        const busy = /上传中|正在上传/.test(own), failed = failedTexts.some(t => own.includes(t));
        if (!busy && !failed) continue;
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (!(r.width && r.height && st.visibility !== 'hidden' && st.display !== 'none')) continue;
        if (failed && !out.failed) out.failed = own.slice(0, 40);
        else if (busy && !out.busy) out.busy = own.slice(0, 40);
    }
    return out;
}"""
# multipub patch: 弹窗（封面弹窗等；上游等它关闭用的就是这个选择器，postsail 是 div.d-modal.cover-modal）
XHS_MODAL_SELECTOR = "div.d-modal"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_QR_SWITCH_WAIT = 10.0  # multipub patch: 切扫码面板：等各个切换入口出现的总时长（秒）；30% 时间后试登录框右上角的图标
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
# multipub patch: AI 内容声明选项文字（来自 omnipost，未在真实账号上验证）
XHS_AI_DECLARATION = "笔记含AI合成内容"
# multipub patch: 发布页「内容设置 → 加入合集」。结构为真实页面实测：
# 触发器 .collection-plugin-wrapper 里的 .collection-plugin-button（未选时，文字「选择合集」）；选中后换成 .collection-plugin-choose > .collection-name（合集名），
# 浮层挂在 body 下 .collection-plugin-popover，每项 .item > .item-content > .item-label，底部 .popover-footer 是「创建合集」。
# 用页面脚本定位和点击：patchright 的 CSS locator 在这个页面上时常定位不到这些元素。
XHS_COLLECTION_JS = """(arg) => {
  const wrap = document.querySelector('.collection-plugin-wrapper');
  if (!wrap) return {ok: false, why: 'nowrap'};
  // 未选：.collection-plugin-button（「选择合集」）；选中后换成 .collection-plugin-choose > .collection-name（合集名）
  const btn = wrap.querySelector('.collection-plugin-button, .collection-plugin-choose');
  const nameEl = wrap.querySelector('.collection-name');
  const current = nameEl ? (nameEl.textContent || '').trim() : '';
  if (arg.op === 'current') return {ok: !!btn, current: current, why: btn ? '' : 'nobtn'};
  if (arg.op === 'open') {
    if (!btn) return {ok: false, why: 'nobtn'};
    wrap.scrollIntoView({block: 'center'});
    btn.click();
    return {ok: true, current: current};
  }
  if (arg.op === 'pick') {
    const items = [...document.querySelectorAll('.collection-plugin-popover .item')];
    const label = e => ((e.querySelector('.item-label') || e).textContent || '').trim();
    const it = items.find(e => label(e) === arg.name);
    if (!it) return {ok: false, why: 'noitem', names: items.map(label)};
    it.scrollIntoView({block: 'nearest'});
    it.click();
    return {ok: true};
  }
  return {ok: false, why: 'op'};
}"""
# multipub patch: 创建笔记接口（与开源 xhs 客户端一致；被动监听用于防重复点击和拿笔记 ID，未在真实账号上验证）
XHS_CREATE_API_RE = re.compile(r"^/web_api/sns/v\d+/note/?$")
XHS_API_HOSTS = (".xiaohongshu.com", ".rednote.com")


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。

    multipub patch: 上游无条件连点元素和 3 层祖先——原生 click 本来就会冒泡，同一个选项的处理函数会被触发好几次
    （「来源转载」会弹出好几层媒体名称框，开关类会被点回去）。现在每点一层等一下，元素已被移除或隐藏
    （下拉框/弹窗已收起）就停。
    """
    return await page.evaluate(
        """async (t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            const leaf = nodes[nodes.length - 1];
            let el = leaf;
            for (let i = 0; i < 4 && el; i++) {
                try { el.click(); } catch (e) {}
                await new Promise(r => setTimeout(r, 100));
                if (!leaf.isConnected || !leaf.getClientRects().length) break;
                el = el.parentElement;
            }
            return true;
        }""",
        text,
    )


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _open_xhs_qrcode_panel(page: Page) -> bool:
    """切到扫码登录面板，返回是否切到了。

    multipub patch: 尽力而为——上游只认哈希类名的切换图标（img.css-wemwzq），找不到就抛错、登录直接失败并关掉窗口。
    现在依次试切换图标、「扫码登录」文字；都不行返回 False，由调用方上报整页截图并照常等待登录。
    """
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    try:
        await login_box.wait_for(state="visible", timeout=30000)
    except Exception:
        xiaohongshu_logger.warning(_msg("😵", "没等到小红书登录框，可能是页面改版"))
        return False

    # multipub patch: 以「二维码图片可见」判断是否已在扫码面板。上游看 div:has-text('扫一扫') 的数量：
    # 扫码面板只是隐藏在 DOM 里（默认显示手机号登录）时也有数量，于是不点切换、上报一张没有二维码的截图
    if await _xhs_qrcode_visible(page):
        return True

    # multipub patch: 各个切换入口轮流看（上游只等哈希类名的图标 10 秒）；哪个先出现点哪个，点完二维码出来就算切到了。
    # 最后的兜底是登录框右上角的图片（切换图标的位置，哈希类名改版后也还在那里）
    switches = (
        login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first,
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("APP扫一扫登录").first,
    )
    # 扫码面板的标题已经可见、只是二维码图片还没加载出来：等图片，别去点切换（切换图标在两个面板间来回切）
    try:
        panel_title = page.locator(".login-box-container").get_by_text("APP扫一扫登录").filter(visible=True)
        if await panel_title.count():
            await _xhs_qrcode_img(page).wait_for(state="visible", timeout=5000)
            return True
    except Exception:
        pass

    tried: set[int] = set()
    deadline = Deadline(XHS_QR_SWITCH_WAIT, "切到扫码登录面板")
    corner_tried = False
    while True:
        for i, switch in enumerate(switches):
            if i in tried:
                continue
            try:
                if not (await switch.count() and await switch.is_visible()):
                    continue
                tried.add(i)
                await switch.click(timeout=5000)
                await _xhs_qrcode_img(page).wait_for(state="visible", timeout=5000)
                return True
            except Exception:
                continue
        if await _xhs_qrcode_visible(page):
            return True
        elapsed = deadline.seconds - deadline.remaining()
        if not corner_tried and (elapsed >= deadline.seconds * 0.3 or len(tried) == len(switches)):
            corner_tried = True
            try:
                if await page.evaluate(_XHS_CORNER_SWITCH_JS, XHS_LOGIN_BOX_SELECTOR):
                    await _xhs_qrcode_img(page).wait_for(state="visible", timeout=5000)
                    xiaohongshu_logger.info(_msg("🧭", "用登录框右上角的图标切到了扫码登录"))
                    return True
            except Exception:
                pass
        if deadline.expired or (corner_tried and len(tried) == len(switches)):
            break
        await asyncio.sleep(0.5)
    xiaohongshu_logger.warning(_msg("😵", "没能切到扫码登录面板（切换按钮可能改版了）"))
    return False


# multipub patch: 点登录框右上角的图片（扫码 / 手机号登录的切换图标通常在那里）；点到了返回 True
_XHS_CORNER_SWITCH_JS = """(boxSel) => {
    const box = document.querySelector(boxSel);
    if (!box) return false;
    const b = box.getBoundingClientRect();
    for (const img of box.querySelectorAll('img, svg')) {
        const r = img.getBoundingClientRect();
        if (!r.width || !r.height || r.width > b.width / 3) continue;
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        if (cx > b.left + b.width * 0.75 && cy < b.top + b.height * 0.25) {
            img.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            return true;
        }
    }
    return false;
}"""


def _xhs_qrcode_img(page: Page):
    return page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)


async def _xhs_qrcode_visible(page: Page) -> bool:
    try:
        img = _xhs_qrcode_img(page)
        return bool(await img.count()) and await img.is_visible()
    except Exception:
        return False


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = _xhs_qrcode_img(page)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


XHS_QRCODE_EXPIRED_RE = re.compile(r"已过期|已失效|点击刷新")


async def _refresh_xhs_qrcode(page: Page, account_file: str, qrcode_info: dict | None, qrcode_callback=None) -> dict | None:
    """multipub patch: 上游只上报一次二维码。二维码过期（「已过期」「已失效」「点击刷新」）时点一下刷新；
    二维码图片变了（页面自己刷新、或点过刷新）就重新保存并上报，无头登录时用户也总能拿到最新的二维码。"""
    if not qrcode_info or qrcode_info.get("fallback"):
        return qrcode_info
    try:
        expired = page.locator(".login-box-container").get_by_text(XHS_QRCODE_EXPIRED_RE).filter(visible=True).first
        if await expired.count():
            xiaohongshu_logger.warning(_msg("😵", "二维码过期了，小人去刷新"))
            await expired.click(timeout=3000)
            await page.wait_for_timeout(1000)
        img = _xhs_qrcode_img(page)
        if not await img.count():
            return qrcode_info
        src = await img.get_attribute("src", timeout=3000)
    except Exception:
        return qrcode_info
    if not src or src == qrcode_info.get("image_data_url"):
        return qrcode_info
    previous = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    xiaohongshu_logger.info(_msg("🖼️", "二维码刷新了，重新上报"))
    return await _save_xhs_qrcode(page, account_file, previous, qrcode_callback=qrcode_callback)


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_login_page(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    """multipub patch: 定位不到二维码时，把登录页截图当作二维码图片上报（payload 带 fallback: True）。"""
    fallback_path = build_login_qrcode_path(account_file, suffix="xhs_login_page")
    try:
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(fallback_path))
    except Exception as exc:
        xiaohongshu_logger.warning(_msg("😵", f"登录页截图失败: {exc!r}"))
        return {"image_path": "", "image_data_url": ""}
    if previous_qrcode_path and previous_qrcode_path != fallback_path:
        remove_qrcode_file(previous_qrcode_path)
    qrcode_info = {"image_path": str(fallback_path), "image_data_url": "", "fallback": True}
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    # multipub patch: 定位不到二维码不再是致命错误：上报登录页截图，调用方照常等待登录完成
    # （有头模式下可以直接在窗口里扫码，或改用手机号登录）
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    try:
        qrcode_src = await _extract_xhs_qrcode_src(page)
        qrcode_img = await _find_xhs_qrcode_locator(page)
        if qrcode_src.startswith("data:image/"):
            save_data_url_image(qrcode_src, qrcode_path)
        else:
            qrcode_path.parent.mkdir(parents=True, exist_ok=True)
            await qrcode_img.screenshot(path=str(qrcode_path))
    except Exception as exc:
        xiaohongshu_logger.warning(_msg("😵", f"没定位到二维码（{str(exc)[:60]}）——上报登录页截图；"
                                             "也可以直接在弹出的浏览器里扫码或登录，小人继续等登录完成"))
        return await _save_xhs_login_page(page, account_file, previous_qrcode_path, qrcode_callback)

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        # multipub patch: 统一按配置选择浏览器
        browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(True))
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await goto_resilient(page, _build_xhs_creator_url(  # multipub patch: 慢网络
                "/publish/publish?from=homepage&target=video"
            ))
            await page.wait_for_timeout(3000)

            if page.url.startswith(_build_xhs_creator_url("/login")):
                xiaohongshu_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
            if await login_box.count():
                try:
                    if await login_box.is_visible():
                        xiaohongshu_logger.info(_msg("🥹", "页面仍然停留在登录二维码页，按 cookie 失效处理"))
                        return False
                except Exception:
                    return False

            xiaohongshu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
    login_timeout: float | None = None,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        # multipub patch: 扫码等待时长可配置（上游固定 3s × 100 次）
        extra = {"max_checks": max(1, int(login_timeout // 3))} if login_timeout else {}
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
            **extra,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        # multipub patch: 按配置选择浏览器
        browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(headless))
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            await goto_resilient(page, _build_xhs_creator_url("/login"))  # multipub patch: 慢网络
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            # multipub patch: 兜底截图也可能失败（image_path 为空）：仍然等用户在窗口里登录
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                # multipub patch: 二维码过期 / 刷新后重新上报
                qrcode_info = await _refresh_xhs_qrcode(page, account_file, qrcode_info, qrcode_callback)
                qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info and qrcode_info.get("image_path") else qrcode_path
                if await _is_xhs_login_completed(page):
                    await asyncio.sleep(2)
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "小红书扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        hooks: Hooks | None = None,
        ai_generated: bool = False,
        repost_source: str | None = None,
        declaration: str | None = None,
        collection: str | None = None,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless
        # multipub patch: 适配器注入的回调/超时；声明均为显式开启
        self.hooks = hooks or Hooks(platform="xiaohongshu")
        self.ai_generated = bool(ai_generated)
        self.repost_source = (repost_source or "").strip()
        # multipub patch: 显式指定的内容类型声明（platforms.xiaohongshu.declaration），设置不上就拒绝发布
        self.declaration = (declaration or "").strip()
        # multipub patch: 要加入的合集名（platforms.xiaohongshu.collection），选不上只告警
        self.collection = (collection or "").strip()
        self._publish_clicked = False
        self.publish_guard = self.hooks.publish_guard()
        self.published_note_id: str | None = None
        self.publish_api_error: str | None = None
        self.publish_http_note: str | None = None
        self.declaration_warnings: list[str] = []  # 没能自动完成的声明，适配器写进结果消息
        self._response_tasks: set = set()
        self._captcha = CaptchaWatch(headless=headless, guard=self.publish_guard, logger=xiaohongshu_logger)
        self._upload_retries = 0

    # multipub patch: 页面被踢回登录页时立即报登录失效（上游要等到上传/发布截止时间才超时，视频默认 50 分钟）
    @staticmethod
    def _on_login_page(page: Page) -> bool:
        try:
            return str(page.url).startswith(_build_xhs_creator_url("/login"))
        except Exception:
            return False

    async def _raise_if_login_page(self, page: Page, step: str) -> None:
        if not self._on_login_page(page):
            return
        shot = await save_screenshot(page, self.hooks, "login")
        where = "（已点击发布，结果未知）" if self._publish_clicked else ""
        xiaohongshu_logger.error(_msg("😵", f"{step}时页面跳到了登录页{where}"))
        raise LoginPageDetected(f"{step}时页面跳到了登录页，登录已失效{where}", step=step,
                                after_submit=self._publish_clicked, screenshot=shot)

    async def _classify_failure(self, page: Page | None, exc: BaseException) -> None:
        """multipub patch: 流程里的普通异常（多为填表时的点击 / 输入超时）先看页面停在哪：被踢回登录页 → LoginPageDetected，
        停在人机验证 → CaptchaRequired（适配器给出 login_required / captcha，点过发布则为结果未知）。
        上游只会把 30 秒的 Playwright 超时原样抛出，适配器只能报 internal（例如编辑区出来后被风控弹窗遮住、
        图文页填标题时会话失效）。其余情况直接返回，由调用方重新抛出原异常。"""
        if page is None:
            return
        reason = "login" if self._on_login_page(page) else await diagnose(page)
        if not reason:
            return
        where = "（已点击发布，结果未知）" if self._publish_clicked else ""
        detail = str(exc).splitlines()[0][:80] if str(exc) else exc.__class__.__name__
        kw = {"step": "填写发布表单", "after_submit": self._publish_clicked}
        if reason == "login":
            xiaohongshu_logger.error(_msg("😵", f"发布过程中页面跳到了登录页{where}（{detail}）"))
            raise LoginPageDetected(f"发布过程中页面跳到了登录页，登录已失效{where}", **kw) from exc
        xiaohongshu_logger.error(_msg("😵", f"发布过程中页面停在人机验证{where}（{detail}）"))
        raise CaptchaRequired(f"发布过程中页面停在人机验证，没能继续{where}", **kw) from exc

    async def _wait_upload_input(self, page: Page, selector: str, what: str):
        """multipub patch: 等上传框渲染出来（SPA 慢时最多 page_timeout；上游直接 set_input_files，30 秒后报 internal），
        期间跳到登录页立即报登录失效。"""
        deadline = Deadline(self.hooks.page_timeout, f"等待{what}上传框出现")
        upload_input = page.locator(selector).first
        while True:
            await self._raise_if_login_page(page, deadline.step)
            try:
                await upload_input.wait_for(state="attached", timeout=1000)
                return upload_input
            except Exception:
                pass
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=False)

    async def _wait_uploads_settled(self, page: Page, deadline: Deadline) -> None:
        """multipub patch: 图片在后台上传，标题框早就出来了；上传中点「发布」只会弹提示（PublishGuard 要隔 20 秒才补点，
        最多 3 次，之后报结果未知）。点发布前先等页面上不再显示「上传中」（连续两次检查都没有），最多到上传截止时间。
        有图片显示「上传失败」时抛 UploadFailed（还没点发布）：页面要么一直不让发（以前报成结果未知），
        要么少了这张图照样发出去。"""
        quiet, logged = 0, False
        while quiet < 2:
            await self._raise_if_login_page(page, deadline.step)
            try:
                state = await page.evaluate(_UPLOAD_STATE_JS, list(XHS_UPLOAD_FAILED_TEXTS))
            except Exception:
                state = None
            busy = str((state or {}).get("busy") or "") if isinstance(state, dict) else ""
            failed = str((state or {}).get("failed") or "") if isinstance(state, dict) else ""
            if failed:
                shot = await save_screenshot(page, self.hooks, "upload-failed")
                xiaohongshu_logger.error(_msg("😵", f"有素材上传失败（{failed}），不发布"))
                raise UploadFailed(f"有图片上传失败（页面提示「{failed}」），笔记没有发出；请检查图片和网络后重试"
                                   + (f"；截图: {shot}" if shot else ""))
            if busy:
                quiet = 0
                if not logged:
                    logged = True
                    xiaohongshu_logger.info(_msg("🧍", f"素材还在上传（{busy}），小人等传完再发布"))
                if deadline.expired:
                    await fail_step(page, self.hooks, deadline, after_submit=False)
                await asyncio.sleep(1)
            else:
                quiet += 1
                if quiet < 2:
                    await asyncio.sleep(0.5)

    def _before_publish_click(self) -> None:
        """multipub patch: 第一次点击最终「发布」之前通知适配器（对应 ctx.mark_submit_started）。"""
        if not self._publish_clicked:
            self._publish_clicked = True
            self.hooks.call("before_publish_click")

    async def apply_declarations(self, page: Page) -> None:
        """multipub patch: 上游无条件声明「来源转载」（媒体名称为空），改为显式开启：
        有 repost_source 才声明转载，设置不上就拒绝发布（不能把转载内容当原创发出去）；
        ai_generated 时尽力选择 AI 合成内容声明（没能确认选上只告警，写进结果消息）。
        显式指定 declaration 时选这一项（优先于 ai_generated），设置不上就拒绝发布。"""
        if self.repost_source and not await self.check_original_declaration(page):
            await save_screenshot(page, self.hooks, "declaration")
            raise DeclarationFailed(
                f"「来源转载」声明（来源：{self.repost_source}）没能设置上，拒绝继续发布，笔记没有发出。"
                "可去掉 platforms.xiaohongshu.repost_source 后重发，并在网页上手动声明转载"
            )
        if self.declaration:
            if not await self.set_content_declaration(page, self.declaration):
                await save_screenshot(page, self.hooks, "declaration")
                raise DeclarationFailed(
                    f"内容类型声明「{self.declaration}」没能设置上，拒绝继续发布，笔记没有发出。"
                    "请核对 platforms.xiaohongshu.declaration 与页面上的选项文字完全一致，"
                    "或去掉它后重发，并在网页上手动声明"
                )
            return
        if self.ai_generated and not await self.set_ai_content_declaration(page):
            self.declaration_warnings.append(f"未能确认已选择「{XHS_AI_DECLARATION}」声明，请在笔记管理里检查并补充")

    PUBLISH_CLICK_TIMEOUT_MS = 5000

    async def _publish_button(self, page: Page):
        """multipub patch: 上游的 button:has-text("发布") 是子串匹配；命中多个按钮时改用文字完全一致的那个，
        仍不唯一就不点（_click_publish 不会点不唯一的按钮），等到截止时间按超时处理。"""
        text = "定时发布" if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED else "发布"
        button = page.locator(f'button:has-text("{text}")')
        if await button.count() > 1:
            exact = page.locator("button").filter(has_text=re.compile(rf"^\s*{text}\s*$"))
            if await exact.count() == 1:
                return exact
        return button

    async def _click_publish(self, button) -> bool:
        """multipub patch: 受 PublishGuard 限制地点击最终发布按钮；第一次点击前通知适配器。

        按钮不唯一 / 不可见 / 不可用时不点、不记账、不标记已提交，继续在截止时间内等。"""
        if not self.publish_guard.may_click():
            return False
        if not await publish_button_ready(button):
            xiaohongshu_logger.debug(_msg("🧍", "发布按钮还不能点（不唯一、不可见或不可用），小人再等等"))
            return False
        # multipub patch: 先 trial（只做可点性检查、不派发点击）。按钮被弹窗 / 遮罩挡住时 click() 会超时、根本没点下去，
        # 以前这也记一次点击并标记已提交，最后报 unknown_outcome（其实什么都没发出，查重还会拦住重发）
        try:
            await button.click(timeout=self.PUBLISH_CLICK_TIMEOUT_MS, trial=True)
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("🧍", f"发布按钮被挡住或还不能点（{exc.__class__.__name__}），本轮不点"))
            await self._press_escape(getattr(button, "page", None))  # 收起可能挡着的下拉框
            return False
        self._before_publish_click()
        self.publish_guard.clicked()  # 先记账再点：点击抛异常时也可能已经触发
        await button.click(timeout=self.PUBLISH_CLICK_TIMEOUT_MS)
        self.publish_guard.click_returned()
        return True

    @staticmethod
    def _is_create_request(request) -> bool:
        try:
            parsed = urlparse(request.url)
            return (request.method == "POST" and (parsed.hostname or "").endswith(XHS_API_HOSTS)
                    and bool(XHS_CREATE_API_RE.match(parsed.path)))
        except Exception:
            return False

    def _watch_publish_response(self, page: Page) -> None:
        """multipub patch: 被动监听创建笔记接口（不拦截、不改动请求）：记录笔记 ID / 平台错误，告知 PublishGuard。"""

        async def capture(response) -> None:
            ok: bool | None = None
            try:
                # multipub patch: 网关错误（5xx、HTML 错误页）记下 HTTP 状态，写进最后的超时报错
                status = getattr(response, "status", 200)
                if isinstance(status, int) and status >= 400:
                    self.publish_http_note = f"创建笔记接口返回 HTTP {status}"
                    xiaohongshu_logger.warning(_msg("😵", self.publish_http_note))
                body = await response.json()
                if not isinstance(body, dict):
                    return
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                code = body.get("code")
                if 200 <= response.status < 300 and (body.get("success") is True or code == 0):
                    ok = True
                    note_id = str(data.get("id") or data.get("note_id") or "")
                    if re.fullmatch(r"[0-9a-f]{24}", note_id):
                        self.published_note_id = note_id
                        xiaohongshu_logger.info(_msg("🧾", f"创建笔记接口返回笔记 ID: {note_id}"))
                elif body.get("success") is False or (isinstance(code, int) and not isinstance(code, bool) and code != 0):
                    ok = False
                    self.publish_api_error = str(body.get("msg") or f"code={code}")
                    xiaohongshu_logger.warning(_msg("😵", f"创建笔记接口返回失败: {self.publish_api_error}"))
            except Exception:
                return
            finally:
                self.publish_guard.request_done(ok, self.publish_api_error or "")

        def on_request(request) -> None:
            if self._publish_clicked and self._is_create_request(request):
                self.publish_guard.request_sent()

        def on_response(response) -> None:
            if not self._publish_clicked or not self._is_create_request(response.request):
                return
            task = asyncio.ensure_future(capture(response))
            self._response_tasks.add(task)
            task.add_done_callback(self._response_tasks.discard)

        def on_failed(request) -> None:
            if self._publish_clicked and self._is_create_request(request):
                self.publish_guard.request_done(None)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

    def _publish_detail(self) -> str:
        parts = []
        if self.published_note_id:
            parts.append(f"创建接口已返回笔记 ID {self.published_note_id}")
        if self.publish_api_error:
            parts.append(f"平台返回: {self.publish_api_error}")
        if self.publish_http_note and not self.publish_api_error:
            parts.append(self.publish_http_note)
        return "；".join(parts)

    # 创建笔记接口已返回成功、页面却一直没跳成功页：等这么久后按成功处理（接口回执就是笔记已创建的证据）。
    # 以 hooks.receipt_grace 为准（runtime 里与抖音共用），这里是它不存在时的默认值
    SUCCESS_REDIRECT_GRACE = 10.0

    async def _publish_until_success(self, page: Page, what: str) -> None:
        """上游的发布循环（multipub patch: 带截止时间；视频/图文共用；
        发布按钮只经 _click_publish 点击：PublishGuard 限制重复点击，第一次点击前通知适配器；
        有头模式下等用户完成人机验证：CaptchaWatch；跳回登录页立即报错；
        创建接口已返回成功但页面不跳成功页时，宽限 SUCCESS_REDIRECT_GRACE 秒后按成功处理）。"""
        self._watch_publish_response(page)
        deadline = Deadline(self.hooks.publish_timeout, f"等待{what}发布成功")
        api_success_at: float | None = None
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=self._publish_clicked,
                                detail=self._publish_detail())
            try:
                await self._raise_if_login_page(page, deadline.step)
                if self.publish_guard.succeeded:
                    api_success_at = api_success_at or time.monotonic()
                    grace = float(getattr(self.hooks, "receipt_grace", self.SUCCESS_REDIRECT_GRACE))
                    if time.monotonic() - api_success_at >= grace:
                        xiaohongshu_logger.warning(_msg("🧾", f"创建笔记接口已返回成功，但页面没有跳到成功页（{page.url}），"
                                                              "按发布成功处理"))
                        break
                # multipub patch: 有头模式下页面停在人机验证：等用户在窗口里完成，期间不点「发布」、不判被拒
                if await self._captcha.blocking(page, deadline):
                    await asyncio.sleep(self.hooks.poll_interval)
                    continue
                publish_button = await self._publish_button(page)
                if await publish_button.count():
                    await self._click_publish(publish_button)
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_RE,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", f"{what}发布成功，小人开心收工"))
                break
            except SauError:
                raise
            except Exception:
                # 接口明确失败、宽限期内页面没有进展：直接报平台拒绝（fail_rejected 抛出的异常不会被这里吞掉）
                rejection = self.publish_guard.rejection_settled(self.hooks.reject_grace)
                if rejection:
                    await fail_rejected(page, self.hooks, rejection)
                xiaohongshu_logger.info(_msg("🏃", f"小人正在冲刺发布{what}"))
                await asyncio.sleep(0.5)
        if self._response_tasks:
            await asyncio.wait(set(self._response_tasks), timeout=5)

    async def validate_base_args(self):
        # multipub patch: 抛 CookieInvalid，适配器据此提示重新登录
        if not os.path.exists(self.account_file):
            raise CookieInvalid(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise CookieInvalid(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    SCHEDULE_SWITCH_SELECTOR = ".d-switch"  # 在含「定时发布」的 .custom-switch-card 里（上游选择器）
    SCHEDULE_INPUT_SELECTORS = (".d-datepicker-input-filter input.d-text", 'input[placeholder*="日期和时间"]')

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        """上游：点「定时发布」开关 → 往日期框里 fill 时间。

        multipub patch: 上游不管开关原来的状态（已经打开时会把它点关），也不核实结果；开关没打开时
        发布按钮不叫「定时发布」会一直等到超时，时间没填进去则会按页面默认时间定时。现在：开关已打开就不点；
        填完时间后让输入框失焦以提交（不按回车：输入框若在 form 里，回车会触发隐式提交），再核实开关已打开、
        框里的值就是要求的时间（精确到分钟）；否则截图并抛 ScheduleFailed——还没点「发布」，笔记不会发出。
        """
        want = publish_date.strftime("%Y-%m-%d %H:%M")
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        switch = page.locator('.custom-switch-card').filter(has_text="定时发布").locator(self.SCHEDULE_SWITCH_SELECTOR).first
        if not await switch.count():
            await self._schedule_failed(page, "页面上找不到「定时发布」开关")
        state = await toggle_state(switch)
        if state is None and await self._schedule_input(page, timeout=1000) is not None:
            state = True  # 看不出开关状态，但日期框已经在了：当作已打开，不再点（再点会把它关掉）
        if state is not True:
            await switch.click()
            await page.wait_for_timeout(1000)
        if await toggle_state(switch) is False:
            await self._schedule_failed(page, "点了「定时发布」开关，但它没有打开")

        time_input = await self._schedule_input(page)
        if time_input is None:
            await self._schedule_failed(page, "没有出现定时发布的日期时间输入框")
        await time_input.fill(want)
        try:
            await time_input.evaluate("el => { el.dispatchEvent(new Event('change', {bubbles: true})); el.blur(); }")
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        value = await input_value(time_input)
        if await toggle_state(switch) is False:
            await self._schedule_failed(page, "「定时发布」开关又关上了")
        if not schedule_value_matches(value, publish_date):
            await self._schedule_failed(page, f"日期时间框显示「{value or '空'}」，应为「{want}」")
        xiaohongshu_logger.success(_msg("🕒", f"定时发布已设置并核实: {want}"))

    async def _schedule_input(self, page: Page, timeout: float = 8000):
        """multipub patch: 定时发布的日期时间输入框（打开开关后才出现）；没有出现返回 None。"""
        for selector in self.SCHEDULE_INPUT_SELECTORS:
            loc = page.locator(selector).first
            try:
                await loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:
                timeout = min(timeout, 1000)
        return None

    async def _schedule_failed(self, page: Page, reason: str):
        """multipub patch: 定时没能核实：截图后拒绝继续（还没点「发布」）。"""
        shot = await save_screenshot(page, self.hooks, "schedule")
        xiaohongshu_logger.error(_msg("😵", f"定时发布没能设置上：{reason}" + (f"；截图: {shot}" if shot else "")))
        raise ScheduleFailed(
            f"定时发布没能设置上（{reason}），拒绝继续发布，笔记没有发出。"
            "可以直接重试；仍失败时去掉 schedule、到时间再发，或在网页上手动定时发布"
        )

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        title_container = page.locator('input[placeholder*="填写标题"]')
        want = self.title[:20]
        await title_container.fill(want)
        # multipub patch: 读回核对。标题框的 maxlength 按 UTF-16 计数（emoji 等算 2 个），20 个字里有 emoji 时
        # 末尾会被网页静默截掉，上游照样发出。重填一次仍被截断就拒绝发布（还没点发布）；其他不一致只告警
        for attempt in range(2):
            got = await input_value(title_container.first)
            if got.strip() == want.strip():
                return
            if attempt == 0:
                await title_container.fill(want)
        if not got:
            xiaohongshu_logger.debug(_msg("📝", "没能读回标题框的内容，跳过核对"))
            return
        if want.startswith(got):
            await save_screenshot(page, self.hooks, "title")
            raise DeclarationFailed(
                f"标题被网页截成了「{got}」（小红书标题框按 UTF-16 计字数，emoji 等算 2 个字），拒绝继续发布，"
                "笔记没有发出。请缩短标题后重发"
            )
        xiaohongshu_logger.warning(_msg("📝", f"标题框读回来是「{got}」，和要填的「{want}」不一致"))
        self.declaration_warnings.append(f"标题框显示「{got}」，与清单里的标题不一致，请到笔记管理里检查")

    @staticmethod
    async def _desc_editor(page: Page):
        """multipub patch: 正文为空时第一段带 data-placeholder（上游只认它）；页面恢复了草稿、正文不为空时
        就没有这个属性，上游会在 click 上等 30 秒后报 internal。这时退回到可编辑区本身（随后 Ctrl+A 清空重写）。"""
        desc = page.locator(XHS_DESC_SELECTOR).first
        try:
            await desc.wait_for(state="visible", timeout=5000)
            return desc
        except Exception:
            pass
        editor = page.locator(XHS_EDITOR_FALLBACK_SELECTOR).filter(visible=True).first
        if await editor.count():
            xiaohongshu_logger.warning(_msg("📝", "正文框不是空的（可能恢复了上次的草稿），小人清空后重写"))
            return editor
        return desc

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = await self._desc_editor(page)
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        # multipub patch: 逐行输入。正文里写了 #话题 / @某人 时编辑器会弹出候选框，候选框开着时回车会选中第一个候选
        # （先弹热门话题时会换成一个不相干的话题，还吞掉换行）；这样的行在回车前先按 Escape 收起候选框
        for line in self.desc.split("\n"):
            if line:
                await page.keyboard.type(line)
            if "#" in line or "@" in line:
                await page.keyboard.press("Escape")
            await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            xiaohongshu_logger.warning(
                _msg("🏷️", f"标签数量 {len(self.tags)} 超过小红书上限 {max_tags}，只取前 {max_tags} 个: {self.tags[:max_tags]}")
            )
            self.tags = self.tags[:max_tags]

        if not getattr(self, "desc", ""):
            desc = await self._desc_editor(page)
            await desc.click()

        for tag in self.tags:  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # 标签是可选增强项：等不到候选框就跳过该标签继续，不让整条发布因此失败。
            try:
                await page.keyboard.type("#" + tag, delay=30)
                await page.locator('#creator-editor-topic-container').wait_for(
                    state="visible",
                    timeout=6000
                )
                first_item = page.locator('#creator-editor-topic-container .item').first
                await first_item.wait_for(state="visible", timeout=4000)
                # multipub patch: 不再无条件点第一个候选——下拉框可能先显示热门话题、联想结果稍后才刷新，
                # 那样会挂上不相干的话题；等名字对得上的候选出现（最多约 3 秒），对不上就跳过该标签
                item = await self._matching_topic_item(page, tag)
                if item is None:
                    raise RuntimeError("候选里没有同名话题")
                await item.click()
            except Exception as exc:
                xiaohongshu_logger.warning(
                    _msg("🏷️", f"话题『{tag}』未出现候选，跳过该标签继续发布: {exc}")
                )
                # 清掉已键入但未成词的 "#tag" 文本，避免它残留进正文
                for _ in range(len("#" + tag)):
                    await page.keyboard.press("Backspace")
                continue

    _TOPIC_TEXTS_JS = """items => items.map(it => [it, ...it.querySelectorAll('*')]
        .map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim())
        .filter(Boolean))"""

    @staticmethod
    def _topic_name(text: str) -> str:
        text = re.sub(r"\[话题\]#?$", "", text.strip())
        return text.strip().lstrip("#").strip().casefold()

    async def _matching_topic_item(self, page: Page, tag: str, wait: float = 3.0):
        """multipub patch: 话题候选里名字与 tag 相同（忽略 # 和大小写）的那一项；没有时退而求其次取名字包含 tag 的；
        等 wait 秒仍没有就返回 None。"""
        items = page.locator('#creator-editor-topic-container .item')
        want = self._topic_name(tag)
        deadline = Deadline(wait, f"等话题『{tag}』的候选")
        while True:
            try:
                texts = await items.evaluate_all(self._TOPIC_TEXTS_JS)
            except Exception:
                texts = []
            names = [[self._topic_name(t) for t in parts] for parts in texts]
            for i, parts in enumerate(names):
                if want in parts:
                    return items.nth(i)
            if deadline.expired:
                for i, parts in enumerate(names):
                    if parts and want in parts[0]:
                        return items.nth(i)
                return None
            await asyncio.sleep(0.3)

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)

    async def set_ai_content_declaration(self, page: Page) -> bool:
        """小红书「添加内容类型声明」选择「笔记含AI合成内容」。

        multipub patch: 移植自 omnipost（MIT，基于 SAU 99a392a），尽力而为，失败只告警。
        """
        if not getattr(self, "ai_generated", False):
            return False
        return await self.set_content_declaration(page, XHS_AI_DECLARATION)

    async def set_content_declaration(self, page: Page, declaration_text: str) -> bool:
        """在「添加内容类型声明」下拉里选择文字为 declaration_text 的选项。

        multipub patch: 由 set_ai_content_declaration 泛化而来，选项文字由调用方给出（如「虚构演绎，仅供娱乐」）。
        不再凭选项附近有「取消/×」就当作已选：点击后核实——点过的选项随面板收起、
        页面上（输入区以外）显示出这几个字，才返回 True。
        """
        xiaohongshu_logger.info(_msg("🧍", f"小人准备设置内容类型声明：{declaration_text}"))
        try:
            # multipub patch: 兜底从 div.filter(has_text=...)（会命中最外层容器，点到页面中央）
            # 改为非精确 get_by_text（命中包含文字的最内层元素），与上游 check_original_declaration 一致
            trigger_candidates = [
                page.get_by_text("添加内容类型声明", exact=True).first,
                page.get_by_text("添加内容类型声明").first,
            ]

            trigger = None
            for candidate in trigger_candidates:
                try:
                    if await candidate.count():
                        trigger = candidate
                        break
                except Exception:
                    continue

            if not trigger:
                xiaohongshu_logger.warning(_msg("😵", "没找到「添加内容类型声明」入口"))
                return False

            # 先收起可能开着的面板，再看声明是否已经显示为已选（例如页面恢复了草稿）
            await self._press_escape(page)
            if await find_visible_text(page, [declaration_text], exact=True):
                xiaohongshu_logger.info(_msg("🥳", f"内容类型声明已是「{declaration_text}」，跳过"))
                return True

            await trigger.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await trigger.click()
            await page.wait_for_timeout(600)

            option_candidates = [
                page.get_by_text(declaration_text, exact=True).first,
                page.get_by_text(declaration_text).first,
            ]

            for opt in option_candidates:
                try:
                    if not await opt.count():
                        continue
                    if not await opt.is_visible():
                        continue

                    try:
                        handle = await opt.element_handle()
                    except Exception:
                        handle = None
                    await opt.click()
                    await page.wait_for_timeout(600)
                    # 收起声明面板，避免挡住底部「发布」
                    await self._press_escape(page)
                    if await self._declaration_displayed(page, handle, declaration_text):
                        xiaohongshu_logger.success(_msg("🥳", f"已选择内容类型声明：{declaration_text}"))
                        return True
                    xiaohongshu_logger.warning(_msg("😵", f"点了「{declaration_text}」，但没能确认页面上已显示为已选"))
                    return False
                except Exception:
                    continue

            xiaohongshu_logger.warning(_msg("😵", f"没找到「{declaration_text}」选项"))
            return False
        except Exception as e:
            xiaohongshu_logger.warning(_msg("😵", f"设置内容类型声明失败: {e}"))
            return False

    async def apply_collection(self, page: Page) -> None:
        """multipub patch: 在发布页「内容设置 → 加入合集」里选择已有合集（按名称精确匹配）。

        尽力而为，和抖音的合集一致：找不到区域 / 列表里没有这个名字 / 选完按钮没显示这个名字，
        都只写进 declaration_warnings（适配器放进结果消息），照常发布。不会点「创建合集」。
        """
        name = getattr(self, "collection", "")
        if not name:
            return

        async def run(op: str) -> dict:
            r = await page.evaluate(XHS_COLLECTION_JS, {"op": op, "name": name})
            return r if isinstance(r, dict) else {}

        xiaohongshu_logger.info(_msg("🧍", f"小人准备加入合集：{name}"))
        try:
            state: dict = {}
            for _ in range(8):  # 「加入合集」区域出现后，按钮要再过一会儿才渲染（实测）
                state = await run("current")
                if state.get("ok"):
                    break
                await page.wait_for_timeout(500)
            if not state.get("ok"):
                self.declaration_warnings.append(f"没有加入合集「{name}」（页面上找不到「加入合集」）")
                return
            if state.get("current") == name:
                xiaohongshu_logger.info(_msg("🥳", f"合集已是「{name}」，跳过"))
                return
            if not (await run("open")).get("ok"):
                self.declaration_warnings.append(f"没有加入合集「{name}」（打不开合集列表）")
                return
            picked: dict = {}
            for _ in range(5):  # 浮层和列表是异步渲染的
                await page.wait_for_timeout(600)
                picked = await run("pick")
                if picked.get("ok") or picked.get("names"):
                    break
            if not picked.get("ok"):
                await self._press_escape(page)
                why = (f"合集列表里没有这个名字，现有：{'、'.join(picked['names'])}" if picked.get("names")
                       else "合集列表没有出现")
                self.declaration_warnings.append(f"没有加入合集「{name}」（{why}）")
                return
            await page.wait_for_timeout(800)
            if (await run("current")).get("current") != name:
                self.declaration_warnings.append(f"合集「{name}」可能没选上，请到 App 里核对")
                return
            xiaohongshu_logger.success(_msg("🥳", f"已选择合集：{name}"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"选择合集失败: {exc}"))
            self.declaration_warnings.append(f"没有加入合集「{name}」（选择时出错）")
            await self._press_escape(page)

    @staticmethod
    async def _press_escape(page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

    @staticmethod
    async def _declaration_displayed(page: Page, clicked_option, text: str) -> bool:
        """multipub patch: 点过的选项已随面板收起（或已脱离页面），且页面上显示着声明文字。"""
        if clicked_option is not None:
            try:
                if await clicked_option.is_visible():
                    return False  # 面板还开着：看到的文字可能只是选项本身
            except Exception:
                pass  # 元素已脱离页面 = 面板已收起
        return bool(await find_visible_text(page, [text], exact=True))

    async def check_original_declaration(self, page: Page) -> bool:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        multipub patch: 返回是否设置成功，并核实结果——点「确认」后媒体名称输入框关闭、
        页面上（输入区以外）显示出来源名称或「来源转载」。任一步失败返回 False，调用方拒绝发布
        （上游失败只记 warning、照常发出去）。
        """
        source = getattr(self, "repost_source", "") or ""
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            if not await trigger.count():
                xiaohongshu_logger.warning(_msg("⚠️", "没找到「添加内容类型声明」入口"))
                return False
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            # multipub patch: patchright 里「锚定正则 filter + .last」永远解析不到元素（count() 为 0、操作一直等），
            # 上游这里实际总是走 JS 兜底；改为数出个数后取最后一个
            repost_options = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            )
            option_count = await repost_options.count()
            if option_count:
                await repost_options.nth(option_count - 1).click(force=True)
            elif not await _js_click_by_text(page, "来源转载"):
                xiaohongshu_logger.warning(_msg("⚠️", "没找到「来源转载」选项"))
                return False
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                if not await _js_click_by_text(page, "确认"):
                    xiaohongshu_logger.warning(_msg("⚠️", "没找到「确认」按钮"))
                    return False

            await page.wait_for_timeout(1000)
            # 5. multipub patch: 核实——输入框关闭、页面上显示出来源
            try:
                await source_input.wait_for(state="hidden", timeout=5000)
            except Exception:
                xiaohongshu_logger.warning(_msg("⚠️", "点「确认」后媒体名称输入框仍未关闭"))
                return False
            if not await find_visible_text(page, [source, "来源转载"]):
                xiaohongshu_logger.warning(_msg("⚠️", "没能确认页面上已显示「来源转载」声明"))
                return False
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
            return True
        except Exception as exc:
            # multipub patch: 不再按 Escape 继续发布：调用方会截图并拒绝发布
            xiaohongshu_logger.warning(_msg("⚠️", f"设置来源转载失败: {exc}"))
            return False


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        hooks: Hooks | None = None,
        ai_generated: bool = False,
        repost_source: str | None = None,
        declaration: str | None = None,
        collection: str | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
            hooks=hooks,
            ai_generated=ai_generated,
            repost_source=repost_source,
            declaration=declaration,
            collection=collection,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    MAX_UPLOAD_RETRIES = 2

    async def _retry_video_upload(self, page: Page, text: str) -> None:
        """multipub patch: 预览区显示上传失败：用同一个上传框重传，最多 MAX_UPLOAD_RETRIES 次；
        用完（或重传不了）截图并抛 UploadFailed（还没点发布，笔记没有发出，可直接重试）。
        上游把失败页上的「重新上传」当成上传成功，接着去点「发布」，只会弹提示，最后报成结果未知。"""
        reason = " ".join(text.split())[:40]
        if self._upload_retries < self.MAX_UPLOAD_RETRIES:
            self._upload_retries += 1
            xiaohongshu_logger.warning(_msg("😵", f"视频上传失败（{reason}），小人重新上传（第 {self._upload_retries} 次）"))
            try:
                upload_input = page.locator("input.upload-input").first
                # 同一个文件再设一次浏览器不会触发 change：先清空再设
                await upload_input.set_input_files([], timeout=10000)
                await upload_input.set_input_files(self.file_path, timeout=10000)
                # 等预览区不再显示失败，免得同一次失败被数两次
                preview = page.locator("input.upload-input ~ div[class*='preview-new']").first
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    shown = await preview.inner_text(timeout=2000) if await preview.count() else ""
                    if not any(k in shown for k in XHS_UPLOAD_FAILED_TEXTS):
                        break
                return
            except Exception as exc:
                xiaohongshu_logger.warning(_msg("😵", f"重新上传没能开始: {exc}"))
        shot = await save_screenshot(page, self.hooks, "upload-failed")
        raise UploadFailed(f"视频上传失败（页面提示「{reason}」），已重传 {self._upload_retries} 次仍失败，笔记没有发出；"
                           "请检查网络和视频文件后重试" + (f"；截图: {shot}" if shot else ""))

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        # 封面设置为增强步骤：失败时记 warning 跳过、继续发布（用视频首帧兜底）。
        try:
            # 发布页封面区域内嵌，点击 div.upload-cover 打开封面弹窗（d-modal）。
            cover_section = page.locator("text=设置封面").first
            try:
                await cover_section.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # 1. 点击 div.upload-cover 打开封面弹窗
            upload_cover = page.locator("div.upload-cover").first
            if not await upload_cover.count():
                upload_cover = page.locator("div.cover-plugin-preview div.default.pointer").first
            await upload_cover.click(force=True)
            modal = page.locator(XHS_MODAL_SELECTOR).filter(visible=True).first
            await modal.wait_for(state="visible", timeout=10000)  # multipub patch: 等弹窗出现，不再固定等 3 秒
            await page.wait_for_timeout(500)

            # 2. 切换到「上传封面」tab（默认在「截取封面」）
            # multipub patch: 有的版本弹窗里直接就是上传框、没有页签（postsail 的写法）：页签 3 秒内没出现就直接找上传框
            # （上游在这里等 10 秒后失败，并把封面弹窗留在页面上挡住「发布」）
            upload_tab = page.get_by_text("上传封面", exact=True).first
            try:
                await upload_tab.wait_for(state="visible", timeout=3000)
                await upload_tab.click()
                await page.wait_for_timeout(2000)
            except Exception:
                xiaohongshu_logger.info(_msg("🖼️", "封面弹窗里没有「上传封面」页签，直接找上传框"))

            # 3. 找到图片 file input（parent class: upload-wrapper）并上传
            file_input = modal.locator('input[type="file"][accept*="image"]').first
            try:
                await file_input.wait_for(state="attached", timeout=5000)
            except Exception:
                file_input = page.locator('div.upload-wrapper input[type="file"][accept*="image"]').first
                if not await file_input.count():
                    file_input = page.locator('input[type="file"][accept*="image"]').last
                if not await file_input.count():
                    raise RuntimeError("封面弹窗里没有找到图片上传框")
            await file_input.set_input_files(thumbnail_path, timeout=10000)
            await page.wait_for_timeout(4000)  # 等图片加载+裁剪渲染

            # 4. 点「确定」按钮
            modal_footer = page.locator("div.d-modal-footer")
            confirm = modal_footer.get_by_text("确定", exact=True).first
            if not await confirm.count():
                confirm = page.get_by_role("button", name="确定").first
            await confirm.wait_for(state="visible", timeout=10000)
            await confirm.click()

            # 5. 等弹窗关闭
            # multipub patch: 弹窗一直不关（图片不合要求等）不再当成功：上游照样报「封面已经设置完成」，
            # 弹窗留在页面上挡住「发布」
            await modal.wait_for(state="hidden", timeout=15000)
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            reason = str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__
            xiaohongshu_logger.warning(_msg("🖼️", f"封面设置失败，跳过该步骤继续发布（用平台默认封面）：{reason}"))
            await save_screenshot(page, self.hooks, "cover")
            # multipub patch: 关掉留在页面上的封面弹窗（上游只按一次 Escape，弹窗不响应 Escape 时一直挡着「发布」，
            # 点击被遮罩拦下却记作已点击，最后报 unknown_outcome），并把封面没设上写进结果消息
            await self._close_modal(page)
            self.declaration_warnings.append("自定义封面没能设置上，用的是平台默认封面，请到笔记管理里检查")

    async def _close_modal(self, page: Page) -> bool:
        """multipub patch: 关掉页面上可见的 d-modal 弹窗：Escape → 弹窗里的「取消」→ 关闭图标。返回是否已关掉。"""
        modal = page.locator(XHS_MODAL_SELECTOR).filter(visible=True)
        closers = (
            None,  # Escape
            lambda m: m.locator("div.d-modal-footer").get_by_text("取消", exact=True).first,
            lambda m: m.get_by_role("button", name="取消").first,
            lambda m: m.locator(".d-modal-close, [class*='close-icon'], [class*='modal-close']").first,
        )
        for closer in closers:
            try:
                if not await modal.count():
                    return True
                if closer is None:
                    await self._press_escape(page)
                else:
                    target = closer(modal.first)
                    if not await target.count():
                        continue
                    await target.click(timeout=3000)
                await modal.first.wait_for(state="hidden", timeout=3000)
                return True
            except Exception:
                continue
        try:
            if not await modal.count():
                return True
        except Exception:
            pass
        xiaohongshu_logger.warning(_msg("😵", "封面弹窗关不掉，可能会挡住「发布」按钮"))
        return False

    async def upload_video_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=video"
        )
        await goto_resilient(page, publish_url)  # multipub patch: 慢网络
        await page.wait_for_url(publish_url)
        # multipub patch: 等上传框渲染（期间跳登录页立即报错），不再直接 set_input_files（30 秒后报 internal）
        upload_input_box = await self._wait_upload_input(page, XHS_VIDEO_INPUT_SELECTOR, "视频")
        await upload_input_box.set_input_files(self.file_path)

        # multipub patch: 带截止时间
        deadline = Deadline(self.hooks.upload_timeout, "等待视频上传完成")
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=False)
            await self._raise_if_login_page(page, deadline.step)  # multipub patch
            try:
                upload_input = await page.wait_for_selector('input.upload-input', timeout=3000)
                preview_new = await upload_input.query_selector(
                    'xpath=following-sibling::div[contains(@class, "preview-new")]')
                if preview_new:
                    # 获取整个预览区域的文本，更鲁棒地判断上传状态
                    all_text = await preview_new.inner_text()
                    # multipub patch: 先认上传失败（失败时预览区也有「重新上传」）：重传，次数用完抛 UploadFailed
                    if any(keyword in all_text for keyword in XHS_UPLOAD_FAILED_TEXTS):
                        await self._retry_video_upload(page, all_text)
                        continue
                    upload_success = any(keyword in all_text for keyword in XHS_UPLOAD_SUCCESS_TEXTS)
                    
                    if not upload_success:
                        # 检查是否有特定的状态码或百分比
                        stage_elements = await preview_new.query_selector_all('div.stage')
                        for stage in stage_elements:
                            text_content = await page.evaluate('(element) => element.textContent', stage)
                            if '上传成功' in text_content or '分辨率' in text_content:
                                upload_success = True
                                break
                    
                    if upload_success:
                        xiaohongshu_logger.success(_msg("🥳", "视频已经传完啦"))
                        break
                    
                    if self.debug:
                        normalized_text = all_text.strip().replace("\n", " ")
                        xiaohongshu_logger.debug(_msg("🧍", f"预览区域内容: {normalized_text}"))
                    xiaohongshu_logger.debug(_msg("🧍", "还没看到上传成功标识，小人继续等一会"))
                else:
                    # 尝试检查标题输入框是否已经出现，如果是，说明已经进入编辑状态
                    title_container = page.locator('input[placeholder*="填写标题"]')
                    if await title_container.count() > 0 and await title_container.is_visible():
                        xiaohongshu_logger.success(_msg("🥳", "虽然没看到预览区，但标题框出来了，小人继续"))
                        break
                    xiaohongshu_logger.debug(_msg("🧍", "还没拿到预览区域，小人继续等一会"))
            except SauError:
                raise
            except Exception as e:
                xiaohongshu_logger.debug(_msg("😵", f"上传状态还没稳定下来，小人继续观察: {e}"))
            await asyncio.sleep(2)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.set_thumbnail(page, self.thumbnail_path)

        # await self.set_location(page, "青岛市")

        # multipub patch: 上游无条件声明「来源转载」，改为显式开启（见 apply_declarations）
        await self.apply_declarations(page)
        await self.apply_collection(page)  # multipub patch

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        await self._publish_until_success(page, "视频")

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        # multipub patch: 按配置选择浏览器（上游写死 channel="chromium"）
        browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(self.headless))
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        page = None
        try:
            try:
                page = await context.new_page()
                await self.upload_video_content(page)
            except SauError:
                raise
            except Exception as exc:
                # multipub patch: 出错先截图留证；停在登录页 / 人机验证时改抛对应的异常
                await save_screenshot(page, self.hooks, "error")
                await self._classify_failure(page, exc)
                raise
            # multipub patch: 发布已成功，回写 cookie / 关浏览器出错不改变结果
            if await save_state_quietly(context, self.account_file):
                xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await close_quietly(context, browser)

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        hooks: Hooks | None = None,
        ai_generated: bool = False,
        repost_source: str | None = None,
        declaration: str | None = None,
        collection: str | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
            hooks=hooks,
            ai_generated=ai_generated,
            repost_source=repost_source,
            declaration=declaration,
            collection=collection,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await goto_resilient(page, publish_url)  # multipub patch: 慢网络
        await page.wait_for_url(publish_url)

        # multipub patch: 上游先 count() 图片上传框、没有就换视频页的选择器——SPA 还没渲染完时 count() 为 0，
        # 会错选兜底选择器；现在两种选择器一起等（期间跳登录页立即报错）
        upload_input = await self._wait_upload_input(page, XHS_IMAGE_INPUT_SELECTOR, "图片")
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        # multipub patch: 带截止时间
        deadline = Deadline(self.hooks.upload_timeout, "等待图片上传完成")
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=False)
            await self._raise_if_login_page(page, deadline.step)  # multipub patch
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文编辑区已经出来了，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        # multipub patch: 上游无条件声明「来源转载」，改为显式开启（见 apply_declarations）
        await self.apply_declarations(page)
        await self.apply_collection(page)  # multipub patch

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        # multipub patch: 标题框出来时图片可能还在上传：等传完再点发布
        await self._wait_uploads_settled(page, deadline)

        await self._publish_until_success(page, "图文")

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        # multipub patch: 按配置选择浏览器（上游写死 channel="chromium"）
        browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(self.headless))
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        page = None
        try:
            try:
                page = await context.new_page()
                await self.upload_note_content(page)
            except SauError:
                raise
            except Exception as exc:
                # multipub patch: 出错先截图留证；停在登录页 / 人机验证时改抛对应的异常
                await save_screenshot(page, self.hooks, "error")
                await self._classify_failure(page, exc)
                raise
            # multipub patch: 发布已成功，回写 cookie / 关浏览器出错不改变结果
            if await save_state_quietly(context, self.account_file):
                xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await close_quietly(context, browser)

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_note()
