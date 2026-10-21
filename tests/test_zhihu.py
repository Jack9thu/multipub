"""知乎适配器测试：全部离线（httpx.MockTransport，不开浏览器）。"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from multipub.config import load_settings
from multipub.manifest import load_manifest
from multipub.models import (ErrorCode, Kind, LoginRequired, NeedsAction, PlatformPost, PublishError, Status,
                             ValidationFailed)
from multipub.platforms import zhihu as zh
from multipub.platforms import zhihu_api as za
from multipub.platforms import zhihu_html as zm
from multipub.platforms.base import JobContext
from multipub.platforms.zhihu import ZhihuAdapter

XSRF = "xsrf-token-1"
Z_C0 = "2|1:0|10:1700000000|4:z_c0|92:Mi4xYWJj|deadbeef"
TRACE_RE = re.compile(r"^\d{13},[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
RISK_REDIRECT = "/account/unhuman?type=S6E3V1&need_login=true&session=abc"


# ------------------------------------------------------------------ helpers

def storage_state(*, z_c0_expires: float | None = None, with_xsrf: bool = True) -> dict[str, Any]:
    exp = z_c0_expires if z_c0_expires is not None else time.time() + 30 * 86400

    def ck(name: str, value: str, domain: str = ".zhihu.com", expires: float = -1) -> dict[str, Any]:
        return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
                "httpOnly": False, "secure": True, "sameSite": "Lax"}

    cookies = [ck("z_c0", Z_C0, expires=exp), ck("d_c0", "dc0-value"), ck("www_only", "w", domain="www.zhihu.com")]
    if with_xsrf:
        cookies.append(ck("_xsrf", XSRF))
    return {"cookies": cookies, "origins": []}


def write_state(path: Path, state: dict[str, Any] | None = None, *, bom: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state or storage_state(), ensure_ascii=False),
                    encoding="utf-8-sig" if bom else "utf-8")
    return path


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (64, 48), color=(10, 200, 30)) -> bytes:
    buf = io.BytesIO()
    mode = "RGBA" if fmt in ("PNG", "WEBP") else "RGB"
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def independent_oss_sig(key: str, string_to_sign: str) -> str:
    return base64.b64encode(hmac.new(key.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()


class FakeZhihu:
    """按 host + path 返回知乎接口的假数据，记录所有请求。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: dict[tuple[str, str, str], Any] = {}
        self.known_hashes: set[str] = set()  # 这些图片登记时返回 state=1（知乎已有）
        self.processing_polls = 1  # 每张图先返回几次 processing
        self.polls: dict[str, int] = {}
        self.publish_response: Any = {"code": 0, "message": "success",
                                      "data": {"result": json.dumps({"publish": {"id": "777"}})}}
        self.article_visible = True
        self.remote = {"/a.webp": image_bytes("WEBP", (30, 20))}

    def json_body(self, request: httpx.Request) -> Any:
        return json.loads(request.content.decode("utf-8")) if request.content else None

    def find(self, method: str, host: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.host == host and r.url.path == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, host, path = request.method, request.url.host, request.url.path
        override = self.overrides.get((method, host, path))
        if override is not None:
            return override(request) if callable(override) else override
        if host == "www.zhihu.com":
            if path == "/api/v4/me":
                return httpx.Response(200, json={"id": "u1", "name": "测试用户", "is_bind_phone": True})
            if path == "/api/v4/content/drafts":
                return httpx.Response(200, json={"code": 0, "data": {"content_id": "888"}})
            if path == "/api/v4/content/publish":
                pr = self.publish_response
                return pr if isinstance(pr, httpx.Response) else httpx.Response(200, json=pr)
            if path.startswith("/api/v4/articles/"):
                if not self.article_visible:
                    return httpx.Response(404, json={"error": {"code": 4041, "message": "资源不存在"}})
                return httpx.Response(200, json={"id": int(path.rsplit("/", 1)[-1]), "title": "x"})
            if path.startswith("/api/v4/pins/"):
                return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        if host == "zhuanlan.zhihu.com":
            if path == "/api/articles/drafts" and method == "POST":
                return httpx.Response(200, json={"id": "555", "title": self.json_body(request)["title"]})
            if path == "/api/articles/555/draft" and method == "PATCH":
                return httpx.Response(200, json={})
            if path == "/api/autocomplete/topics":
                token = request.url.params["token"]
                if token == "不存在":
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[{"id": "1", "name": token + "相关", "type": "topic"},
                                                  {"id": "19550517", "name": token, "type": "topic"}])
            if path == "/api/articles/555/topics" and method == "POST":
                return httpx.Response(200, json=self.json_body(request))
        if host == "api.zhihu.com":
            if path == "/images" and method == "POST":
                md5 = self.json_body(request)["image_hash"]
                state = 1 if md5 in self.known_hashes else 2
                return httpx.Response(200, json={
                    "upload_file": {"state": state, "image_id": f"id-{md5[:8]}", "object_key": f"v2-{md5}"},
                    "upload_token": {"access_id": "AID", "access_key": "AKEY", "access_token": "STS-TOKEN"},
                })
            if path.startswith("/images/") and method == "GET":
                image_id = path.rsplit("/", 1)[-1]
                n = self.polls[image_id] = self.polls.get(image_id, 0) + 1
                if n <= self.processing_polls:
                    return httpx.Response(200, json={"status": "processing"})
                h = image_id[3:]
                return httpx.Response(200, json={
                    "status": "success", "src": f"https://pic1.zhimg.com/v2-{h}",
                    "original_src": f"https://picx.zhimg.com/v2-{h}", "watermark": "watermark",
                    "watermark_src": f"https://pic2.zhimg.com/v2-{h}_wm"})
        if host == "zhihu-pics-upload.zhimg.com" and method == "PUT":
            return httpx.Response(200)
        if host == "img.example.com" and path in self.remote:
            return httpx.Response(200, content=self.remote[path], headers={"Content-Type": "image/webp"})
        return httpx.Response(404, json={"error": {"code": 404, "message": f"no route {method} {host}{path}"}})


