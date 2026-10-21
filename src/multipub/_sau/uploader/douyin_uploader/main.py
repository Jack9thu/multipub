# -*- coding: utf-8 -*-
# vendored from social-auto-upload 0012d2c (MIT)；改动见 multipub/_sau/PATCHES.md，代码中以 "# multipub patch:" 标出
from datetime import datetime

import asyncio
import inspect
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

# multipub patch: 包内绝对导入；conf 由 multipub._sau.conf.configure() 预先设置
from multipub._sau import conf as sau_conf
from multipub._sau.conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from multipub._sau.runtime import (CookieInvalid, Deadline, DeclarationFailed, Hooks, SauError, SmsCodeTimeout,
                                   SmsVerifyFailed, UploadFailed, close_quietly, fail_rejected, fail_step,
                                   find_visible_text, publish_button_ready, save_screenshot, save_state_quietly)
from multipub._sau.uploader.base_video import BaseVideoUploader
from multipub._sau.utils.base_social_media import set_init_script
from multipub._sau.utils.login_qrcode import build_login_qrcode_path
from multipub._sau.utils.login_qrcode import decode_qrcode_from_path
from multipub._sau.utils.login_qrcode import print_terminal_qrcode
from multipub._sau.utils.login_qrcode import remove_qrcode_file
from multipub._sau.utils.login_qrcode import save_data_url_image
from multipub._sau.utils.log import douyin_logger

DOUYIN_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
DOUYIN_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
# multipub patch: 只在显式要求（或 ai_generated）时才使用的 AI 声明选项文字
DOUYIN_AI_DECLARATION = "内容由AI生成"
# multipub patch: 发布接口（被动监听回执拿作品 ID；未在真实账号上验证）
DOUYIN_CREATE_API_RE = re.compile(r"^/web/api/media/aweme/create(?:_v\d+)?/?$")
UPLOAD_LAUNCH_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _read_verify_code(code_file) -> str:
    """multipub patch: 只读文件（utf-8-sig 兼容记事本 BOM）；worker 的 stdin 已关闭，不再提示终端输入。"""
    try:
        return Path(code_file).read_text(encoding="utf-8-sig").strip()
    except (FileNotFoundError, OSError):
        return ""


async def _native_click(page, locator) -> bool:
    """对元素做"真人级"点击：真实鼠标点中心 + 派发完整 pointer/mouse 事件序列。
    抖音身份验证组件（uc_verification_component）只认这整套事件，不认单纯 click。
    返回是否点击成功。"""
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if not box:
        try:
            await locator.click(timeout=8000)
            return True
        except Exception:
            return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await page.mouse.move(x, y)
        await asyncio.sleep(0.15)
        await page.mouse.click(x, y)
        await asyncio.sleep(0.2)
        await page.evaluate(
            """({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return;
                const opts = {bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,view:window,pointerId:1,pointerType:'mouse',isPrimary:true,button:0,buttons:1};
                for (const t of ['pointerover','pointerenter','pointerdown','mousedown','pointerup','mouseup','click']) {
                    const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
                    try { el.dispatchEvent(new C(t, opts)); } catch(e){ try{ el.dispatchEvent(new MouseEvent(t,opts)); }catch(_){} }
                }
            }""",
            {"x": x, "y": y},
        )
        return True
    except Exception:
        return False


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    use_headless = os.environ.get("DOUYIN_COOKIE_AUTH_HEADLESS", "true").lower() in ("1", "true", "yes")
    # multipub patch: 按配置选择浏览器（channel / executable_path）
    launch_kwargs = sau_conf.launch_kwargs(use_headless, UPLOAD_LAUNCH_ARGS)
    for _attempt in range(3):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(storage_state=account_file)
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(2500)  # 等页面稳定，避免瞬时跳转误判
                has_login = await page.get_by_text("手机号登录").count() or await page.get_by_text("扫码登录").count()
                if "content/upload" in page.url and not has_login:
                    return True
            except Exception as exc:
                # multipub patch: 记录原因（网络错误也会被当成 cookie 失效）
                douyin_logger.warning(_msg("😵", f"cookie 校验出错（第{_attempt + 1}次）: {exc!r}"))
            finally:
                await browser.close()
    return False


async def douyin_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS, cdp_url: str | None = None, login_timeout: float | None = None):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        douyin_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        # multipub patch: 扫码等待时长可配置（上游固定 2s × 60 次）
        extra = {"max_checks": max(1, int(login_timeout // 2))} if login_timeout else {}
        result = await douyin_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless, cdp_url=cdp_url, **extra)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def _extract_douyin_qrcode_src(page: Page) -> str:
    # 等 SPA 加载完成（不只等"扫码登录"文字，否则抖音慢加载时 30s 就超时）。
    # 给 domcontentloaded 后足够时间让客户端 JS 注入登录卡。
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    scan_login_tab = page.get_by_text("扫码登录", exact=True).first
    # attached 状态：DOM 里出现即可，不要求 visible/渲染完整，避免 race
    await scan_login_tab.wait_for(state="attached", timeout=60000)

    # 新版抖音创作者中心 (single_tab + animate_qrcode_container) 不再用 aria-label="二维码"。
    # 按优先级兜底多个 selector，至少一个能命中即可。
    qrcode_selectors = [
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
    ]
    last_err: Exception | None = None
    for sel in qrcode_selectors:
        qrcode_img = page.locator(sel).first
        try:
            await qrcode_img.wait_for(state="attached", timeout=10000)
        except Exception as e:
            last_err = e
            continue
        src = await qrcode_img.get_attribute("src")
        if src:
            return src
        last_err = RuntimeError(f"selector {sel} 命中但 src 为空")

    raise RuntimeError(f"未获取到抖音登录二维码地址 (last_err={last_err})")


async def _save_douyin_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    # 提取二维码 src 仅为了保存/终端显示；定位不到时不致命——有头浏览器里二维码可见，直接扫码即可
    try:
        qrcode_src = await _extract_douyin_qrcode_src(page)
    except Exception as exc:
        douyin_logger.warning(_msg("😵", f"没定位到二维码元素（{str(exc)[:50]}）——请直接在弹出的浏览器里扫码，小人继续等登录跳转"))
        # multipub patch: 把登录页截图当作二维码图片上报（无头登录、或窗口不在前台时也能扫）
        fallback_path = build_login_qrcode_path(account_file, suffix="login_page")
        try:
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(fallback_path))
        except Exception as shot_exc:
            douyin_logger.warning(_msg("😵", f"登录页截图失败: {shot_exc!r}"))
            return {"image_path": "", "image_data_url": ""}
        if previous_qrcode_path and previous_qrcode_path != fallback_path:
            remove_qrcode_file(previous_qrcode_path)
        qrcode_info = {"image_path": str(fallback_path), "image_data_url": "", "fallback": True}
        await _emit_qrcode_callback(qrcode_callback, qrcode_info)
        return qrcode_info
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file))
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))
    douyin_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "抖音APP")
    else:
        douyin_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_douyin_login_completed(page: Page) -> bool:
    # 登录后会跳到 creator-micro 下任意页（home/content 等）；登录页是 creator.douyin.com/ 根路径
    if "creator.douyin.com/creator-micro" not in page.url:
        return False

    login_markers = [
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("手机号登录", exact=True).first,
        page.get_by_text("二维码失效", exact=True).first,
        page.get_by_role("img", name="二维码").first,
    ]

    for marker in login_markers:
        if not await marker.count():
            continue
        try:
            if await marker.is_visible():
                return False
        except Exception:
            continue

    return True


