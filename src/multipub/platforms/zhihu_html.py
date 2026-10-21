"""Markdown → 知乎 HTML（文章草稿 content 与 content/publish 的 hybrid.html 都用这种 HTML）。

规则参考 zhihu_obsidian（0BSD）的 custom_render 与几个开源客户端的抓包结果，这里是独立实现：

- 开头的一级标题丢弃（文章标题是单独字段）；其余标题按正文里最浅的一级对齐：
  最浅一级 → <h2>，次一级 → <h3>，更深 → <p><b>…</b></p>（知乎只有「标题」「小标题」两级）
- 段落里的单个换行拆成多个 <p>（知乎编辑器一行一个段落）；粗体 / 链接等标签内部的换行写成 <br/>，
  避免拆段后标签交错（<p><b>…</p><p>…</b></p>）
- 粗体 <b>、斜体 <i>、删除线 <del>、行内代码 <code>、链接 <a href>
- 代码块 <pre lang="python">…</pre>
- 表格 <table data-draft-node="block" data-draft-type="table" …><tbody>，表头行也放进 tbody（保留 <th>）
- 公式 $…$ / $$…$$ → 知乎公式图片 <img eeimg="1|2" src="//www.zhihu.com/equation?tex=…">
- 脚注 [^1] → 知乎参考文献 <sup data-draft-type="reference" …>[1]</sup>，文末脚注列表去掉
- 图片：调用方先上传到知乎图床，传入 {Markdown 里的原始地址: ZhihuImage}
- 原始 HTML 原样保留，HTML 注释去掉

只依赖 mistune（纯 Python），不联网。
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable

MD_PLUGINS = ["table", "strikethrough", "footnotes", "math", "url"]
TABLE_ATTRS = ('data-draft-node="block" data-draft-type="table" data-size="normal" '
               'data-row-style="normal"')
EQUATION_URL = "//www.zhihu.com/equation?tex="

_BREAK = "\ue000"  # 段内换行占位符（私有区字符，正文里不会出现）
_INNER_BREAK = "\ue001"  # 粗体 / 链接等行内标签里面的换行：不能拆段
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_FRONT_MATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_URL_RE = re.compile(r"https?://[^\s<>()\]]+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^\s)]+)[^)]*\)")
_UNSAFE_SCHEMES = ("javascript:", "vbscript:", "data:")
_RAW_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']?([^"'\s>]+)""", re.I)


@dataclass
class ZhihuImage:
    """已上传到知乎图床的图片（GET api.zhihu.com/images/<id> 的结果 + 本地尺寸）。"""

    src: str
    original_src: str
    watermark: str = "watermark"
    watermark_src: str = ""
    width: int = 0
    height: int = 0
    ext: str = "jpg"
    image_id: str = ""
    md5: str = ""


# ------------------------------------------------------------------ 解析

def strip_front_matter(markdown: str) -> str:
    """去掉开头的 YAML front matter（Obsidian 等编辑器常用），否则会被渲染成分割线 + 标题。"""
    m = _FRONT_MATTER_RE.match(markdown)
    return markdown[m.end():] if m else markdown