@pytest.fixture
def settings(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[platforms.zhihu]\nwrite_interval = 0\nimage_timeout = 5\n", encoding="utf-8")
    s = load_settings(home)
    s.ensure_dirs()
    return s


@pytest.fixture
def fake(monkeypatch):
    f = FakeZhihu()
    transport = httpx.MockTransport(f)
    monkeypatch.setattr(za, "make_client", functools.partial(za.make_client, transport=transport))
    monkeypatch.setattr(za, "make_download_client", functools.partial(za.make_download_client, transport=transport))
    monkeypatch.setattr(za, "IMAGE_POLL_INTERVAL", 0)
    monkeypatch.setattr(za, "MIN_WRITE_INTERVAL", 0)
    return f


def make_ctx(settings, action: str = "publish", account: str = "main") -> tuple[JobContext, list[dict]]:
    events: list[dict] = []
    ctx = JobContext(settings=settings, platform="zhihu", account=account, action=action,
                     headless=True, run_dir=settings.runs_dir / "t", emit=events.append)
    return ctx, events


@pytest.fixture
def env(settings, fake):
    write_state(settings.account_file("zhihu", "main"))
    ctx, events = make_ctx(settings)
    return ctx, events, fake


ARTICLE_MD = """# 测试文章

第一段，**重点**。
第二行

![图一](pics/%E5%9B%BE.png)

## 小节

![远程](https://img.example.com/a.webp)
"""


def article_post(media: Path, *, zhihu: str = "", extra_top: str = "") -> PlatformPost:
    content = media / "content"
    (content / "pics").mkdir(parents=True, exist_ok=True)
    (content / "pics" / "图.png").write_bytes(image_bytes("PNG", (40, 30)))
    (content / "article.md").write_text(ARTICLE_MD, encoding="utf-8")
    (media / "post.yaml").write_text(
        "kind: article\ntitle: 测试文章\nbody_file: content/article.md\ncover: cover.jpg\n"
        "tags: [人工智能, 不存在]\nimages: [images/2.jpg]\naccount: main\n" + extra_top +
        "platforms:\n  zhihu:\n" + (zhihu or "    toc: false\n"), encoding="utf-8")
    return load_manifest(media / "post.yaml").for_platform("zhihu")


def note_post(media: Path, **kw: Any) -> PlatformPost:
    base = dict(platform="zhihu", account="main", kind=Kind.NOTE, title="周末徒步", body="正文第一行\n\n第二行 <b>",
                tags=["徒步", "周末"], images=[media / "images" / "1.jpg", media / "images" / "2.jpg"],
                options={"tags_in_text": True}, source=str(media / "post.yaml"))
    base.update(kw)
    return PlatformPost(**base)


# ------------------------------------------------------------------ Markdown → 知乎 HTML

def zimg(h: str = "abc", ext: str = "jpg") -> zm.ZhihuImage:
    return zm.ZhihuImage(src=f"https://pic1.zhimg.com/v2-{h}", original_src=f"https://picx.zhimg.com/v2-{h}",
                         watermark="watermark", watermark_src=f"https://pic2.zhimg.com/v2-{h}_wm",
                         width=40, height=30, ext=ext)


def test_markdown_dialect():
    md = (
        "# 标题\n\n第一行\n第二行 **粗** *斜* ~~删~~ `a<b>` [链接](https://x.com) [坏](javascript:alert(1))\n\n"
        "## 一节\n\n### 小节\n\n#### 更深\n\n"
        "![说明](img/1.png)\n\n"
        "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n\n"
        "```python\nprint('<x>')\n```\n\n"
        "行内公式 $a+b$ 和脚注[^1]\n\n$$\nE=mc^2\n$$\n\n"
        "<!-- 注释 -->\n\n- 甲\n- 乙\n\n> 引用\n\n[^1]: 参考资料 https://example.com/ref\n"
    )
    html = zm.render_article(md, {"img/1.png": zimg("abc", "png")}, title="标题")
    assert not html.startswith("<h") and "标题" not in html  # 唯一的一级标题被丢弃
    assert "<p>第一行</p><p>第二行 <b>粗</b> <i>斜</i> <del>删</del> <code>a&lt;b&gt;</code> " in html
    assert '<a href="https://x.com">链接</a>' in html and '<a href="#">坏</a>' in html
    # 最浅的二级 → h2，三级 → h3，四级 → 加粗段落
    assert "<h2>一节</h2><h3>小节</h3><p><b>更深</b></p>" in html
    assert ('<p><img src="https://picx.zhimg.com/v2-abc.png" data-caption="说明" data-size="normal" '
            'data-rawwidth="40" data-rawheight="30" data-watermark="watermark" '
            'data-original-src="https://picx.zhimg.com/v2-abc.png" '
            'data-watermark-src="https://pic2.zhimg.com/v2-abc_wm.png" data-private-watermark-src=""/></p>') in html
    assert ('<table data-draft-node="block" data-draft-type="table" data-size="normal" data-row-style="normal">'
            "<tbody><tr><th>甲</th><th>乙</th></tr><tr><td>1</td><td>2</td></tr></tbody></table>") in html
    assert "<thead>" not in html
    assert '<pre lang="python">print(\'&lt;x&gt;\')</pre>' in html
    assert '<img eeimg="1" src="//www.zhihu.com/equation?tex=a%2Bb" alt="a+b"/>' in html
    assert '<p><img eeimg="2" src="//www.zhihu.com/equation?tex=E%3Dmc%5E2" alt="E=mc^2"/></p>' in html
    assert ('<sup data-text="参考资料" data-url="https://example.com/ref" data-draft-node="inline" '
            'data-draft-type="reference" data-numero="1">[1]</sup>') in html
    assert "注释" not in html and "<section" not in html  # 注释与脚注列表去掉
    assert "<ul><li>甲</li><li>乙</li></ul>" in html and "<blockquote><p>引用</p></blockquote>" in html


def test_soft_breaks_inside_inline_tags_do_not_split_paragraph():
    # 换行在粗体 / 链接 / 斜体里面：不能拆成两个 <p>（否则标签交错），改成 <br/>
    assert zm.render_article("**粗体跨\n两行**结束") == "<p><b>粗体跨<br/>两行</b>结束</p>"
    assert zm.render_article("[链接文字\n第二行](https://x.com)") == \
        '<p><a href="https://x.com">链接文字<br/>第二行</a></p>'
    assert zm.render_article("前\n*斜\n体* 后\n~~删  \n除~~") == \
        "<p>前</p><p><i>斜<br/>体</i> 后</p><p><del>删<br/>除</del></p>"
    assert zm.render_article("- **甲\n乙**\n- 丙") == "<ul><li><b>甲<br/>乙</b></li><li>丙</li></ul>"
    assert zm.render_article("正文\n\n**标\n题**\n---") == "<p>正文</p><h2><b>标 题</b></h2>"
    assert "\ue000" not in zm.render_article("a **b\nc** d\ne") and "\ue001" not in zm.render_article("**b\nc**")


def test_markdown_headings_kept_when_h1_used_as_sections():
    html = zm.render_article("# 第一章\n\n正文\n\n# 第二章\n\n## 一节\n", title="书名")
    assert html == "<h2>第一章</h2><p>正文</p><h2>第二章</h2><h3>一节</h3>"
    # 与标题相同的一级标题即使不唯一也丢掉
    assert zm.render_article("# 书名\n\n# 附录\n", title="书名") == "<h2>附录</h2>"
    assert zm.render_article("正文", append=[zimg("t")]).startswith("<p>正文</p><p><img ")


def test_front_matter_stripped():
    md = "---\ntitle: x\ntags: [a]\n---\n# 标题\n\n正文\n"
    assert zm.render_article(md, title="标题") == "<p>正文</p>"
    assert zm.strip_front_matter("---\n\n正文") == "---\n\n正文"  # 没有闭合的不动
    assert zm.render_article("正文\n\n---\n\n下一段") == "<p>正文</p><hr/><p>下一段</p>"


def test_markdown_missing_image_and_zhimg_passthrough():
    with pytest.raises(ValueError):
        zm.render_article("![](x.png)")
    assert zm.render_article("前![]()后") == "<p>前后</p>"  # 空图片丢掉，不报错
    assert zm.raw_html_images('<img src="a.png">\n\n```\n<img src="code.png">\n```\n') == ["a.png"]
    html = zm.render_article("![a](https://pic3.zhimg.com/v2-xyz.jpg)")
    assert '<img src="https://pic3.zhimg.com/v2-xyz.jpg" data-caption="a"' in html
    assert zm.collect_image_urls("![a](x.png) ![b](x.png) ![c](https://e.com/y.gif)") == ["x.png", "https://e.com/y.gif"]


def test_html_helpers():
    assert zm.with_ext("https://picx.zhimg.com/v2-abc", "png") == "https://picx.zhimg.com/v2-abc.png"
    assert zm.with_ext("https://picx.zhimg.com/v2-abc.jpg", "png") == "https://picx.zhimg.com/v2-abc.jpg"
    assert zm.with_ext("https://pic.zhimg.com/v2-abc?x=1", "jpg") == "https://pic.zhimg.com/v2-abc.jpg?x=1"
    assert zm.is_zhimg("https://pic1.zhimg.com/a") and zm.is_zhimg("//picx.zhimg.com/a")
    assert not zm.is_zhimg("https://zhimg.com.evil.cn/a")
    assert zm.text_length("<p>a&amp;b</p><img src='x'/><p>中文</p>") == 5
    html = zm.pin_html("第一行\n\n <b>二</b> \n", [zimg("p")])
    assert html.startswith("<p>第一行</p><p>&lt;b&gt;二&lt;/b&gt;</p><img src=\"https://pic1.zhimg.com/v2-p\"")
    assert 'data-original-src="https://picx.zhimg.com/v2-p"' in html


# ------------------------------------------------------------------ OSS 签名、cookie、请求体

def test_oss_signature_deterministic():
    date = "Tue, 20 Oct 2026 08:00:00 GMT"
    token = {"access_id": "AID", "access_key": "AKEY", "access_token": "STS-TOKEN"}
    headers = za.oss_put_headers(token, "v2-abc", "image/png", date)
    expected_sts = (f"PUT\n\nimage/png\n{date}\nx-oss-date:{date}\nx-oss-security-token:STS-TOKEN\n"
                    f"x-oss-user-agent:{za.OSS_UA}\n/zhihu-pics/v2-abc")
    assert headers["Authorization"] == f"OSS AID:{independent_oss_sig('AKEY', expected_sts)}"
    assert headers["x-oss-date"] == headers["Date"] == date
    assert headers["Content-Type"] == "image/png" and headers["Origin"] == "https://zhuanlan.zhihu.com"
    # 与 KrisTHL181 的最小写法（只有安全令牌一个 x-oss 头）一致
    minimal = za.oss_signature("AKEY", method="PUT", content_type="image/jpeg", date=date,
                               oss_headers={"X-OSS-Security-Token": "STS"}, resource="/zhihu-pics/v2-k")
    assert minimal == independent_oss_sig("AKEY", f"PUT\n\nimage/jpeg\n{date}\nx-oss-security-token:STS\n/zhihu-pics/v2-k")


async def test_cookie_jar_from_storage_state(tmp_path: Path):
    state = storage_state()
    state["cookies"].append({"name": "old", "value": "gone", "domain": ".zhihu.com", "path": "/",
                             "expires": time.time() - 10})
    path = write_state(tmp_path / "a.json", state)
    cookies = za.load_cookies(path, account="main")
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("cookie")
        return httpx.Response(200, json={})

    async with za.make_client(cookies, transport=httpx.MockTransport(handler)) as client:
        for url in ("https://www.zhihu.com/x", "https://zhuanlan.zhihu.com/x", "https://api.zhihu.com/x",
                    "https://zhihu-pics-upload.zhimg.com/x"):
            await client.get(url)
        assert za.ZhihuApi(client, account="main").xsrf == XSRF
    assert f"z_c0={Z_C0}" in seen["www.zhihu.com"] and "www_only=w" in seen["www.zhihu.com"]
    assert "z_c0=" in seen["zhuanlan.zhihu.com"] and "www_only" not in seen["zhuanlan.zhihu.com"]
    assert "_xsrf=" in seen["api.zhihu.com"]
    assert "old=" not in seen["www.zhihu.com"]  # 过期的不发
    assert seen["zhihu-pics-upload.zhimg.com"] is None  # 不把知乎 cookie 发给图床


def test_load_cookies_formats_and_errors(tmp_path: Path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"z_c0": "Z", "_xsrf": "X"}), encoding="utf-8")
    cookies = za.load_cookies(p, account="a")
    assert {c["name"]: c["domain"] for c in cookies} == {"z_c0": ".zhihu.com", "_xsrf": ".zhihu.com"}
    p.write_text(json.dumps([{"name": "z_c0", "value": "Z", "domain": ".zhihu.com", "expirationDate": 1}]),
                 encoding="utf-8")
    with pytest.raises(LoginRequired, match="过期"):
        za.load_cookies(p, account="a")
    with pytest.raises(LoginRequired, match="mpub login zhihu --account a"):
        za.load_cookies(tmp_path / "missing.json", account="a")
    write_state(p, {"cookies": [{"name": "_xsrf", "value": "x", "domain": ".zhihu.com"}]})
    with pytest.raises(LoginRequired, match="z_c0"):
        za.load_cookies(p, account="a")
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(LoginRequired):
        za.load_cookies(p, account="a")