async def _wait_for_douyin_login(page: Page, account_file: str, qrcode_info: dict, qrcode_callback=None, poll_interval: int = 3, max_checks: int = 100) -> dict:
    qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    original_url = page.url
    saw_2fa = False
    for _ in range(max_checks):
        if await _is_douyin_login_completed(page):
            douyin_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "抖音扫码登录成功", account_file, qrcode_info, page.url)

        # URL 变化 + sessionid 未到位 → 二验流程，继续等
        if page.url != original_url and not await _is_douyin_login_completed(page):
            sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]')
            if await sms_input.count() > 0:
                if not saw_2fa:
                    douyin_logger.warning(_msg("⚠️", f"检测到抖音短信/安全二次验证，请在弹出的浏览器中手动输入。等待 sessionid ({_}/{max_checks})"))
                    saw_2fa = True
            await asyncio.sleep(poll_interval)
            continue

        expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
        if await expired_box.count() and await expired_box.is_visible():
            douyin_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
            await expired_box.click()
            await asyncio.sleep(1)
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None

        await asyncio.sleep(poll_interval)

    return _build_login_result(False, "timeout", "等待抖音扫码登录超时", account_file, qrcode_info, page.url)

async def douyin_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 2,
    max_checks: int = 60,
    headless: bool = LOCAL_CHROME_HEADLESS,
    cdp_url: str | None = None,
):
    async with async_playwright() as playwright:
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            should_close_context = False
        else:
            # multipub patch: 按配置选择浏览器
            browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(headless))
            context = await browser.new_context()
            should_close_context = True
        context = await set_init_script(context)
        qrcode_path = None
        result = _build_login_result(False, "failed", "抖音登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/")
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            douyin_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_douyin_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
            )
            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                # 登录已通过"发布视频"确认成功、storage_state 刚从已登录浏览器抓下来，
                # 不再用 flaky 的浏览器重检（那正是导致成功被误判为失败的老 bug）。
                # 只轻量确认文件里有 sessionid。
                try:
                    # multipub patch: 显式 UTF-8 读取（Windows 默认 GBK 会解码失败）
                    _d = json.loads(Path(account_file).read_text(encoding="utf-8-sig"))
                    _has_sess = any(c.get("name") == "sessionid" and c.get("value") for c in _d.get("cookies", []))
                    if not _has_sess:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "抖音扫码流程结束，但 cookie 中无 sessionid",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                except Exception as _e:
                    douyin_logger.warning(_msg("⚠️", f"cookie 文件校验异常（忽略，按成功处理）: {_e}"))
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                douyin_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if should_close_context:
                await context.close()
            await browser.close()
        return result


class DouYinBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        hooks: Hooks | None = None,
    ):
        self.publish_date = publish_date
        self.account_file = account_file
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless
        # multipub patch: 适配器注入的回调/超时；发布点击状态与接口回执
        self.hooks = hooks or Hooks(platform="douyin")
        self._publish_clicked = False
        self.publish_guard = self.hooks.publish_guard()
        self._sms_rounds = 0
        self.published_item_id: str | None = None
        self.publish_api_error: str | None = None
        self._response_tasks: set = set()

    async def validate_base_args(self):
        # multipub patch: 抛 CookieInvalid，适配器据此提示重新登录
        if not os.path.exists(self.account_file):
            raise CookieInvalid(f"cookie文件不存在，请先完成抖音登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise CookieInvalid(f"cookie文件已失效，请先完成抖音登录: {self.account_file}")
        if self.publish_strategy not in {DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_douyin(self, page, publish_date):
        label_element = page.locator("[class^='radio']:has-text('定时发布')")
        await label_element.click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep(1)
        await page.locator('.semi-input[placeholder="日期和时间"]').click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(publish_date_hour))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        # 2026-06 抖音发布页 DOM：标题=input[placeholder*=填写作品标题]，描述=div.zone-container[contenteditable]
        # version_2(post/video) 发布页要等视频上传完才渲染表单（实测约 40s），故等待超时给到 120s
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        await title_input.wait_for(state="visible", timeout=120000)
        await title_input.fill(title[:30])

        description_editor = page.locator('div.zone-container[contenteditable="true"]').first
        await description_editor.wait_for(state="visible", timeout=120000)
        await description_editor.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")

        # 先填正文描述，再填 #话题（此前 description 参数未被写入，导致抖音只有标签没有正文）
        if description and description.strip():
            await page.keyboard.type(description.strip())

        for tag in tags or []:
            await page.keyboard.type(" #" + tag)
            await page.keyboard.press("Space")
        await page.keyboard.press("Escape")  # 收起话题下拉，避免浮层拦截后续点击

    async def set_location(self, page: Page, location: str = ""):
        if not location:
            return
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def handle_product_dialog(self, page: Page, product_title: str):
        await page.wait_for_timeout(2000)
        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            douyin_logger.error(_msg("😵", "没找到商品短标题输入框"))
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“完成编辑”按钮"))
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return True

        douyin_logger.error(_msg("😵", "“完成编辑”按钮是灰的，小人先把弹窗关掉"))
        cancel_button = page.locator('button:has-text("取消")')
        if await cancel_button.count():
            await cancel_button.click()
        else:
            close_button = page.locator(".semi-modal-close")
            await close_button.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return False

    async def set_product_link(self, page: Page, product_link: str, product_title: str):
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            if not await dropdown.count():
                douyin_logger.error(_msg("😵", "没找到标签下拉框"))
                return False
            douyin_logger.debug(_msg("🧍", "找到标签下拉框，小人准备选择“购物车”"))
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            douyin_logger.debug(_msg("🥳", "已经选中“购物车”"))

            await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            douyin_logger.debug(_msg("🔗", f"商品链接已经填好了: {product_link}"))

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                douyin_logger.error(_msg("😵", "“添加链接”按钮现在点不了"))
                return False
            await add_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“添加链接”按钮"))

            await page.wait_for_timeout(2000)
            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                douyin_logger.error(_msg("😢", "这个商品链接无效"))
                return False

            if not await self.handle_product_dialog(page, product_title):
                return False

            douyin_logger.debug(_msg("🥳", "商品链接设置好了"))
            return True
        except Exception as e:
            douyin_logger.error(_msg("😢", f"设置商品链接时出错: {str(e)}"))
            return False

    async def set_self_declaration(self, page: Page, declaration: str) -> bool:
        """抖音「自主声明」：打开声明弹窗 → 单选声明类型 → 确定。

        真实弹窗（用户 F12 实测）：header「请选择声明类型（单选）」，选项为
        label.semi-radio 内 span.semi-radio-addon 文本，「内容由AI生成」与
        「内容为转载信息」「内容为个人观点或见解」等并列；底部 footer 的
        semi-button-primary =「确定」。

        入口/弹窗异步渲染；且填完话题后残留的 mention-wrapper/semi-portal 浮层会盖住入口，
        必须先清浮层再点。失败返回 False。

        multipub patch: 抖音组件常吞掉点击且不抛异常，所以不再"点完就算成功"：选项要真的变成选中状态、
        点「确定」后弹窗要关闭（取不到选中状态时，改为核实入口处显示了所选声明），否则返回 False
        （调用方拒绝发布）。

        Args:
            declaration: 声明类型文本（调用方显式传入）
        """
        try:
            # 清掉会遮挡入口的浮层（话题下拉/引导层），并让输入框失焦
            await self._clear_blocking_overlays(page)

            # 入口：点开声明弹窗（多个候选文案，native 仅作兜底）
            entry = None
            for etext in ["请选择自主声明", "请选择声明类型", "添加自主声明", "自主声明", "作品声明"]:
                cand = page.get_by_text(etext).first
                if await cand.count():
                    entry = cand
                    break
            if entry is not None:
                try:
                    await entry.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await entry.click(timeout=6000)
                except Exception:
                    await _native_click(page, entry)
                await page.wait_for_timeout(1200)

            # 弹窗：header「请选择声明类型（单选）」
            dialog = page.locator(".semi-modal-content").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                dialog = page.locator(".semi-modal-body").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                douyin_logger.warning(_msg("🧾", "自主声明弹窗未打开，跳过声明继续发布"))
                return False
            await dialog.first.wait_for(state="visible", timeout=6000)

            # 选项：label.semi-radio 内 span.semi-radio-addon 精确匹配
            option = dialog.locator("label.semi-radio").filter(
                has=page.locator(f'.semi-radio-addon:text-is("{declaration}")')
            ).first
            if await option.count() == 0:
                option = dialog.locator("label.semi-radio").filter(has_text=declaration).first
            checked: bool | None = None  # None：不是 label.semi-radio 结构，选中状态无从判断
            if await option.count():
                try:
                    await option.click(timeout=6000)
                except Exception:
                    await _native_click(page, option)
                await page.wait_for_timeout(400)
                checked = await self._radio_checked(option)
                if not checked:  # 普通点击被组件吞掉：派发完整原生事件再试一次
                    await _native_click(page, option)
                    await page.wait_for_timeout(400)
                    checked = await self._radio_checked(option)
                if not checked:
                    douyin_logger.warning(_msg("🧾", f"点了「{declaration}」但选项没有变成选中状态"))
                    return False
            else:
                await dialog.get_by_text(declaration, exact=True).first.click(timeout=6000, force=True)
                await page.wait_for_timeout(400)

            # 确定：footer 的 primary 按钮
            confirm_btn = dialog.locator("button.semi-button-primary").filter(has_text="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = dialog.get_by_role("button", name="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = page.get_by_role("button", name="确定").first
            if not await self._dialog_closed_after(page, dialog, confirm_btn):
                douyin_logger.warning(_msg("🧾", "点「确定」后声明弹窗没有关闭"))
                return False
            if checked is None and not await find_visible_text(page, [declaration], exclude=".semi-modal"):
                douyin_logger.warning(_msg("🧾", f"声明弹窗已关闭，但页面上没有显示「{declaration}」"))
                return False
            douyin_logger.success(_msg("🧾", f"自主声明已选择「{declaration}」"))
            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🧾", f"自主声明设置失败：{exc}"))
            return False

    @staticmethod
    async def _radio_checked(label) -> bool:
        """multipub patch: Semi 单选项是否处于选中状态（label 带 semi-radio-checked，或内部 radio 已勾选）。"""
        try:
            if "semi-radio-checked" in (await label.get_attribute("class") or ""):
                return True
            radio = label.locator('input[type="radio"]').first
            return bool(await radio.count() and await radio.is_checked())
        except Exception:
            return False

    @staticmethod
    async def _dialog_closed_after(page: Page, dialog, button) -> bool:
        """multipub patch: 点按钮后弹窗关闭才算生效；普通点击没关掉就用原生事件再点一次。"""
        for attempt in range(2):
            clicked = False
            if attempt == 0:
                try:
                    await button.click(timeout=6000)
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                clicked = await _native_click(page, button)
            try:
                await dialog.first.wait_for(state="hidden", timeout=6000)
                return True
            except Exception:
                if not clicked:  # 按钮点不到，弹窗也没关
                    return False
        return False

    async def select_bgm(self, page: Page, bgm_name: str) -> bool:
        """为图文发布选择 BGM：可选增强功能，搜索无结果或异常均跳过不中断发布。"""
        try:
            # 点击「选择音乐」按钮
            music_entry = page.locator('text="选择音乐"').nth(1)
            if not await music_entry.count():
                music_entry = page.locator('text="选择音乐"').first
            await music_entry.wait_for(state="visible", timeout=10000)
            await music_entry.click()

            # 等待侧边栏出现并搜索
            sidesheet = page.locator(".semi-sidesheet-content").first
            await sidesheet.wait_for(state="visible", timeout=8000)
            search_input = sidesheet.locator('input.semi-input[placeholder="搜索音乐"]').first
            await search_input.wait_for(state="visible", timeout=5000)
            await search_input.fill(bgm_name)
            await search_input.press("Enter")

            # 等待搜索结果
            await asyncio.sleep(2)
            first_card = sidesheet.locator(".card-container-tmocjc").first
            try:
                await first_card.wait_for(state="visible", timeout=8000)
            except Exception:
                douyin_logger.warning(_msg("🎵", f"音乐「{bgm_name}」搜索结果为空，小人跳过"))
                await self._close_music_sidesheet(page)
                return False

            # 打印找到的音乐名称
            try:
                song_name_el = first_card.locator(".song-name-oRge4d").first
                if await song_name_el.count():
                    song_name = await song_name_el.inner_text()
                    douyin_logger.info(_msg("🎵", f"小人找到了: {song_name}"))
            except Exception:
                pass

            # JS 点击「使用」（按钮 visibility:hidden，普通 click 无效）
            apply_btn = first_card.locator(".apply-btn-LUPP0D").first
            await apply_btn.evaluate("el => el.click()")
            douyin_logger.info(_msg("🥳", f"BGM「{bgm_name}」已应用"))

            # 等待侧边栏关闭，超时则手动关闭
            try:
                await sidesheet.wait_for(state="hidden", timeout=5000)
            except Exception:
                await self._close_music_sidesheet(page)

            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🎵", f"添加 BGM 时出错，跳过该步骤继续发布：{exc}"))
            try:
                await self._close_music_sidesheet(page)
            except Exception:
                pass
            return False

    async def _close_music_sidesheet(self, page: Page) -> None:
        try:
            close_btn = page.locator(".semi-sidesheet-close").first
            if await close_btn.count() and await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # multipub patch: 以下方法为新增，或从 DouYinVideo 移到基类供图文复用
    # ------------------------------------------------------------------

    def _before_publish_click(self) -> None:
        """第一次点击最终「发布」之前通知适配器（对应 ctx.mark_submit_started）。"""
        if not self._publish_clicked:
            self._publish_clicked = True
            self.hooks.call("before_publish_click")

    # 视频流程：发布按钮用 force 点击、短信弹窗用上游的宽松判定；图文流程相反（见 DouYinVideo）
    PUBLISH_CLICK_KWARGS: dict = {}
    STRICT_SMS_CHECK = True

    async def _prepare_publish_attempt(self, page: Page) -> None:
        """每轮点击「发布」前的准备（视频流程会移除拦截点击的浮层）。"""

    async def _platform_blocked_publish(self, page: Page) -> bool:
        """上一次点击是否被平台提示拦下并已处理（视频流程：「请设置封面后再发布」）。"""
        return False

    async def _publish_until_success(self, page: Page, what: str, success_url: str) -> None:
        """上游视频/图文的发布循环（选择器不变），multipub 改动：
        截止时间；短信验证有截止时间与轮数上限；「发布」只经 _click_publish 点击（PublishGuard 限制重复点击，
        第一次点击前通知适配器）；接口明确失败且没有后续验证时提前报错。"""
        self._watch_publish_response(page)
        deadline = Deadline(self.hooks.publish_timeout, f"等待{what}发布成功")
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=self._publish_clicked,
                                detail=self._publish_detail())
            try:
                await self._prepare_publish_attempt(page)
                # 检测并处理短信验证码弹窗；等验证码的时间不计入发布等待
                if await self._handle_sms_verification(page, strict=self.STRICT_SMS_CHECK):
                    deadline.reset()

                # ── 正常发布流程 ──
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await self._click_publish(publish_button, **self.PUBLISH_CLICK_KWARGS)
                await page.wait_for_url(success_url, timeout=3000)
                douyin_logger.success(_msg("🥳", f"{what}发布成功，小人开心收工"))
                break
            except SauError:
                raise
            except Exception:
                if await self._platform_blocked_publish(page):
                    self.publish_guard.blocked_by_platform()
                await self._raise_if_rejected(page)
                douyin_logger.info(_msg("🏃", f"小人正在冲刺发布{what}"))
                await asyncio.sleep(0.5)
        await self._drain_publish_responses()

    PUBLISH_CLICK_TIMEOUT_MS = 5000

    async def _click_publish(self, button, **click_kwargs) -> bool:
        """受 PublishGuard 限制地点击最终「发布」；第一次点击前通知适配器。返回是否点击了。

        按钮不唯一 / 不可见 / 不可用时不点、不记账、不标记已提交（这样的点击不会生效：
        force 点 disabled 按钮什么也不发生），继续在发布截止时间内等它变得可点。"""
        if not self.publish_guard.may_click():
            return False
        if not await publish_button_ready(button):
            douyin_logger.debug(_msg("🧍", "发布按钮还不能点（不唯一、不可见或不可用），小人再等等"))
            return False
        self._before_publish_click()
        self.publish_guard.clicked()  # 先记账再点：点击抛异常时也可能已经触发
        await button.click(timeout=self.PUBLISH_CLICK_TIMEOUT_MS, **click_kwargs)
        self.publish_guard.click_returned()
        return True

    async def _raise_if_rejected(self, page: Page) -> None:
        """发布接口明确失败、且宽限期内没有出现验证弹窗：不再等到超时，直接报平台拒绝。"""
        rejection = self.publish_guard.rejection_settled(self.hooks.reject_grace)
        if rejection:
            await fail_rejected(page, self.hooks, rejection)

    @staticmethod
    def _is_create_request(request) -> bool:
        try:
            parsed = urlparse(request.url)
            return (request.method == "POST" and parsed.netloc == "creator.douyin.com"
                    and bool(DOUYIN_CREATE_API_RE.match(parsed.path)))
        except Exception:
            return False

    def _watch_publish_response(self, page: Page) -> None:
        """被动监听发布接口（不拦截、不改动请求）：记录作品 ID / 平台错误，并告知 PublishGuard 请求状态。"""

        async def capture(response) -> None:
            ok: bool | None = None
            try:
                body = await response.json()
                if not isinstance(body, dict):
                    return
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                code = body.get("status_code")
                item_id = body.get("item_id") or data.get("item_id")
                if (200 <= response.status < 300 and code == 0 and item_id
                        and re.fullmatch(r"[1-9]\d*", str(item_id))):
                    ok = True
                    self.published_item_id = str(item_id)
                    douyin_logger.info(_msg("🧾", f"发布接口返回作品 ID: {item_id}"))
                elif isinstance(code, int) and not isinstance(code, bool) and code != 0:
                    ok = False
                    self.publish_api_error = str(body.get("status_msg") or f"status_code={code}")
                    douyin_logger.warning(_msg("😵", f"发布接口返回失败: {self.publish_api_error}"))
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

    async def _drain_publish_responses(self, timeout: float = 5) -> None:
        if self._response_tasks:
            await asyncio.wait(set(self._response_tasks), timeout=timeout)

    def _publish_detail(self) -> str:
        parts = []
        if self.published_item_id:
            parts.append(f"创建接口已返回作品 ID {self.published_item_id}")
        if self.publish_api_error:
            parts.append(f"平台返回: {self.publish_api_error}")
        return "；".join(parts)

    SMS_INPUT_SELECTOR = 'input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]'
    MAX_SMS_ROUNDS = 3
    # 图文流程（上游未验证过短信弹窗）额外要求出现验证组件的特征，避免把普通输入框误判成短信弹窗
    SMS_DIALOG_MARKERS = ('[class*="uc-ui-verify"]', "text=获取验证码", "text=身份验证", "text=短信验证")

    async def _sms_dialog_marked(self, page: Page) -> bool:
        for marker in self.SMS_DIALOG_MARKERS:
            loc = page.get_by_text(marker[5:]).first if marker.startswith("text=") else page.locator(marker).first
            try:
                if await loc.count() and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _handle_sms_verification(self, page: Page, strict: bool = False) -> bool:
        """检测并处理短信验证码弹窗（选择器与点击逻辑同上游）。返回是否处理了弹窗。

        与上游的区别：验证码只从 verify_code.txt 读取（不读 stdin），等待有截止时间，
        开始等待时通过 hooks.sms_code_required 通知一次。strict=True 时还要求出现验证组件特征。
        """
        sms_input = page.locator(self.SMS_INPUT_SELECTOR).first
        if not (await sms_input.count() and await sms_input.is_visible()):
            return False
        if strict and not await self._sms_dialog_marked(page):
            return False
        # 提交过验证码后弹窗还在 = 验证码错误/过期；轮数有上限，避免无限循环发短信
        if self._sms_rounds >= self.MAX_SMS_ROUNDS:
            shot = await save_screenshot(page, self.hooks, "sms-failed")
            raise SmsVerifyFailed(
                f"短信验证已提交 {self._sms_rounds} 次验证码，弹窗仍未消失（验证码错误或已过期）",
                step="短信验证", after_submit=self._publish_clicked, screenshot=shot,
            )
        self._sms_rounds += 1
        douyin_logger.warning(_msg("📱", f"检测到短信验证码弹窗（第 {self._sms_rounds} 次）"))
        code_file = sau_conf.verify_code_file()
        # 清掉上一次残留的验证码，避免提交过期的验证码
        try:
            code_file.unlink()
        except FileNotFoundError:
            pass
        get_code_btn = page.get_by_text("获取验证码").first
        if await get_code_btn.count() and await get_code_btn.is_visible():
            await get_code_btn.click()
            douyin_logger.info(_msg("📤", "已点击「获取验证码」，请查看手机短信"))
        code = await self._wait_verify_code(page, code_file)
        await self._submit_sms_verify_code(page, sms_input, code, str(code_file))
        # 短信验证拦下了上一次「发布」：允许再点一次（与上游一致：验证后继续点发布）
        self.publish_guard.blocked_by_platform()
        return True

    async def _wait_verify_code(self, page: Page, code_file: Path) -> str:
        self.hooks.call("sms_code_required", code_file)
        douyin_logger.warning(_msg("⏳", f"等待短信验证码：写入 {code_file}，或运行 mpub verify-code douyin <验证码>"))
        deadline = Deadline(self.hooks.sms_timeout, "等待短信验证码")
        while True:
            code = _read_verify_code(code_file)
            if code:
                return code
            if deadline.expired:
                shot = await save_screenshot(page, self.hooks, "sms-timeout")
                raise SmsCodeTimeout(
                    f"{int(deadline.seconds)} 秒内没有收到短信验证码",
                    step=deadline.step, after_submit=self._publish_clicked, screenshot=shot,
                )
            await asyncio.sleep(self.hooks.poll_interval)

    async def _submit_sms_verify_code(self, page: Page, sms_input, code: str, code_file: str) -> bool:
        douyin_logger.info(_msg("✍️", "已获取验证码，准备填入"))
        await sms_input.click()
        await sms_input.fill(code)
        douyin_logger.info(_msg("✅", "验证码已填入输入框"))
        await page.wait_for_timeout(500)

        verify_btn = page.locator('div.uc-ui-verify_sms-verify_button:has-text("验证")').first
        if await verify_btn.count() and await verify_btn.is_visible():
            try:
                await verify_btn.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(force)"))
            except Exception:
                await page.eval_on_selector('div.uc-ui-verify_sms-verify_button', 'el => el.click()')
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(JS)"))
        else:
            verify_by_text = page.get_by_text("验证", exact=True).first
            if await verify_by_text.count():
                await verify_by_text.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(text)"))
            else:
                douyin_logger.warning(_msg("⚠️", "未找到验证按钮，尝试按Enter"))
                await page.keyboard.press("Enter")

        if os.path.exists(code_file):
            os.remove(code_file)
            douyin_logger.info(_msg("🧹", "验证码文件已清理"))

        await page.wait_for_timeout(3000)
        douyin_logger.info(_msg("🔄", "验证码处理完成，继续发布流程"))
        return True


class DouYinVideo(DouYinBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_landscape_path=None,
        productLink="",
        productTitle="",
        thumbnail_portrait_path=None,
        desc: str | None = None,
        collection_name: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        declaration: str | None = None,
        sync_toutiao: bool | None = None,
        hooks: Hooks | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
            hooks=hooks,
        )
        # multipub patch: 头条/西瓜同步开关改为显式设置（上游强制打开）：None 不碰，True 打开，False 关闭
        self.sync_toutiao = None if sync_toutiao is None else bool(sync_toutiao)
        self._upload_retries = 0
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.thumbnail_landscape_path = thumbnail_landscape_path
        self.thumbnail_portrait_path = thumbnail_portrait_path
        self.productLink = productLink
        self.productTitle = productTitle
        self.desc = desc or ""
        self.collection_name = collection_name
        self.declaration = declaration.strip() if declaration and declaration.strip() else None

    async def apply_self_declaration(self, page: Page) -> None:
        if not self.declaration:
            return
        if not await self.set_self_declaration(page, self.declaration):
            # multipub patch: 专门的异常类型，适配器据此提示检查声明文字（上游是 RuntimeError）
            await save_screenshot(page, self.hooks, "declaration")
            raise DeclarationFailed(
                f"自主声明「{self.declaration}」设置失败（没能确认已选中），拒绝继续发布，作品没有发出。"
                "请确认 platforms.douyin.declaration 与页面上的选项文字完全一致，或设为 false 不声明"
            )

    SYNC_SWITCH_SELECTOR = '[class^="info"] > [class^="first-part"] div div.semi-switch'  # 上游选择器
    SYNC_SWITCH_LABEL_RE = re.compile("头条|西瓜")

    async def apply_sync_toutiao(self, page: Page) -> None:
        """multipub patch: 「同步到今日头条/西瓜视频」开关按调用方的显式设置打开或关闭，并核实。

        只动旁边写着「头条/西瓜」的那个开关（选择器是上游的旧选择器，改版后可能命中别的开关）。
        要求关闭却关不掉时拒绝发布（不能违背调用方的明确选择去同步）；要求打开却打不开只告警。
        """
        if self.sync_toutiao is None:
            return
        want = self.sync_toutiao
        switch = page.locator(self.SYNC_SWITCH_SELECTOR).first
        if not await switch.count():
            douyin_logger.warning(_msg("😵", "没找到「同步到今日头条/西瓜」开关，" + ("未开启同步" if want else "无需关闭")))
            return
        try:
            label = await switch.evaluate(
                "el => { let n = el; for (let i = 0; i < 4 && n.parentElement; i++) n = n.parentElement;"
                " return n.innerText || ''; }"
            )
        except Exception:
            label = ""
        if not self.SYNC_SWITCH_LABEL_RE.search(str(label or "")):
            douyin_logger.warning(_msg("😵", "开关旁边没有「头条/西瓜」字样，可能是页面改版，没有改动它"))
            return

        async def is_on() -> bool:
            return "semi-switch-checked" in (await switch.get_attribute("class") or "")

        if await is_on() == want:
            return
        try:
            await switch.locator("input.semi-switch-native-control").click()
            await page.wait_for_timeout(500)
            ok = await is_on() == want
        except Exception:
            ok = False
        if ok:
            douyin_logger.success(_msg("🔁", "已" + ("打开" if want else "关闭") + "「同步到今日头条/西瓜」"))
        elif want:
            douyin_logger.warning(_msg("😵", "没能打开「同步到今日头条/西瓜」开关，按不同步发布"))
        else:
            await save_screenshot(page, self.hooks, "sync-toutiao")
            raise DeclarationFailed(
                "「同步到今日头条/西瓜」开关处于打开状态且没能关闭（sync_toutiao: false），拒绝继续发布，作品没有发出。"
                "请在网页上手动关闭后重发，或删掉 sync_toutiao 保持页面默认"
            )

    async def _clear_blocking_overlays(self, page: Page) -> None:
        """清除会拦截点击的浮层：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)
        及其所在 semi-portal、其它非模态 semi-portal(tooltip/popover)、shepherd 引导层，
        并让当前输入框失焦。合集/声明下拉自身的 portal 是"点开后"才创建，故此处清理不误伤。

        根因见 recorder.log 2026-08-10 05:31：apply_collection 点合集下拉时，
        publish-mention-wrapper / semi-portal 拦截 pointer events → click 超时 → 归集被跳过。
        """
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
                    document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove());
                    document.querySelectorAll('[class*="mention-wrapper"]').forEach(e=>{ const p=e.closest('.semi-portal'); (p||e).remove(); });
                    // 关闭残留的非模态 Semi 浮层 portal（保留模态框，如声明弹窗）
                    document.querySelectorAll('.semi-portal').forEach(e=>{ if(!e.querySelector('.semi-modal, .semi-modal-content')) e.remove(); });
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(400)

    async def apply_collection(self, page: Page) -> None:
        """在发布表单页"添加合集"区选择目标合集（Semi Design select，字节组件库）。

        结构与快手（Ant Design）不同：合集名是纯文本 span.option-title-*，无 label 属性，
        用文本精确匹配。触发器用专属 class .select-collection-* 定位（页面唯一，第一级
        "合集/系列"类型下拉与此无关，不会误选）。找不到匹配合集时按 Escape 收起下拉，
        保持未选状态直接发布（界面允许留空，不阻断主发布流程）。
        """
        if not self.collection_name:
            return
        try:
            # 关键修复：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)及 semi-portal
            # 浮层盖在"添加合集"下拉上，普通 click 全点在遮罩上→超时→归集被跳过。先清浮层再点。
            await self._clear_blocking_overlays(page)

            trigger = page.locator('[class*="select-collection-"]').first
            if await trigger.count() == 0:
                douyin_logger.warning(_msg("😵", "未找到\"添加合集\"下拉框，跳过归集"))
                return
            selection = trigger.locator(".semi-select-selection")
            try:
                await selection.click(timeout=5000)
            except Exception:
                await self._clear_blocking_overlays(page)
                await _native_click(page, selection)
            await page.wait_for_timeout(800)

            option = page.locator(".semi-select-option.collection-option").filter(
                has=page.locator(f'[class*="option-title-"]:text-is("{self.collection_name}")')
            )
            if await option.count() == 0:
                douyin_logger.warning(
                    _msg("😵", f"合集下拉框未找到「{self.collection_name}」，跳过归集，保持未选状态")
                )
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                return

            try:
                await option.first.click(timeout=5000)
            except Exception:
                await _native_click(page, option.first)
            await page.wait_for_timeout(500)
            douyin_logger.success(_msg("🥳", f"已选择合集：{self.collection_name}"))
        except Exception as exc:
            douyin_logger.warning(_msg("😵", f"选择合集失败，跳过归集继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    # multipub patch: _submit_sms_verify_code 已移到 DouYinBaseUploader（图文流程复用）

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_landscape_path:
            self.thumbnail_landscape_path = str(self.validate_image_file(self.thumbnail_landscape_path))
        if self.thumbnail_portrait_path:
            self.thumbnail_portrait_path = str(self.validate_image_file(self.thumbnail_portrait_path))

    MAX_UPLOAD_RETRIES = 3
    # multipub patch: 发布循环在基类，这几项保持上游视频流程的行为
    PUBLISH_CLICK_KWARGS = {"force": True}
    STRICT_SMS_CHECK = False

    async def _prepare_publish_attempt(self, page: Page) -> None:
        # 移除会拦截发布按钮点击的新手引导/话题下拉浮层
        await page.evaluate(
            "() => { document.querySelectorAll('.shepherd-element, .shepherd-modal-overlay-container, [class*=\"mention-wrapper\"]').forEach(e => e.remove()); }"
        )

    async def _platform_blocked_publish(self, page: Page) -> bool:
        return bool(await self.handle_auto_video_cover(page))

    async def _wait_for_publish_form(self, page: Page, deadline: Deadline) -> None:
        """multipub patch: 在上传截止时间内等发布表单（标题框）出现，期间照常检查上传失败。"""
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        failed_marker = page.locator('div.progress-div > div:has-text("上传失败")')
        while not deadline.expired:
            try:
                if await title_input.count() and await title_input.is_visible():
                    return
                if await failed_marker.count():
                    douyin_logger.error(_msg("😵", "检测到上传失败，小人准备重试"))
                    await self.handle_upload_error(page)
            except SauError:
                raise
            except Exception:
                pass
            douyin_logger.debug(_msg("🧍", "发布表单还没出来（视频可能还在上传），小人继续等"))
            await asyncio.sleep(self.hooks.poll_interval)
        await fail_step(page, self.hooks, deadline, after_submit=False,
                        detail="发布表单（标题框）一直没有出现")

    async def handle_upload_error(self, page):
        # multipub patch: 重传次数有上限，超过后失败（上游会无限重传）
        self._upload_retries += 1
        if self._upload_retries > self.MAX_UPLOAD_RETRIES:
            raise UploadFailed(f"视频上传失败，已重试 {self.MAX_UPLOAD_RETRIES} 次")
        douyin_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def handle_auto_video_cover(self, page):
        if await page.get_by_text("请设置封面后再发布").first.is_visible():
            douyin_logger.info(_msg("🧍", "发布前还得先把封面弄好"))
            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count():
                douyin_logger.info(_msg("🏃", "小人去选第一个推荐封面"))
                try:
                    await recommend_cover.click()
                    await asyncio.sleep(1)
                    confirm_text = "是否确认应用此封面？"
                    if await page.get_by_text(confirm_text).first.is_visible():
                        douyin_logger.info(_msg("🪟", f"弹出确认框了: {confirm_text}"))
                        await page.get_by_role("button", name="确定").click()
                        douyin_logger.info(_msg("🥳", "推荐封面已经应用"))
                        await asyncio.sleep(1)
                    douyin_logger.info(_msg("🥳", "封面选择流程完成"))
                    return True
                except Exception as e:
                    douyin_logger.warning(_msg("😵", f"推荐封面没选成功: {e}"))
        return False

    async def set_thumbnail(self, page: Page):
        if not self.thumbnail_landscape_path and not self.thumbnail_portrait_path:
            return

        douyin_logger.info(_msg("🏃", "小人正在设置视频封面"))
        # 先清掉 shepherd 新手引导浮层，否则它会拦截封面点击导致弹窗打不开
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove())"
        )

        cover_area = page.locator('[class*="cover-"]').filter(has=page.locator("img")).first
        if not await cover_area.count():
            cover_area = page.locator('[class*="cover"]').first

        # 打开封面弹窗：抖音组件对普通/force click 常静默无效（和"完成"按钮同病），
        # 统一用 _native_click 派发完整原生事件序列；点后校验弹窗是否出现，没出现就重试。
        cover_locator_str = 'div.dy-creator-content-modal'
        cover_locator = page.locator(cover_locator_str).first
        opened = False
        # 刚上传完页面还在过渡，先等封面区渲染稳定，去掉"页面没稳就点空"这个诱因
        try:
            await cover_area.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        for attempt in range(5):
            # hover 若干次，等「编辑封面/选择封面」入口真正浮现，避免回退到封面区中心点空
            trigger = None
            trigger_txt = "封面区域"
            for _ in range(3):
                try:
                    await cover_area.hover(force=True)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
                for txt in ["编辑封面", "选择封面", "设置封面"]:
                    t = page.get_by_text(txt, exact=True).first
                    if await t.count() and await t.is_visible():
                        trigger, trigger_txt = t, txt
                        break
                if trigger is not None:
                    break
            if trigger is None:
                trigger = cover_area
            # 每轮都用 _native_click（force click 对抖音自定义组件常静默失效，白耗时间）
            await _native_click(page, trigger)
            douyin_logger.info(_msg("🖼️", f"已点「{trigger_txt}」尝试打开封面弹窗(第{attempt + 1}次)"))
            try:
                await page.wait_for_selector(cover_locator_str, timeout=5000)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            douyin_logger.warning(_msg("⚠️", "封面弹窗打不开，跳过自定义封面继续发布（交给推荐封面兜底）"))
            return

        await page.wait_for_timeout(1500)

        # 封面弹窗内有两个 input.semi-upload-hidden-input（各自还带一个 -replace 兄弟）：
        #   ① 左侧「生成参考图」(AI封面参考图)——drag 区是 semi-upload-drag-area-custom，只有个 + 图标；
        #   ② 帧选择区「上传封面」——drag 区含 .semi-upload-drag-area-main-text「点击上传文件或拖拽…」。
        # 旧代码用 .first 取到了①，封面被塞进 AI 参考图槽→真封面没设上、检测/AI生成一直转，
        # 「完成」永远关不掉弹窗→挡住发布→超时（用户 F12 实测的真根因）。
        # 改为按 main-text 拖拽区精确定位②的上传 input，取不到再 .last 兜底。
        cover_upload = cover_locator.locator(
            '.semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input'
        ).first
        if await cover_upload.count() == 0:
            cover_upload = cover_locator.locator("input.semi-upload-hidden-input").last

        if self.thumbnail_portrait_path:
            # 弹窗默认就在“设置竖封面”页；防御性点一下 tab（已激活则忽略）
            try:
                await cover_locator.get_by_text("设置竖封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            await cover_upload.set_input_files(self.thumbnail_portrait_path)
            await page.wait_for_timeout(3000)
            douyin_logger.info(_msg("🖼️", "竖版封面已上传到预览"))
        elif self.thumbnail_landscape_path:
            try:
                await cover_locator.get_by_text("设置横封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            await cover_upload.set_input_files(self.thumbnail_landscape_path)
            await page.wait_for_timeout(3000)
            douyin_logger.info(_msg("🖼️", "横版封面已上传到预览"))

        # ── 等"完成"按钮解禁：封面图处理完成前，"完成"是 semi-button-disabled，点了无效 ──
        def _finish_btn():
            return cover_locator.get_by_role("button", name="完成", exact=True).first

        for _ in range(30):  # 最多 ~15s 等图片处理、按钮解禁
            try:
                b = _finish_btn()
                if await b.count():
                    cls = await b.get_attribute("class") or ""
                    if "semi-button-disabled" not in cls:
                        break
            except Exception:
                pass
            await page.wait_for_timeout(500)

        # ── 点"完成"并验证弹窗真正 detach ──
        # 抖音自定义组件普通 click 可能不抛异常也不生效，所以每轮点后都校验弹窗是否消失：
        # 消失才算成功；否则升级 _native_click、处理可能的二次确认、最后 Esc 兜底。
        closed = False
        for attempt in range(4):
            btn = _finish_btn()
            if not await btn.count():
                btn = cover_locator.locator("button.semi-button").filter(has_text="完成").first
            if await btn.count() and await btn.is_visible():
                try:
                    await btn.click(timeout=4000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
                if await cover_locator.count() == 0:
                    closed = True
                    break
                # 普通点没关掉 → 派发完整原生事件序列
                await _native_click(page, btn)
                await page.wait_for_timeout(1500)
                if await cover_locator.count() == 0:
                    closed = True
                    break

            # 点"完成"后抖音可能弹二次确认（如未设横封面时问"确定完成？"）→ 点确认类按钮
            for cname in ["确定", "确认", "仍然完成", "仍要完成", "继续"]:
                confirm = page.locator(".semi-modal-content").get_by_role("button", name=cname, exact=True).first
                if await confirm.count() and await confirm.is_visible():
                    await _native_click(page, confirm)
                    await page.wait_for_timeout(1500)
                    break
            if await cover_locator.count() == 0:
                closed = True
                break

            # 仍没关掉：Esc 兜底后再验证一次
            douyin_logger.debug(_msg("🖼️", f"封面「完成」后弹窗未关，重试(第{attempt + 1}次)"))
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
            if await cover_locator.count() == 0:
                closed = True
                break

        if closed:
            douyin_logger.info(_msg("🥳", "视频封面设置完成，弹窗已关闭"))
        else:
            douyin_logger.warning(_msg("⚠️", "封面弹窗未能关闭，可能挡住自主声明/发布"))


    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "上传前检查通过"))

        # multipub patch: 按配置选择浏览器（上游写死 channel="chromium"）
        browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(self.headless, UPLOAD_LAUNCH_ARGS))
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)
        page = None
        # multipub patch: 出错先截图留证；无论成败（含被取消）都关闭浏览器；发布成功后的收尾出错不影响结果
        try:
            try:
                page = await context.new_page()
                await self._upload_video_steps(page)
            except SauError:
                raise
            except Exception:
                await save_screenshot(page, self.hooks, "error")
                raise
            if await save_state_quietly(context, self.account_file):
                douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
            await asyncio.sleep(2)
        finally:
            await close_quietly(context, browser)

    async def _upload_video_steps(self, page: Page) -> None:
        """上游 upload() 的页面步骤（multipub patch: 拆出来以便统一截图/关闭；循环加截止时间）。"""
        await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
        douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
        await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

        # ── 进入页面后可能弹身份验证（短信验证码）或被踢到登录页 ──
        await page.wait_for_timeout(2000)

        # 确认已经在上传页（非登录页），再找上传 input
        # 用更精确的选择器避免匹配到登录表单的 input
        upload_input = page.locator("input.upload-btn-input, div[class^='container'] input[accept]").first
        if not await upload_input.count():
            # 兜底：排除登录页的 input
            upload_input = page.locator("div[class^='container'] input[type='file'], div[class^='container'] input.upload-input").first
        if not await upload_input.count():
            # 最终兜底
            upload_input = page.locator("div[class^='container'] input").first
        await upload_input.wait_for(state="attached", timeout=60000)
        await upload_input.set_input_files(self.file_path)

        # multipub patch: 带截止时间
        deadline = Deadline(self.hooks.page_timeout, "等待进入视频发布页")
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=False)
            try:
                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入 version_1 发布页面"))
                break
            except Exception:
                try:
                    await page.wait_for_url(
                        "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                        timeout=3000,
                    )
                    douyin_logger.info(_msg("🥳", "已经进入 version_2 发布页面"))
                    break
                except Exception:
                    douyin_logger.debug(_msg("🧍", "还没进到视频发布页面，小人继续等一会"))
                    await asyncio.sleep(0.5)

        # multipub patch: 上传等待从这里开始计时：version_2 发布页要等视频传完才渲染表单，
        # 等表单和等「重新上传」共用同一个 upload_timeout（上游在填表时固定等 120 秒，大文件会失败）
        deadline = Deadline(self.hooks.upload_timeout, "等待视频上传完成")
        await self._wait_for_publish_form(page, deadline)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.desc, self.tags)
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        # multipub patch: 带截止时间；重传次数有上限（handle_upload_error 抛 UploadFailed）
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=False)
            try:
                number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
                if number > 0:
                    douyin_logger.success(_msg("🥳", "视频已经传完啦"))
                    break
                douyin_logger.info(_msg("🏃", "小人正在努力上传视频"))
                await asyncio.sleep(2)
                if await page.locator('div.progress-div > div:has-text("上传失败")').count():
                    douyin_logger.error(_msg("😵", "检测到上传失败，小人准备重试"))
                    await self.handle_upload_error(page)
            except SauError:
                raise
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等视频上传完成"))
                await asyncio.sleep(2)

        if self.productLink and self.productTitle:
            douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
            await self.set_product_link(page, self.productLink, self.productTitle)
            douyin_logger.info(_msg("🥳", "商品链接设置完成"))

        # multipub patch: 上游在未传 declaration 时默认选「内容由AI生成」，已删除；
        # 只有调用方显式传入 declaration（适配器在 ai_generated 时传 DOUYIN_AI_DECLARATION）才设置。
        await self.apply_self_declaration(page)

        # 先归集：此时尚未打开封面弹窗，避免 dy-creator-content-portal 封面浮层拦截合集下拉
        # （实测：封面弹窗在 headless 下常滞留"检测中"未关闭，会盖住"添加合集"下拉）
        await self.apply_collection(page)

        # 再设封面（放最后，关掉弹窗，避免残留浮层挡住发布按钮）
        await self.set_thumbnail(page)

        # multipub patch: 头条/西瓜同步开关只按 sync_toutiao 显式设置（None 保持页面默认），见 apply_sync_toutiao
        await self.apply_sync_toutiao(page)

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        # multipub patch: 发布循环抽到基类 _publish_until_success（带截止时间、限制重复点击、短信验证）
        await self._publish_until_success(page, "视频", "https://creator.douyin.com/creator-micro/content/manage**")

    async def douyin_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.douyin_upload_video()