def _parse(markdown: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import mistune

    md = mistune.create_markdown(renderer=None, plugins=MD_PLUGINS)
    tokens, state = md.parse(strip_front_matter(markdown))
    return tokens, state.env


def _walk(tokens: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for tok in tokens:
        yield tok
        yield from _walk(tok.get("children") or [])


def collect_image_urls(markdown: str) -> list[str]:
    """Markdown 里引用的图片地址（按出现顺序去重，原样返回）。"""
    tokens, _ = _parse(markdown)
    out: list[str] = []
    for tok in _walk(tokens):
        if tok.get("type") == "image":
            url = str((tok.get("attrs") or {}).get("url") or "")
            if url and url not in out:
                out.append(url)
    return out


def raw_html_images(markdown: str) -> list[str]:
    """正文里原始 HTML <img> 标签的地址（不会被上传，只用于 validate 警告）。代码块里的不算。"""
    tokens, _ = _parse(markdown)
    out: list[str] = []
    for tok in _walk(tokens):
        if tok.get("type") in ("block_html", "inline_html"):
            for url in _RAW_IMG_RE.findall(str(tok.get("raw") or "")):
                if url not in out:
                    out.append(url)
    return out


def is_remote(url: str) -> bool:
    return url.lower().startswith(("http://", "https://", "//"))


def is_zhimg(url: str) -> bool:
    """已经在知乎图床上的图片，不需要再上传。"""
    host = urllib.parse.urlsplit(url if not url.startswith("//") else "https:" + url).hostname or ""
    return host == "zhimg.com" or host.endswith(".zhimg.com")


# ------------------------------------------------------------------ 小工具

def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _text(raw: str) -> str:
    # 与 mistune 的 safe_entity 一致：先还原已有实体再转义，避免 &amp;amp;
    return html.escape(html.unescape(raw), quote=False)


def _safe_url(url: str) -> str:
    if url.strip().lower().startswith(_UNSAFE_SCHEMES):
        return "#"
    safe = ":/?#@!$&()*+,;=%~"
    return urllib.parse.quote(html.unescape(url), safe=safe)


def with_ext(url: str, ext: str) -> str:
    """知乎返回的 original_src 通常没有扩展名（https://picx.zhimg.com/v2-<md5>），补上。"""
    if not url or not ext:
        return url
    parts = urllib.parse.urlsplit(url)
    last = parts.path.rsplit("/", 1)[-1]
    if not last or "." in last:
        return url
    return urllib.parse.urlunsplit(parts._replace(path=f"{parts.path}.{ext}"))


def equation_img(tex: str, display: bool) -> str:
    src = EQUATION_URL + urllib.parse.quote(tex.strip(), safe="-_.!~*'()")
    alt = " ".join(tex.split())
    img = f'<img eeimg="{2 if display else 1}" src="{_attr(src)}" alt="{_attr(alt)}"/>'
    return f"<p>{img}</p>" if display else img


def article_img(img: ZhihuImage, caption: str = "") -> str:
    """文章正文里的图片（属性与知乎编辑器一致；src 用 original_src + 扩展名，同 zhihu_obsidian）。"""
    src = with_ext(img.original_src or img.src, img.ext)
    wm_src = with_ext(img.watermark_src, img.ext) if img.watermark_src else ""
    return (f'<img src="{_attr(src)}" data-caption="{_attr(caption)}" data-size="normal" '
            f'data-rawwidth="{int(img.width)}" data-rawheight="{int(img.height)}" '
            f'data-watermark="{_attr(img.watermark)}" data-original-src="{_attr(src)}" '
            f'data-watermark-src="{_attr(wm_src)}" data-private-watermark-src=""/>')


def pin_img(img: ZhihuImage) -> str:
    """想法正文里的图片（与 KrisTHL181/zhihu-cli 发想法时一致，直接用接口返回的地址）。"""
    return (f'<img src="{_attr(img.src)}" data-caption="" data-size="normal" '
            f'data-rawwidth="{int(img.width)}" data-rawheight="{int(img.height)}" '
            f'data-watermark="{_attr(img.watermark)}" data-original-src="{_attr(img.original_src or img.src)}" '
            f'data-watermark-src="{_attr(img.watermark_src)}" data-private-watermark-src=""/>')


def _footnote(raw: str) -> tuple[str, str]:
    """脚注定义 → (说明文字, 链接)。支持 "说明 https://..." 与 "[说明](https://...)"。"""
    text = " ".join(raw.split())
    m = _MD_LINK_RE.search(text)
    if m:
        url = m.group(2)
        text = (text[:m.start()] + m.group(1) + text[m.end():]).strip()
        return text or url, url
    m = _URL_RE.search(text)
    if m:
        url = m.group(0)
        text = (text[:m.start()] + text[m.end():]).strip(" ：:，,")
        return text or url, url
    return text, ""


# ------------------------------------------------------------------ 渲染

class _Renderer:
    def __init__(self, images: dict[str, ZhihuImage], footnotes: dict[str, str], heading_base: int):
        self.images = images
        self.footnotes = footnotes
        self.heading_base = heading_base

    # ---- 块级
    def blocks(self, tokens: list[dict[str, Any]]) -> str:
        return "".join(self.block(t) for t in tokens)

    def block(self, tok: dict[str, Any]) -> str:
        kind = tok.get("type")
        children = tok.get("children") or []
        attrs = tok.get("attrs") or {}
        if kind == "paragraph":
            return self.paragraph(children)
        if kind == "heading":
            return self.heading(children, int(attrs.get("level", 1)))
        if kind == "block_text":  # 紧凑列表项里的文字：换行用 <br/>
            return _breaks(self.inline(children), "<br/>")
        if kind == "block_code":
            info = str(attrs.get("info") or "").strip()
            lang = info.split()[0] if info else ""
            code = str(tok.get("raw") or "").rstrip("\n")
            return f'<pre lang="{_attr(lang)}">{html.escape(code, quote=False)}</pre>'
        if kind == "block_quote":
            return f"<blockquote>{self.blocks(children)}</blockquote>"
        if kind == "list":
            if attrs.get("ordered"):
                start = attrs.get("start")
                opening = f'<ol start="{int(start)}">' if start not in (None, 1) else "<ol>"
                return f"{opening}{self.blocks(children)}</ol>"
            return f"<ul>{self.blocks(children)}</ul>"
        if kind == "list_item":
            return f"<li>{self.blocks(children)}</li>"
        if kind == "thematic_break":
            return "<hr/>"
        if kind == "block_html":
            return _COMMENT_RE.sub("", str(tok.get("raw") or "")).strip()
        if kind == "block_math":
            return equation_img(str(tok.get("raw") or ""), display=True)
        if kind == "table":
            return f"<table {TABLE_ATTRS}><tbody>{self.blocks(children)}</tbody></table>"
        if kind == "table_head":
            return f"<tr>{self.blocks(children)}</tr>"
        if kind == "table_body":
            return self.blocks(children)
        if kind == "table_row":
            return f"<tr>{self.blocks(children)}</tr>"
        if kind == "table_cell":
            tag = "th" if attrs.get("head") else "td"
            return f"<{tag}>{_breaks(self.inline(children), ' ')}</{tag}>"
        if kind in ("blank_line", "footnotes"):  # 脚注列表由知乎根据 <sup> 自动生成
            return ""
        if children:
            return self.blocks(children)
        return _text(str(tok.get("raw") or ""))

    def paragraph(self, children: list[dict[str, Any]]) -> str:
        # 只在段落顶层的换行处拆段；标签里面的换行（_INNER_BREAK）写成 <br/>
        parts = [_breaks(p, "<br/>").strip() for p in self.inline(children).split(_BREAK)]
        return "".join(f"<p>{p}</p>" for p in parts if p)

    def heading(self, children: list[dict[str, Any]], level: int) -> str:
        text = _breaks(self.inline(children), " ").strip()
        depth = level - self.heading_base
        if depth <= 0:
            return f"<h2>{text}</h2>"
        if depth == 1:
            return f"<h3>{text}</h3>"
        return f"<p><b>{text}</b></p>"

    # ---- 行内
    def inline(self, tokens: list[dict[str, Any]]) -> str:
        return "".join(self.span(t) for t in tokens)

    def nested(self, tokens: list[dict[str, Any]]) -> str:
        """标签内部的行内内容：里面的换行标成 _INNER_BREAK，外层拆段时不会从标签中间断开。"""
        return self.inline(tokens).replace(_BREAK, _INNER_BREAK)

    def span(self, tok: dict[str, Any]) -> str:
        kind = tok.get("type")
        children = tok.get("children") or []
        attrs = tok.get("attrs") or {}
        raw = str(tok.get("raw") or "")
        if kind == "text":
            return _text(raw)
        if kind in ("softbreak", "linebreak"):
            return _BREAK
        if kind == "strong":
            return f"<b>{self.nested(children)}</b>"
        if kind == "emphasis":
            return f"<i>{self.nested(children)}</i>"
        if kind == "strikethrough":
            return f"<del>{self.nested(children)}</del>"
        if kind == "codespan":
            return f"<code>{html.escape(raw, quote=False)}</code>"
        if kind == "link":
            url = _safe_url(str(attrs.get("url") or ""))
            return f'<a href="{_attr(url)}">{self.nested(children)}</a>'
        if kind == "image":
            return self.image(str(attrs.get("url") or ""), _plain(children))
        if kind == "inline_html":
            return _COMMENT_RE.sub("", raw)
        if kind == "inline_math":
            return equation_img(raw, display=False)
        if kind == "footnote_ref":
            index = int(attrs.get("index") or 0)
            text, url = _footnote(self.footnotes.get(raw) or self.footnotes.get(raw.lower(), ""))
            return (f'<sup data-text="{_attr(text)}" data-url="{_attr(url)}" data-draft-node="inline" '
                    f'data-draft-type="reference" data-numero="{index}">[{index}]</sup>')
        if children:
            return self.nested(children)
        return _text(raw)

    def image(self, url: str, alt: str) -> str:
        if not url.strip():  # ![]() 空图片：丢掉
            return ""
        img = self.images.get(url)
        if img is not None:
            return article_img(img, alt)
        if is_zhimg(url):
            return f'<img src="{_attr(url)}" data-caption="{_attr(alt)}" data-size="normal"/>'
        raise ValueError(f"图片还没有上传到知乎: {url}")


def _breaks(fragment: str, sep: str) -> str:
    """把两种换行占位符都换成 sep（列表项 / 标题 / 表格单元格里不拆段）。"""
    return fragment.replace(_BREAK, sep).replace(_INNER_BREAK, sep)


def _plain(tokens: list[dict[str, Any]]) -> str:
    out = []
    for tok in _walk(tokens):
        if tok.get("type") in ("text", "codespan"):
            out.append(str(tok.get("raw") or ""))
    return html.unescape("".join(out)).strip()


def _first_block(tokens: list[dict[str, Any]]) -> int | None:
    for i, tok in enumerate(tokens):
        if tok.get("type") != "blank_line":
            return i
    return None


def render_article(markdown: str, images: dict[str, ZhihuImage] | None = None, *,
                   title: str = "", append: Iterable[ZhihuImage] = ()) -> str:
    """把 Markdown 渲染成知乎文章 HTML。append 里的图片依次放在正文末尾。"""
    tokens, env = _parse(markdown)
    first = _first_block(tokens)
    if first is not None and tokens[first].get("type") == "heading" \
            and (tokens[first].get("attrs") or {}).get("level") == 1:
        h1s = [t for t in tokens if t.get("type") == "heading" and (t.get("attrs") or {}).get("level") == 1]
        # 只有一个一级标题（通常就是文章标题）或与 title 相同时才丢弃，避免吞掉把 # 当章节用的正文
        if len(h1s) == 1 or _plain(tokens[first].get("children") or []) == title.strip():
            tokens = tokens[:first] + tokens[first + 1:]
    levels = [int((t.get("attrs") or {}).get("level", 1)) for t in tokens if t.get("type") == "heading"]
    renderer = _Renderer(images or {}, dict(env.get("ref_footnotes") or {}), min(levels) if levels else 1)
    body = renderer.blocks(tokens)
    tail = "".join(f"<p>{article_img(img)}</p>" for img in append)
    return body + tail


def pin_html(text: str, images: Iterable[ZhihuImage] = ()) -> str:
    """想法正文：纯文本，一行一个 <p>（空行去掉），图片接在后面。"""
    paragraphs = "".join(f"<p>{html.escape(line.strip(), quote=False)}</p>"
                         for line in text.splitlines() if line.strip())
    return paragraphs + "".join(pin_img(img) for img in images)


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def text_length(fragment: str) -> int:
    """HTML 里可见文字的长度（hybrid.textLength 用；图片、标签不计）。"""
    parser = _TextCollector()
    parser.feed(fragment)
    parser.close()
    return len("".join(parser.parts))