def test_publish_payload_builders_and_id_parsing():
    payload = za.article_publish_payload(draft_id="555", title="T", html="<p>x</p>", text_len=1,
                                         disclaimer_type="ai_creation", toc=True, column="c_1")
    d = payload["data"]
    assert payload["action"] == "article" and TRACE_RE.match(d["publish"]["traceId"])
    business = json.loads(d["extra_info"]["pc_business_params"])
    assert isinstance(d["extra_info"]["pc_business_params"], str)
    assert business["disclaimer_type"] == "ai_creation" and business["disclaimer_status"] == "open"
    assert business["table_of_contents_enabled"] is True and "column" not in business
    assert business["commercial_report_info"] == {"commercial_types": []} and business["canReward"] is False
    assert d["column"] == {"column": ["c_1"]} and d["draft"] == {"disabled": 1, "id": "555", "isPublished": False}
    plain = json.loads(za.article_publish_payload(draft_id="1", title="T", html="", text_len=0)
                       ["data"]["extra_info"]["pc_business_params"])
    assert plain["column"] is None and plain["disclaimer_status"] == "close"

    assert za.parse_publish_id({"code": 0, "data": {"result": json.dumps({"publish": {"id": 12}})}}) == "12"
    assert za.parse_publish_id({"code": 0, "data": {"result": json.dumps({"id": "34"})}}) == "34"
    assert za.parse_publish_id({"code": 0, "data": {"result": "not json"}}) == ""
    assert za.parse_publish_id({"code": 0, "data": {"id": 56}}) == "56"
    assert za.parse_publish_id(None) == ""


@pytest.mark.parametrize("status,body,headers,exc,code,retryable", [
    (401, {"error": {"code": 100, "message": "ERR_TICKET_NOT_EXIST"}}, {}, LoginRequired, ErrorCode.LOGIN_REQUIRED, False),
    (403, {"error": {"code": 40352, "redirect": RISK_REDIRECT}}, {}, NeedsAction, ErrorCode.RISK_CONTROL, False),
    (403, "<html>zh-zse-ck</html>", {}, NeedsAction, ErrorCode.RISK_CONTROL, False),
    (429, {}, {}, NeedsAction, ErrorCode.RISK_CONTROL, False),
    (302, b"", {"location": "https://www.zhihu.com/signin?next=x"}, LoginRequired, ErrorCode.LOGIN_REQUIRED, False),
    (502, b"", {}, PublishError, ErrorCode.PLATFORM_ERROR, True),
    (400, {"error": {"code": 4001, "message": "标题太长"}}, {}, PublishError, ErrorCode.PLATFORM_ERROR, False),
    (403, {"error": {"code": 10003, "message": "请求参数异常，请升级客户端后重试"}}, {}, PublishError,
     ErrorCode.PLATFORM_ERROR, False),
])
def test_raise_for_response(status, body, headers, exc, code, retryable):
    kw = {"json": body} if isinstance(body, dict) else {"content": body.encode() if isinstance(body, str) else body}
    resp = httpx.Response(status, headers=headers, **kw)
    with pytest.raises(exc) as info:
        za.raise_for_response(resp, za._json(resp), what="测试", account="main")
    assert info.value.code is code and info.value.retryable is retryable
    if status == 400:
        assert "标题太长" in str(info.value)
    if body == {"error": {"code": 10003, "message": "请求参数异常，请升级客户端后重试"}}:
        assert "x-zse-96" in str(info.value)
    if code is ErrorCode.RISK_CONTROL and isinstance(body, dict) and body.get("error"):
        assert info.value.url == "https://www.zhihu.com" + RISK_REDIRECT
        assert "https://www.zhihu.com/account/unhuman?type=S6E3V1" in str(info.value)


def test_raise_for_response_after_submit_and_codes():
    ok = httpx.Response(200, json={"code": 0})
    za.raise_for_response(ok, {"code": 0}, what="x", account="a", check_code=True)
    bad = httpx.Response(200, json={"code": 2000, "message": "发布失败"})
    with pytest.raises(PublishError, match="2000: 发布失败"):
        za.raise_for_response(bad, bad.json(), what="x", account="a", check_code=True)
    for resp in (httpx.Response(500), httpx.Response(200, content=b"<html>")):
        with pytest.raises(PublishError) as info:
            za.raise_for_response(resp, za._json(resp), what="发布", account="a", after_submit=True)
        assert info.value.code is ErrorCode.UNKNOWN_OUTCOME
    explicit = httpx.Response(400, json={"error": {"message": "内容违规"}})
    with pytest.raises(PublishError) as info:
        za.raise_for_response(explicit, explicit.json(), what="发布", account="a", after_submit=True)
    assert info.value.code is ErrorCode.PLATFORM_ERROR


def test_prepare_image_conversions():
    png = za.prepare_image(image_bytes("PNG"), "a.png")
    assert (png.mime, png.ext, png.converted_from, png.width, png.height) == ("image/png", "png", "", 64, 48)
    webp = za.prepare_image(image_bytes("WEBP", (20, 10)), "b.webp")
    assert webp.mime == "image/jpeg" and webp.converted_from == "WEBP" and webp.data[:2] == b"\xff\xd8"
    gif = za.prepare_image(image_bytes("GIF", (8, 8)), "c.gif")
    assert gif.converted_from == "GIF" and (gif.width, gif.height) == (8, 8)
    jpg = image_bytes("JPEG")
    assert za.prepare_image(jpg, "d.jpg").data == jpg
    with pytest.raises(ValidationFailed):
        za.prepare_image(b"not an image", "e.png")


def _exif(orientation: int) -> Image.Exif:
    exif = Image.Exif()
    exif[za.EXIF_ORIENTATION] = orientation
    return exif


def test_prepare_image_mpo_and_exif_orientation(monkeypatch):
    # 手机 Ultra HDR / 深度图照片：Pillow 认成 MPO，按 JPEG 原样上传（保留 EXIF，不重新压缩）
    buf = io.BytesIO()
    Image.new("RGB", (80, 40), (255, 0, 0)).save(buf, "MPO", save_all=True, exif=_exif(6),
                                                 append_images=[Image.new("RGB", (80, 40), (0, 255, 0))])
    mpo = buf.getvalue()
    with Image.open(io.BytesIO(mpo)) as im:
        assert im.format == "MPO"
    p = za.prepare_image(mpo, "pixel.jpg")
    assert (p.mime, p.ext, p.converted_from, p.data) == ("image/jpeg", "jpg", "", mpo)
    assert (p.width, p.height) == (40, 80)  # 竖拍：宽高按显示方向

    buf = io.BytesIO()
    Image.new("RGB", (80, 40)).save(buf, "JPEG", exif=_exif(6))
    jpg = za.prepare_image(buf.getvalue(), "iphone.jpg")
    assert jpg.data == buf.getvalue() and (jpg.width, jpg.height) == (40, 80)
    buf = io.BytesIO()
    Image.new("RGB", (80, 40)).save(buf, "JPEG", exif=_exif(3))  # 180° 不换宽高
    assert (za.prepare_image(buf.getvalue(), "x.jpg").width, za.prepare_image(buf.getvalue(), "x.jpg").height) \
        == (80, 40)

    # 要转码的图（转码会丢 EXIF）：先把像素转正
    src = Image.new("RGB", (80, 40), (255, 255, 255))
    src.paste((255, 0, 0), (0, 0, 10, 10))  # 左上角红块
    buf = io.BytesIO()
    src.save(buf, "WEBP", lossless=True, exif=_exif(6))
    webp = za.prepare_image(buf.getvalue(), "rot.webp")
    assert webp.converted_from == "WEBP" and (webp.width, webp.height) == (40, 80)
    with Image.open(io.BytesIO(webp.data)) as out:
        assert out.size == (40, 80) and not out.getexif().get(za.EXIF_ORIENTATION)
        r, g, b = out.convert("RGB").getpixel((35, 4))  # 顺时针转 90° 后红块在右上角
        assert r > 200 and g < 80 and b < 80

    # 解压炸弹：不是内部错误，而是 validation
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValidationFailed, match="分辨率太大"):
        za.prepare_image(image_bytes("PNG", (64, 48)), "huge.png")