class DouYinNote(DouYinBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        bgm: str = "",
        hooks: Hooks | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
            hooks=hooks,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []
        self.bgm = bgm or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if len(self.title) > 20:
            raise ValueError(f"标题不能超过20字符，当前: {len(self.title)}字符")

        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        if len(self.image_paths) > 35:
            raise ValueError("图文模式下最多只支持上传 35 张图片")

        note_len = len(self.note) if self.note else 0
        if note_len > 1000:
            raise ValueError(f"正文不能超过1000字符，当前: {note_len}字符")

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        douyin_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        douyin_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.get_by_text("发布图文", exact=True).click()
        await page.wait_for_timeout(1000)

        douyin_logger.info(_msg("📤", "小人正在上传图片"))
        await page.locator("div[class^='container'] input[accept*='image']").set_input_files(self.image_paths)

        # multipub patch: 带截止时间
        deadline = Deadline(self.hooks.upload_timeout, "等待图片上传并进入图文发布页")
        while True:
            if deadline.expired:
                await fail_step(page, self.hooks, deadline, after_submit=False)
            try:
                await page.wait_for_url(
                    "**/creator-micro/content/post/image?**",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入图文发布页面"))
                break
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等图片上传完成"))
                await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.note, self.tags)
        title_len = len(self.title) if self.title else 0
        tags_text = " ".join(f"#{t}" for t in self.tags) if self.tags else ""
        desc_and_tags_len = len(self.note or "") + (len(tags_text) + 2 if self.tags else 0)
        douyin_logger.info(_msg("📝", f"标题总字数: {title_len}，描述+话题总字数: {desc_and_tags_len}"))
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.bgm:
            await self.select_bgm(page, self.bgm)

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        # multipub patch: 发布循环抽到基类 _publish_until_success（带截止时间、限制重复点击）；
        # 上游图文流程没有处理短信验证（弹窗出现会一直空转），基类循环里会处理（strict 模式）
        await self._publish_until_success(page, "图文", "**/creator-micro/content/manage?enter_from=publish**")

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "图文上传前检查通过"))

        # multipub patch: 按配置选择浏览器（上游写死 channel="chromium"）
        browser = await playwright.chromium.launch(**sau_conf.launch_kwargs(self.headless, UPLOAD_LAUNCH_ARGS))
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)

        upload_success = False
        page = None
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
            douyin_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

            await self.upload_note_content(page)
            upload_success = True
        except SauError:
            raise
        except Exception:
            # multipub patch: 出错先截图留证
            await save_screenshot(page, self.hooks, "error")
            raise
        finally:
            # multipub patch: 收尾（回写 cookie、关浏览器）出错不改变发布结果，也不掩盖原异常
            if upload_success:
                if await save_state_quietly(context, self.account_file):
                    douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await close_quietly(context, browser)

    async def douyin_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