# ------------------------------------------------------------------ 图片上传

async def test_upload_image_state2_oss_put_and_polling(fake):
    fake.processing_polls = 2
    data = image_bytes("PNG")
    md5 = hashlib.md5(data).hexdigest()
    async with za.make_client(za.normalize_cookies(storage_state())) as client:
        api = za.ZhihuApi(client, account="main", write_interval=0)
        img = await api.upload_image(za.prepare_image(data, "a.png"), source="article",
                                     referer="https://zhuanlan.zhihu.com/write")
        again = await api.upload_image(za.prepare_image(data, "a.png"), source="article",
                                       referer="https://zhuanlan.zhihu.com/write")
    assert again is img and len(fake.find("POST", "api.zhihu.com", "/images")) == 1  # 同一张图只传一次
    reg = fake.find("POST", "api.zhihu.com", "/images")[0]
    assert fake.json_body(reg) == {"image_hash": md5, "source": "article"}
    assert reg.headers["x-xsrftoken"] == XSRF and reg.headers["origin"] == "https://zhuanlan.zhihu.com"
    put = fake.find("PUT", "zhihu-pics-upload.zhimg.com", f"/v2-{md5}")[0]
    assert put.content == data and "cookie" not in put.headers and "x-xsrftoken" not in put.headers
    sts = (f"PUT\n\nimage/png\n{put.headers['date']}\nx-oss-date:{put.headers['x-oss-date']}\n"
           f"x-oss-security-token:STS-TOKEN\nx-oss-user-agent:{za.OSS_UA}\n/zhihu-pics/v2-{md5}")
    assert put.headers["authorization"] == f"OSS AID:{independent_oss_sig('AKEY', sts)}"
    assert len(fake.find("GET", "api.zhihu.com", f"/images/id-{md5[:8]}")) == 3  # 2 次 processing + 1 次 success
    assert img.original_src == f"https://picx.zhimg.com/v2-{md5[:8]}" and img.ext == "png"
    assert (img.width, img.height, img.md5) == (64, 48, md5)


async def test_upload_image_state1_skips_oss_and_timeout(fake, monkeypatch):
    data = image_bytes("JPEG")
    fake.known_hashes.add(hashlib.md5(data).hexdigest())
    fake.processing_polls = 0
    async with za.make_client(za.normalize_cookies(storage_state())) as client:
        api = za.ZhihuApi(client, account="main", write_interval=0)
        await api.upload_image(za.prepare_image(data, "k.jpg"), source="pin", referer=za.WWW_HOME)
        assert not [r for r in fake.requests if r.method == "PUT"]
        assert fake.json_body(fake.find("POST", "api.zhihu.com", "/images")[0])["source"] == "pin"

        fake.processing_polls = 10 ** 6
        api.image_timeout = 0
        with pytest.raises(PublishError) as info:
            await api.upload_image(za.prepare_image(image_bytes("PNG", (5, 5)), "slow.png"),
                                   source="article", referer=za.WWW_HOME)
    assert info.value.code is ErrorCode.TIMEOUT and info.value.retryable


async def test_write_interval_floor(settings, fake, monkeypatch):
    write_state(settings.account_file("zhihu", "main"))
    ctx, _ = make_ctx(settings)
    monkeypatch.setattr(za, "MIN_WRITE_INTERVAL", 1.0)
    async with ZhihuAdapter(ctx)._api() as api:  # config.toml 里配了 0
        assert api.write_interval == 1.0 and api.image_timeout == 5


async def test_write_throttle(fake, monkeypatch):
    sleeps: list[float] = []
    order: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        order.append("sleep")

    monkeypatch.setattr(za.asyncio, "sleep", fake_sleep)
    async with za.make_client(za.normalize_cookies(storage_state())) as client:
        api = za.ZhihuApi(client, account="main", write_interval=2.0)
        await api.me()  # GET 不限速
        await api.create_content_draft("pin")
        await api.create_content_draft("pin")
        assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 2.0
        n_before = len(fake.requests)
        await api.publish_content({"action": "pin", "data": {}}, origin=za.WWW, referer=za.WWW_HOME,
                                  mark_submit=lambda: order.append(f"mark@{len(fake.requests)}"))
    # 限速等待在前，submit_started 紧挨着真正的发布请求
    assert order[-2:] == ["sleep", f"mark@{n_before}"] and len(fake.requests) == n_before + 1


# ------------------------------------------------------------------ 文章发布

async def test_article_publish_flow(env, media: Path):
    ctx, events, fake = env
    cover_md5 = hashlib.md5((media / "cover.jpg").read_bytes()).hexdigest()
    fake.known_hashes.add(cover_md5)  # 封面知乎已有：不走 OSS
    post = article_post(media, extra_top="ai_generated: true\n",
                        zhihu="    column: https://www.zhihu.com/column/c_123\n    toc: true\n")
    result = await ZhihuAdapter(ctx).publish(post)

    assert result.status is Status.PUBLISHED, result.message
    assert result.url == "https://zhuanlan.zhihu.com/p/777" and result.item_id == "777"
    assert result.submit_started and any(e["event"] == "submit_started" for e in events)
    assert "没找到话题「不存在」" in result.message

    regs = [fake.json_body(r) for r in fake.find("POST", "api.zhihu.com", "/images")]
    assert [r["source"] for r in regs] == ["article"] * 4  # 本地 png、远程 webp、追加的 2.jpg、封面
    assert len([r for r in fake.requests if r.method == "PUT"]) == 3
    assert fake.find("GET", "img.example.com", "/a.webp")[0].headers.get("cookie") is None

    draft = fake.find("POST", "zhuanlan.zhihu.com", "/api/articles/drafts")[0]
    assert fake.json_body(draft) == {"title": "测试文章", "delta_time": 0, "can_reward": False}
    assert draft.headers["referer"] == "https://zhuanlan.zhihu.com/write"
    patches = [fake.json_body(r) for r in fake.find("PATCH", "zhuanlan.zhihu.com", "/api/articles/555/draft")]
    content = patches[0]["content"]
    assert patches[0]["title"] == "测试文章" and patches[0]["table_of_contents"] is True
    assert content.startswith("<p>第一段，<b>重点</b>。</p><p>第二行</p><p><img ")
    assert "<h2>小节</h2>" in content and "测试文章" not in content
    png_md5 = hashlib.md5((media / "content" / "pics" / "图.png").read_bytes()).hexdigest()
    assert f'data-original-src="https://picx.zhimg.com/v2-{png_md5[:8]}.png"' in content
    assert content.count("<img ") == 3 and content.endswith("/></p>")  # 远程图转成了 jpg、2.jpg 在文末
    assert patches[1] == {"titleImage": f"https://picx.zhimg.com/v2-{cover_md5[:8]}",
                          "isTitleImageFullScreen": False, "delta_time": 30}

    topic_posts = fake.find("POST", "zhuanlan.zhihu.com", "/api/articles/555/topics")
    assert [fake.json_body(r)["id"] for r in topic_posts] == ["19550517"]  # 名字完全相同的优先

    pub = fake.find("POST", "www.zhihu.com", "/api/v4/content/publish")[0]
    assert fake.requests.index(pub) > fake.requests.index(topic_posts[0])
    assert pub.headers["x-xsrftoken"] == XSRF and pub.headers["x-requested-with"] == "fetch"
    assert pub.headers["origin"] == "https://zhuanlan.zhihu.com"
    assert pub.headers["referer"] == "https://zhuanlan.zhihu.com/p/555/edit"
    assert "z_c0=" in pub.headers["cookie"]
    body = fake.json_body(pub)
    data = body["data"]
    assert body["action"] == "article" and TRACE_RE.match(data["publish"]["traceId"])
    assert isinstance(data["extra_info"]["pc_business_params"], str)
    business = json.loads(data["extra_info"]["pc_business_params"])
    assert business["disclaimer_type"] == "ai_creation" and business["table_of_contents_enabled"] is True
    assert business["content"] == content and business["title"] == "测试文章"
    assert data["creationStatement"] == {"disclaimer_type": "ai_creation", "disclaimer_status": "open"}
    assert data["column"] == {"column": ["c_123"]} and data["draft"]["id"] == "555"
    assert data["hybrid"] == {"html": content, "textLength": zm.text_length(content)}
    assert data["title"] == {"title": "测试文章"}
    assert fake.find("GET", "www.zhihu.com", "/api/v4/articles/777")


async def test_article_draft_mode(env, media: Path):
    ctx, events, fake = env
    post = article_post(media, zhihu="    draft: true\n    statement: spoiler\n")
    result = await ZhihuAdapter(ctx).publish(post)
    assert result.status is Status.DRAFT and result.url == "https://zhuanlan.zhihu.com/p/555/edit"
    assert result.item_id == "555" and not result.submit_started
    assert "创作声明" in result.message
    assert not fake.find("POST", "www.zhihu.com", "/api/v4/content/publish")
    assert not any(e["event"] == "submit_started" for e in events)


async def test_article_publish_rejected_returns_edit_url(env, media: Path):
    ctx, _, fake = env
    fake.publish_response = {"code": 2000, "message": "发布失败"}
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.FAILED and result.error_code == ErrorCode.PLATFORM_ERROR.value
    assert result.url == "https://zhuanlan.zhihu.com/p/555/edit" and result.submit_started
    assert "2000: 发布失败" in result.message and not result.retryable


async def test_article_published_but_not_visible_is_submitted(env, media: Path):
    ctx, _, fake = env
    fake.article_visible = False
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.SUBMITTED and result.url == "https://zhuanlan.zhihu.com/p/777"


@pytest.mark.parametrize("response", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)),
    httpx.Response(502, text="bad gateway"),
    httpx.Response(200, json={"message": "?"}),  # 没有 code 也查不到文章
])
async def test_article_publish_unknown_outcome(env, media: Path, response):
    ctx, _, fake = env
    fake.overrides[("POST", "www.zhihu.com", "/api/v4/content/publish")] = response
    fake.article_visible = False
    with pytest.raises(PublishError) as info:
        await ZhihuAdapter(ctx).publish(article_post(media))
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME and ctx.submit_started
    assert "https://zhuanlan.zhihu.com/p/555/edit" in str(info.value) and "草稿已保存" not in str(info.value)
    assert len(fake.find("POST", "www.zhihu.com", "/api/v4/content/publish")) == 1  # 不重试
    has_response = isinstance(response, httpx.Response)  # 超时没有响应，也就没有证据文件
    assert any(e.endswith("zhihu-publish-response.txt") for e in ctx.evidence) is has_response
    assert info.value.url == "https://zhuanlan.zhihu.com/p/555/edit"  # 进 Result.url，skill 能直接给出链接


@pytest.mark.parametrize("body", [{"message": "?"}, {"error": {"message": "参数错误"}}, {"data": {}}])
async def test_unrecognized_publish_response_never_checks_draft_id(env, media: Path, body):
    # 没有 code 也没有文章 id：不能拿草稿 id 去查（作者查自己的草稿可能也是 200）→ 结果未知，而不是「已发布」
    ctx, _, fake = env
    fake.publish_response = body
    assert fake.article_visible
    with pytest.raises(PublishError) as info:
        await ZhihuAdapter(ctx).publish(article_post(media))
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME and info.value.url.endswith("/p/555/edit")
    assert not [r for r in fake.requests if r.url.path.startswith("/api/v4/articles/")]


async def test_publish_without_code_but_with_id_is_verified(env, media: Path):
    ctx, _, fake = env
    fake.publish_response = {"data": {"result": json.dumps({"publish": {"id": "778"}})}}
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.PUBLISHED and result.url == "https://zhuanlan.zhihu.com/p/778"
    assert fake.find("GET", "www.zhihu.com", "/api/v4/articles/778")

    ctx2, _ = make_ctx(ctx.settings)
    fake.article_visible = False
    with pytest.raises(PublishError) as info:
        await ZhihuAdapter(ctx2).publish(article_post(media))
    assert info.value.code is ErrorCode.UNKNOWN_OUTCOME


async def test_error_after_draft_carries_edit_url(env, media: Path):
    ctx, _, fake = env
    fake.overrides[("PATCH", "zhuanlan.zhihu.com", "/api/articles/555/draft")] = \
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("reset", request=r))
    with pytest.raises(PublishError) as info:
        await ZhihuAdapter(ctx).publish(article_post(media))
    assert info.value.retryable and not ctx.submit_started
    assert info.value.url == "https://zhuanlan.zhihu.com/p/555/edit" and "草稿已保存" in str(info.value)


async def test_publish_risk_control_40352(env, media: Path, settings):
    ctx, _, fake = env
    fake.overrides[("POST", "www.zhihu.com", "/api/v4/content/publish")] = httpx.Response(
        403, json={"error": {"code": 40352, "message": "系统监测到您的网络环境存在异常",
                             "redirect": RISK_REDIRECT, "need_login": True}})
    with pytest.raises(NeedsAction) as info:
        await ZhihuAdapter(ctx).publish(article_post(media))
    assert info.value.code is ErrorCode.RISK_CONTROL
    assert "https://www.zhihu.com/account/unhuman?type=S6E3V1" in str(info.value)
    assert "mpub login zhihu --account main" in str(info.value)
    verify = settings.accounts_dir / "zhihu" / "main.verify.txt"
    assert verify.read_text(encoding="utf-8") == "https://www.zhihu.com" + RISK_REDIRECT


async def test_risk_before_publish_keeps_draft_url(env, media: Path):
    ctx, _, fake = env
    fake.overrides[("PATCH", "zhuanlan.zhihu.com", "/api/articles/555/draft")] = httpx.Response(
        403, json={"error": {"code": "40352", "redirect": RISK_REDIRECT}})  # code 是字符串也认
    with pytest.raises(NeedsAction) as info:
        await ZhihuAdapter(ctx).publish(article_post(media))
    assert info.value.code is ErrorCode.RISK_CONTROL
    assert "草稿已保存：https://zhuanlan.zhihu.com/p/555/edit" in str(info.value)
    assert info.value.url.endswith("session=abc") and not ctx.submit_started
    assert not fake.find("POST", "www.zhihu.com", "/api/v4/content/publish")


async def test_successful_publish_clears_stale_verify_file(env, media: Path, settings):
    ctx, _, fake = env
    verify = settings.accounts_dir / "zhihu" / "main.verify.txt"
    verify.write_text("https://www.zhihu.com" + RISK_REDIRECT, encoding="utf-8")
    fake.overrides[("POST", "www.zhihu.com", "/api/v4/content/publish")] = httpx.Response(
        200, json={"code": 2000, "message": "发布失败"})
    await ZhihuAdapter(ctx).publish(article_post(media))
    assert verify.exists()  # 失败不删
    fake.overrides.clear()
    ctx2, _ = make_ctx(settings)
    assert (await ZhihuAdapter(ctx2).publish(article_post(media))).status is Status.PUBLISHED
    assert not verify.exists()  # 写接口能用 = 风控已解除


async def test_topic_risk_control_does_not_block_publish(env, media: Path):
    ctx, _, fake = env
    fake.overrides[("GET", "zhuanlan.zhihu.com", "/api/autocomplete/topics")] = httpx.Response(
        403, json={"error": {"code": 40352, "redirect": RISK_REDIRECT}})
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.PUBLISHED and "话题被知乎风控拦截" in result.message
    assert len(fake.find("GET", "zhuanlan.zhihu.com", "/api/autocomplete/topics")) == 1  # 第一次被拦就不再试

    ctx2, _ = make_ctx(ctx.settings)
    fake.overrides[("GET", "zhuanlan.zhihu.com", "/api/autocomplete/topics")] = httpx.Response(
        401, json={"error": {"code": 100, "message": "ERR_TICKET_NOT_EXIST"}})
    with pytest.raises(LoginRequired):  # 登录失效照样中止（发布接口必然也失败）
        await ZhihuAdapter(ctx2).publish(article_post(media))
    assert not ctx2.submit_started


async def test_article_publish_http_rejection_keeps_edit_url_and_evidence(env, media: Path):
    ctx, _, fake = env
    fake.overrides[("POST", "www.zhihu.com", "/api/v4/content/publish")] = httpx.Response(
        400, json={"error": {"code": 4001, "message": "内容含有违规信息"}})
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.FAILED and result.error_code == ErrorCode.PLATFORM_ERROR.value
    assert result.url == "https://zhuanlan.zhihu.com/p/555/edit" and result.item_id == "555"
    assert "内容含有违规信息" in result.message and "手动发布" in result.message
    assert result.submit_started and not result.retryable
    evidence = [Path(e) for e in result.evidence if e.endswith("zhihu-publish-response.txt")]
    assert evidence and "HTTP 400" in evidence[0].read_text(encoding="utf-8")


async def test_topic_and_cover_failures_do_not_block(env, media: Path):
    ctx, _, fake = env
    fake.overrides[("POST", "zhuanlan.zhihu.com", "/api/articles/555/topics")] = httpx.Response(
        400, json={"error": {"message": "话题不可用"}})
    calls = {"n": 0}

    def patch(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "titleImage" in fake.json_body(request):
            return httpx.Response(400, json={"error": {"message": "封面太小"}})
        return httpx.Response(200, json={})

    fake.overrides[("PATCH", "zhuanlan.zhihu.com", "/api/articles/555/draft")] = patch
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.PUBLISHED and calls["n"] == 2
    assert "话题「人工智能」没加上" in result.message and "封面没设置上" in result.message


async def test_topic_without_exact_match_warns_or_skips(env, media: Path):
    ctx, _, fake = env
    names = {"人工智能": ["人工智能技术", "机器学习"], "不存在": ["露营"]}
    fake.overrides[("GET", "zhuanlan.zhihu.com", "/api/autocomplete/topics")] = lambda r: httpx.Response(
        200, json=[{"id": str(i), "name": n, "type": "topic"} for i, n in enumerate(names[r.url.params["token"]])])
    result = await ZhihuAdapter(ctx).publish(article_post(media))
    assert result.status is Status.PUBLISHED
    added = [fake.json_body(r)["name"] for r in fake.find("POST", "zhuanlan.zhihu.com", "/api/articles/555/topics")]
    assert added == ["人工智能技术"]  # 包含关系：采用，但要说出来
    assert "话题「人工智能」没有同名的，已改用「人工智能技术」" in result.message
    assert "没找到话题「不存在」（最接近的是「露营」，没有添加）" in result.message  # 不相干：不挂


async def test_publish_login_expired(env, media: Path):
    ctx, _, fake = env
    fake.overrides[("GET", "www.zhihu.com", "/api/v4/me")] = httpx.Response(
        401, json={"error": {"code": 100, "message": "ERR_TICKET_NOT_EXIST"}})
    with pytest.raises(LoginRequired, match="mpub login zhihu --account main"):
        await ZhihuAdapter(ctx).publish(article_post(media))
    assert not fake.find("POST", "api.zhihu.com", "/images") and not ctx.submit_started


async def test_note_as_article_appends_images(env, media: Path):
    ctx, _, fake = env
    post = note_post(media, body="第一行\n**第二行**", options={"as": "article"}, tags=["徒步"])
    result = await ZhihuAdapter(ctx).publish(post)
    assert result.status is Status.PUBLISHED
    content = fake.json_body(fake.find("PATCH", "zhuanlan.zhihu.com", "/api/articles/555/draft")[0])["content"]
    assert content.startswith("<p>第一行</p><p><b>第二行</b></p><p><img ") and content.count("<img ") == 2
    assert json.loads(fake.json_body(fake.find("POST", "www.zhihu.com", "/api/v4/content/publish")[0])
                      ["data"]["extra_info"]["pc_business_params"])["disclaimer_type"] == "none"


# ------------------------------------------------------------------ 想法

async def test_pin_flow(env, media: Path):
    ctx, events, fake = env
    fake.publish_response = {"code": 0, "message": "success", "data": {"result": json.dumps({"id": "999"})}}
    result = await ZhihuAdapter(ctx).publish(note_post(media))
    assert result.status is Status.PUBLISHED and result.url == "https://www.zhihu.com/pin/999"
    assert result.item_id == "999" and result.submit_started

    assert fake.json_body(fake.find("POST", "www.zhihu.com", "/api/v4/content/drafts")[0]) == {"action": "pin"}
    regs = fake.find("POST", "api.zhihu.com", "/images")
    assert [fake.json_body(r)["source"] for r in regs] == ["pin", "pin"]
    assert regs[0].headers["origin"] == "https://www.zhihu.com"
    pub = fake.find("POST", "www.zhihu.com", "/api/v4/content/publish")[0]
    assert pub.headers["origin"] == "https://www.zhihu.com" and pub.headers["referer"] == "https://www.zhihu.com/"
    body = fake.json_body(pub)
    data = body["data"]
    assert body["action"] == "pin" and TRACE_RE.match(data["publish"]["traceId"])
    assert data["draft"] == {"disabled": 1, "id": "888"} and data["title"] == {"title": "周末徒步"}
    assert data["extra_info"] == {"view_permission": "all", "publisher": "pc"}
    assert data["commentsPermission"] == {"comment_permission": "all"}
    html = data["hybrid"]["html"]
    assert html.startswith("<p>正文第一行</p><p>第二行 &lt;b&gt;</p><p>#徒步# #周末#</p><img src=\"https://pic1.zhimg.com/")
    assert html.count("<img ") == 2 and data["hybrid"]["textLength"] == zm.text_length(html)
    medias = data["media"]["medias"]
    md5 = hashlib.md5((media / "images" / "1.jpg").read_bytes()).hexdigest()
    assert medias[0] == {"image": {"width": 1080, "height": 1440, "url": f"https://pic1.zhimg.com/v2-{md5[:8]}",
                                   "originalUrl": f"https://picx.zhimg.com/v2-{md5[:8]}", "watermark": "watermark",
                                   "watermarkUrl": f"https://pic2.zhimg.com/v2-{md5[:8]}_wm"}}


async def test_pin_rejected_and_missing_id(env, media: Path):
    ctx, _, fake = env
    fake.publish_response = {"code": 10003, "message": "内容含有违规信息"}
    with pytest.raises(PublishError, match="10003: 内容含有违规信息") as info:
        await ZhihuAdapter(ctx).publish(note_post(media))
    assert info.value.code is ErrorCode.PLATFORM_ERROR

    ctx2, _ = make_ctx(ctx.settings)
    fake.publish_response = {"code": 0, "message": "success", "data": {}}
    result = await ZhihuAdapter(ctx2).publish(note_post(media))
    assert result.status is Status.SUBMITTED and result.url is None


# ------------------------------------------------------------------ 登录 / 检查

async def test_check(env, settings):
    ctx, _, fake = env
    ctx.action = "check"
    result = await ZhihuAdapter(ctx).check()
    assert result.status is Status.OK and "测试用户" in result.message and "天后过期" in result.message
    me = fake.find("GET", "www.zhihu.com", "/api/v4/me")[0]
    assert me.url.params["include"] == "is_bind_phone" and me.headers["x-xsrftoken"] == XSRF

    fake.overrides[("GET", "www.zhihu.com", "/api/v4/me")] = httpx.Response(
        200, json={"id": "u1", "name": "无手机", "is_bind_phone": False})
    assert "没有绑定手机号" in (await ZhihuAdapter(ctx).check()).message

    fake.overrides[("GET", "www.zhihu.com", "/api/v4/me")] = httpx.Response(
        401, json={"error": {"code": 100, "message": "ERR_TICKET_NOT_EXIST"}})
    with pytest.raises(LoginRequired, match="mpub login zhihu --account main"):
        await ZhihuAdapter(ctx).check()


async def test_check_without_login(settings, fake):
    ctx, _ = make_ctx(settings, action="check", account="nobody")
    with pytest.raises(LoginRequired, match="还没有登录"):
        await ZhihuAdapter(ctx).check()
    assert not fake.requests


async def test_login_already_logged_in_skips_browser(env, monkeypatch):
    ctx, _, _ = env
    ctx.action = "login"

    async def boom(*a, **k):
        raise AssertionError("不应打开浏览器")

    monkeypatch.setattr(ZhihuAdapter, "_browser_login", boom)
    result = await ZhihuAdapter(ctx).login()
    assert result.status is Status.OK and "已登录：测试用户" in result.message


async def test_login_saves_state_and_uses_pending_verify(env, settings, monkeypatch):
    ctx, _, fake = env
    ctx.action = "login"
    verify = settings.accounts_dir / "zhihu" / "main.verify.txt"
    verify.write_text("https://www.zhihu.com" + RISK_REDIRECT, encoding="utf-8")
    calls: list[tuple[Any, str]] = []
    new_state = storage_state()
    new_state["cookies"][0]["value"] = "new-z_c0"

    async def fake_browser(self, state, verify_url):
        calls.append((state, verify_url))
        return new_state

    monkeypatch.setattr(ZhihuAdapter, "_browser_login", fake_browser)
    result = await ZhihuAdapter(ctx).login()
    assert result.status is Status.OK and "登录成功：测试用户" in result.message
    assert calls[0][1].endswith("session=abc") and calls[0][0]["cookies"][0]["value"] == Z_C0  # 带旧登录态去验证
    saved = json.loads(settings.account_file("zhihu", "main").read_text(encoding="utf-8"))
    assert saved["cookies"][0]["value"] == "new-z_c0" and not verify.exists()


async def test_login_fresh_context_when_cookie_invalid(env, settings, monkeypatch):
    ctx, _, fake = env
    ctx.action = "login"
    fake.overrides[("GET", "www.zhihu.com", "/api/v4/me")] = httpx.Response(401, json={})
    calls: list[tuple[Any, str]] = []

    async def fake_browser(self, state, verify_url):
        calls.append((state, verify_url))
        fake.overrides.clear()
        return storage_state()

    monkeypatch.setattr(ZhihuAdapter, "_browser_login", fake_browser)
    assert (await ZhihuAdapter(ctx).login()).status is Status.OK
    assert calls == [(None, "")]


async def test_login_with_pending_verify_checks_api_first(env, settings, monkeypatch):
    ctx, _, fake = env
    ctx.action = "login"
    verify = settings.accounts_dir / "zhihu" / "main.verify.txt"
    verify.write_text("https://www.zhihu.com/account/unhuman?session=old", encoding="utf-8")
    calls: list[tuple[Any, str]] = []

    async def fake_browser(self, state, verify_url):
        calls.append((state, verify_url))
        fake.overrides.clear()
        return storage_state()

    monkeypatch.setattr(ZhihuAdapter, "_browser_login", fake_browser)
    # me() 本身被风控：用最新的验证页地址（旧地址的 session 可能过期）
    fake.overrides[("GET", "www.zhihu.com", "/api/v4/me")] = httpx.Response(
        403, json={"error": {"code": 40352, "redirect": RISK_REDIRECT}})
    assert (await ZhihuAdapter(ctx).login()).status is Status.OK
    assert calls[-1][1] == "https://www.zhihu.com" + RISK_REDIRECT and calls[-1][0] is not None
    assert not verify.exists()

    # 登录已失效：验证页没意义，直接重新扫码（干净的上下文）
    verify.write_text("https://www.zhihu.com/account/unhuman?session=old", encoding="utf-8")
    fake.overrides[("GET", "www.zhihu.com", "/api/v4/me")] = httpx.Response(401, json={})
    assert (await ZhihuAdapter(ctx).login()).status is Status.OK
    assert calls[-1] == (None, "") and not verify.exists()


def test_login_wait_follows_job_timeout(settings):
    ctx, _ = make_ctx(settings, action="login")
    adapter = ZhihuAdapter(ctx)
    assert adapter._login_wait() == settings.timeout_for("login") - 30 - zh.LOGIN_RESERVE  # 同 runner 的 worker 超时
    ctx.timeout = 600
    assert adapter._login_wait() == 600 - zh.LOGIN_RESERVE
    ctx.timeout = 50
    assert adapter._login_wait() == 60


class _Resp:
    def __init__(self, url: str, data: Any):
        self.url = url
        self._data = data

    async def json(self) -> Any:
        return self._data


async def test_qr_capture_from_signin_api(settings):
    ctx, events = make_ctx(settings, action="login")
    qr = zh._QrCapture(ctx, settings.qrcode_dir / "zhihu-main.png")
    await qr.on_response(_Resp("https://www.zhihu.com/api/v3/account/api/login/qrcode/tok/scan_info", {"link": "x"}))
    assert not qr.done  # 轮询扫码状态的接口不是二维码
    await qr.on_response(_Resp("https://www.zhihu.com/api/v3/account/api/login/qrcode",
                               {"token": "tok", "link": "https://www.zhihu.com/account/scan/login/tok?/api/login/qrcode"}))
    assert qr.done and qr.path.is_file() and qr.path.read_bytes()[:4] == b"\x89PNG"
    assert events[-1]["event"] == "qrcode" and events[-1]["path"] == str(qr.path)
    qr.cleanup()
    assert not qr.path.exists()


class _FakeContext:
    def __init__(self, cookies: list[dict[str, str]]):
        self._cookies = cookies

    async def cookies(self, url: str) -> list[dict[str, str]]:
        return self._cookies


class _FakePage:
    def __init__(self, url: str, closed: bool = False):
        self.url = url
        self.closed = closed

    def is_closed(self) -> bool:
        return self.closed


async def test_browser_logged_in_rules(settings):
    adapter = ZhihuAdapter(make_ctx(settings, action="login")[0])
    z = [{"name": "z_c0", "value": "Z"}]
    assert not await adapter._browser_logged_in(_FakeContext([]), _FakePage(za.SIGNIN_URL), None)
    assert await adapter._browser_logged_in(_FakeContext(z), _FakePage(za.SIGNIN_URL), None)  # 新拿到 z_c0
    unhuman = "https://www.zhihu.com" + RISK_REDIRECT
    assert not await adapter._browser_logged_in(_FakeContext(z), _FakePage(unhuman), "Z")
    assert await adapter._browser_logged_in(_FakeContext(z), _FakePage("https://www.zhihu.com/"), "Z")
    with pytest.raises(PublishError, match="关闭"):
        await adapter._browser_logged_in(_FakeContext(z), _FakePage("x", closed=True), None)


HEADLESS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "HeadlessChrome/150.0.0.0 Safari/537.36")


class _LoginLocator:
    first = property(lambda self: self)

    async def count(self) -> int:
        return 0  # 找不到二维码元素 → 整页截图


class _LoginPage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.gotos: list[str] = []
        self.handlers: dict[str, Any] = {}

    def on(self, name: str, fn: Any) -> None:
        self.handlers[name] = fn

    async def goto(self, url: str, **kw: Any) -> None:
        self.gotos.append(url)
        self.url = url

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> _LoginLocator:
        return _LoginLocator()

    async def evaluate(self, script: str) -> str:
        assert "navigator.userAgent" in script
        return HEADLESS_UA

    async def screenshot(self, path: str, **kw: Any) -> None:
        Path(path).write_bytes(b"\x89PNG fake")


class _LoginContext:
    """假浏览器上下文：第 3 次查 cookie 时出现 z_c0（模拟用户扫码）。"""

    def __init__(self) -> None:
        self.page = _LoginPage()
        self.polls = 0

    async def new_page(self) -> _LoginPage:
        return self.page

    async def cookies(self, url: str) -> list[dict[str, str]]:
        self.polls += 1
        return [{"name": "z_c0", "value": "NEW"}] if self.polls >= 3 else []

    async def storage_state(self) -> dict[str, Any]:
        state = storage_state()
        state["cookies"][0]["value"] = "NEW"
        return state


async def test_browser_login_flow_with_fake_browser(settings, fake, monkeypatch):
    import contextlib

    import multipub.browser as browser_mod

    browser = _LoginContext()
    opened: list[tuple[bool, Any]] = []

    @contextlib.asynccontextmanager
    async def fake_open_context(_settings, *, headless, storage_state=None, **kw):
        opened.append((headless, storage_state))
        yield browser

    real_sleep = zh.asyncio.sleep

    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(browser_mod, "open_context", fake_open_context)
    monkeypatch.setattr(zh.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(zh, "QR_FALLBACK_AFTER", -1)
    ctx, events = make_ctx(settings, action="login")
    result = await ZhihuAdapter(ctx).login()  # 还没有登录文件：开干净的浏览器

    assert result.status is Status.OK and "登录成功：测试用户" in result.message
    assert opened == [(True, None)] and browser.polls == 3
    assert browser.page.gotos == [za.SIGNIN_URL, za.WWW_HOME]  # 登录后再开一次首页补 cookie
    assert "response" in browser.page.handlers  # 拦截二维码接口
    qr = [e for e in events if e["event"] == "qrcode"]
    assert qr and qr[0]["path"] == str(settings.qrcode_dir / "zhihu-main.png") and "截图" in qr[0]["note"]
    assert not (settings.qrcode_dir / "zhihu-main.png").exists()  # 登录完删掉二维码
    saved = json.loads(settings.account_file("zhihu", "main").read_text(encoding="utf-8"))
    assert saved["cookies"][0]["value"] == "NEW"
    # 记下登录浏览器的 UA（去掉 Headless），之后的接口请求沿用
    ua = (settings.accounts_dir / "zhihu" / "main.ua.txt").read_text(encoding="utf-8")
    assert ua == HEADLESS_UA.replace("HeadlessChrome/", "Chrome/")
    assert fake.find("GET", "www.zhihu.com", "/api/v4/me")[-1].headers["user-agent"] == ua



def _patch_browser(monkeypatch, browser: Any, opened: list | None = None) -> None:
    import contextlib

    import multipub.browser as browser_mod

    @contextlib.asynccontextmanager
    async def fake_open_context(_settings, *, headless, storage_state=None, **kw):
        if opened is not None:
            opened.append((headless, storage_state))
        yield browser

    real_sleep = zh.asyncio.sleep

    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    async def no_snapshot(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(browser_mod, "open_context", fake_open_context)
    monkeypatch.setattr(browser_mod, "snapshot", no_snapshot)
    monkeypatch.setattr(zh.asyncio, "sleep", fast_sleep)


async def test_browser_login_saves_state_before_homepage_refresh(settings, fake, monkeypatch):
    browser = _LoginContext()
    account_file = settings.account_file("zhihu", "main")
    saved_at_refresh: list[bool] = []
    goto = browser.page.goto

    async def slow_home(url: str, **kw: Any) -> None:
        if url == za.WWW_HOME:
            saved_at_refresh.append(account_file.is_file())
            raise TimeoutError("首页太慢")
        await goto(url, **kw)

    browser.page.goto = slow_home
    _patch_browser(monkeypatch, browser)
    ctx, _ = make_ctx(settings, action="login")
    assert (await ZhihuAdapter(ctx).login()).status is Status.OK
    assert saved_at_refresh == [True]  # 扫完码先存一份，首页刷新失败 / 任务超时也不白扫


class _VerifyContext(_LoginContext):
    """带旧登录态打开验证页，页面一直停在验证页（需要人操作）。"""

    async def cookies(self, url: str) -> list[dict[str, str]]:
        self.polls += 1
        return [{"name": "z_c0", "value": Z_C0}]


async def test_headless_verify_fails_fast_with_risk_control(env, settings, monkeypatch):
    ctx, events, fake = env
    ctx.action = "login"
    verify = settings.accounts_dir / "zhihu" / "main.verify.txt"
    verify.write_text("https://www.zhihu.com" + RISK_REDIRECT, encoding="utf-8")
    browser = _VerifyContext()
    opened: list[tuple[bool, Any]] = []
    _patch_browser(monkeypatch, browser, opened)
    monkeypatch.setattr(zh, "VERIFY_HEADLESS_WAIT", 0)
    with pytest.raises(NeedsAction) as info:
        await ZhihuAdapter(ctx).login()
    assert info.value.code is ErrorCode.RISK_CONTROL and "去掉 --headless" in str(info.value)
    assert info.value.url == "https://www.zhihu.com" + RISK_REDIRECT
    assert opened[0][1] is not None and browser.page.gotos == ["https://www.zhihu.com" + RISK_REDIRECT]
    assert browser.polls < 5 and verify.exists()  # 没有傻等到超时；验证页地址留着下次用
    assert "response" not in browser.page.handlers  # 过验证不需要二维码


def test_normalize_user_agent():
    assert za.normalize_user_agent(HEADLESS_UA).count("HeadlessChrome") == 0
    assert za.normalize_user_agent("curl/8.0") == "" and za.normalize_user_agent(None) == ""


async def test_publish_through_worker(settings, fake, media: Path):
    from multipub.worker import run_job

    write_state(settings.account_file("zhihu", "main"))
    fake.overrides[("POST", "www.zhihu.com", "/api/v4/content/publish")] = httpx.Response(
        403, json={"error": {"code": 40352, "redirect": RISK_REDIRECT}})
    events: list[dict] = []
    job = {"action": "publish", "platform": "zhihu", "account": "main", "home": str(settings.home),
           "headless": True, "run_dir": str(settings.runs_dir / "w"), "post": note_post(media).to_dict()}
    result = await run_job(job, emit=events.append)
    assert result.status is Status.NEEDS_ACTION and result.error_code == ErrorCode.RISK_CONTROL.value
    assert result.submit_started and not result.retryable
    assert any(e["event"] == "submit_started" for e in events)


# ------------------------------------------------------------------ validate

def issues_of(post: PlatformPost) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"error": [], "warning": []}
    for i in ZhihuAdapter.validate(post):
        out[i.level].append(f"{i.field}: {i.message}")
    return out


def test_validate_article_rules(media: Path):
    post = article_post(media)
    assert issues_of(post)["error"] == []

    long_title = article_post(media)
    long_title.title = "长" * 101
    assert any("最多 100 字" in e for e in issues_of(long_title)["error"])

    bad = article_post(media, zhihu="    statement: maybe\n    column: ''\n    toc: perhaps\n    foo: 1\n    tags_in_text: true\n")
    res = issues_of(bad)
    assert any("创作声明只能是" in e for e in res["error"])
    assert any("专栏 id 不能为空" in e for e in res["error"])
    assert any("platforms.zhihu.toc" in e for e in res["error"])
    assert any("platforms.zhihu.foo: 未知参数" in w for w in res["warning"])
    assert any("platforms.zhihu.tags_in_text: 文章不使用该参数" in w for w in res["warning"])

    (media / "content" / "article.md").write_text("![x](nope.png)\n\n![y](data:image/png;base64,AAA)", encoding="utf-8")
    missing = load_manifest(media / "post.yaml").for_platform("zhihu")
    errs = issues_of(missing)["error"]
    assert any("找不到正文里引用的图片: nope.png" in e for e in errs) and any("data:" in e for e in errs)

    raw = article_post(media)
    raw.body += '\n\n<img src="local.png">\n\n<img src="https://pic1.zhimg.com/v2-x.jpg">\n'
    raw_warns = [w for w in issues_of(raw)["warning"] if "HTML <img>" in w]
    assert len(raw_warns) == 1 and "local.png 等 1 张" in raw_warns[0]

    sched = article_post(media)
    sched.schedule = datetime.now() + timedelta(days=1)
    assert any("定时" in e for e in issues_of(sched)["error"])

    conflict = article_post(media, extra_top="ai_generated: true\n", zhihu="    statement: fiction\n    draft: true\n")
    warns = issues_of(conflict)["warning"]
    assert any("以 statement 为准" in w for w in warns) and any("只存草稿时" in w for w in warns)


    gone = article_post(media)
    gone.images = [media / "images" / "nope.jpg"]
    assert any(e.startswith("images: 文件不存在") for e in issues_of(gone)["error"])  # common_issues 不查文章的图


def test_validate_note_and_kind_rules(media: Path):
    assert issues_of(note_post(media))["error"] == []
    many = note_post(media, images=[media / "images" / "1.jpg"] * 10, options={"draft": True})
    errs = issues_of(many)["error"]
    assert any("最多 9 张图" in e for e in errs) and any("想法没有草稿箱" in e for e in errs)

    res = issues_of(note_post(media, options={}, ai_generated=True, cover=media / "cover.jpg",
                              images=[media / "a.gif"], body="字" * 1001))
    assert any("tags 已忽略" in w for w in res["warning"]) and any("创作声明" in w for w in res["warning"])
    assert any("封面" in w for w in res["warning"]) and any("动图" in w for w in res["warning"])
    assert any("1001 字" in w for w in res["warning"])

    assert any("as" in e for e in issues_of(note_post(media, options={"as": "video"}))["error"])
    article = PlatformPost(platform="zhihu", account="a", kind=Kind.ARTICLE, title="t", body="b", tags=["x"],
                           options={"as": "pin"})
    assert any("长文只能发成知乎文章" in e for e in issues_of(article)["error"])
    video = PlatformPost(platform="zhihu", account="a", kind=Kind.VIDEO, title="t")
    assert issues_of(video)["error"] == ["kind: 知乎暂不支持视频"]
    assert Kind.VIDEO not in ZhihuAdapter.kinds

    na = note_post(media, title="t", options={"as": "article"}, tags=[])
    assert any("没有设置话题" in w for w in issues_of(na)["warning"])
    tif = note_post(media, images=[media / "x.tiff"])
    assert any("只支持" in e for e in issues_of(tif)["error"])


def test_pure_helpers(tmp_path: Path):
    p = PlatformPost(platform="zhihu", account="a", kind=Kind.NOTE, body="正文 #a#", tags=["a", "b"],
                     options={"tags_in_text": "yes"})
    assert zh.pin_text(p) == "正文 #a#\n#b#"
    assert zh.column_id("https://www.zhihu.com/column/c_42/") == "c_42" and zh.column_id(" c_1 ") == "c_1"
    assert zh.statement_type(PlatformPost(platform="zhihu", account="a", kind=Kind.ARTICLE, ai_generated=True)) \
        == "ai_creation"
    assert zh.statement_type(PlatformPost(platform="zhihu", account="a", kind=Kind.ARTICLE, ai_generated=True,
                                          options={"statement": "none"})) == "none"
    d = tmp_path / "中文 目录"
    (d / "图 片").mkdir(parents=True)
    (d / "图 片" / "一.png").write_bytes(b"x")
    found = zh.resolve_local_image("%E5%9B%BE%20%E7%89%87/%E4%B8%80.png", [tmp_path, d])
    assert found == (d / "图 片" / "一.png").resolve()
    assert zh.resolve_local_image((d / "图 片" / "一.png").as_uri(), []) == found
    assert zh.resolve_local_image("missing.png", [d]) is None


def test_adapter_module_import_is_cheap():
    code = ("import sys, multipub.platforms.zhihu as m; m.ZhihuAdapter.validate; m.ZhihuAdapter.kinds; "
            "bad=[x for x in ('httpx','patchright','segno','mistune','PIL') if x in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_registry_and_doctor(settings):
    from multipub.platforms import load_adapter

    assert load_adapter("zhihu") is ZhihuAdapter
    items = ZhihuAdapter.doctor(settings)
    assert items and all(i["ok"] for i in items)
