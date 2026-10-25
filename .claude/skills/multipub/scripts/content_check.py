"""发布前的内容检查：找出渲染结果和作者本意可能不符的写法。只报告，不修改；修法见 references/content-check.md。

- 知乎文章（Markdown）：用 multipub 自己的解析器（mistune：CommonMark + 表格、删除线、脚注、公式、自动链接）解析，
  按语法树找残留的 ** / __、被配成删除线的 ~、被识别成斜体的 * / _、被当成 HTML 的 <xxx>、被当成公式的 $、
  被合并的单个换行、没识别出来的表格 / 列表、行首被误识别成标题 / 有序列表 / 引用、缩进代码块、表格列数不一致。代码块和行内代码不查。
- 小红书 / B站 / 抖音 / 知乎想法的正文（纯文本）：平台不渲染 Markdown，找出会原样显示的 Markdown 符号、正文里的 #话题、超长正文。

在仓库根目录运行（用 multipub 的环境，因为要用它的知乎解析器）：

  uv run python .claude/skills/multipub/scripts/content_check.py <post.yaml 或其目录>   # 按清单的 kind 和平台自动选
  uv run python .claude/skills/multipub/scripts/content_check.py --md article.md          # 只按知乎 Markdown 查
  uv run python .claude/skills/multipub/scripts/content_check.py --plain 正文.txt [--limit 1000]

不在仓库目录时用 uv run --project <仓库路径> python <脚本路径> ...
  加 --topics：清单里有知乎时，联网（只读）核对话题有没有同名的（没有同名会被 multipub 换成最接近的，可能是书籍）。

退出码：0 没有错误（可能有提醒）；1 有错误；2 用法或文件错误。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:  # noqa: BLE001
    pass

ERR, WARN = '错误', '提醒'
CJK_PUNCT = '。，、；：！？“”‘’（）《》【】…—'
KNOWN_TAGS = {'a', 'b', 'i', 'em', 'strong', 'br', 'sup', 'sub', 'span', 'u', 's', 'del', 'img', 'p', 'div', 'hr',
              'code', 'pre', 'table', 'tr', 'td', 'th', 'thead', 'tbody', 'blockquote', 'ul', 'ol', 'li', 'figure'}
PLAIN_LIMIT = {'bilibili': 2000, 'douyin': 1000, 'xiaohongshu': 1000, 'zhihu': 0}


class Report:
    def __init__(self, name: str, src: str):
        self.name, self.src, self.items = name, src, []

    def line_of(self, snippet: str) -> int:
        """按片段在源文件里找行号（找不到返回 0）。"""
        snippet = snippet.strip()[:20]
        pos = self.src.find(snippet) if snippet else -1
        return self.src.count('\n', 0, pos) + 1 if pos >= 0 else 0

    def add(self, level: str, what: str, snippet: str, line: int | None = None, hint: str = ''):
        line = self.line_of(snippet) if line is None else line
        self.items.append((level, line, what, snippet.replace('\n', '⏎')[:60], hint))

    def print(self) -> int:
        errs = sum(1 for i in self.items if i[0] == ERR)
        print(f'== {self.name}：{errs} 个错误，{len(self.items) - errs} 个提醒')
        for level, line, what, snippet, hint in sorted(self.items, key=lambda i: (i[1], i[0])):
            loc = f'第 {line} 行 ' if line else ('位置未知 ' if self.src else '')
            tail = f'：…{snippet}…' if snippet else ''
            print(f'  [{level}] {loc}{what}{tail}' + (f'\n         → {hint}' if hint else ''))
        return errs


# ------------------------------------------------------------------ 知乎 Markdown

def _plain(tokens) -> str:
    out = []
    for t in tokens or []:
        if t.get('type') in ('text', 'codespan', 'inline_html', 'inline_math'):
            out.append(str(t.get('raw') or ''))
        elif t.get('type') in ('softbreak', 'linebreak'):
            out.append('\n')
        out.append(_plain(t.get('children')))
    return ''.join(out)


def _blank_front_matter(src: str) -> str:
    """front matter 换成空行（保持行号），multipub 发布时也会去掉它。"""
    m = re.match(r'---\n.*?\n---\n', src, re.S)
    return '\n' * m.group(0).count('\n') + src[m.end():] if m else src


def _split_cells(row: str) -> list[str]:
    """按未转义、不在反引号里的 | 切表格行。"""
    row = row.strip()
    cells, cur, in_code, i = [], '', False, 0
    while i < len(row):
        ch = row[i]
        if ch == '\\' and i + 1 < len(row):
            cur += row[i:i + 2]
            i += 2
            continue
        if ch == '`':
            in_code = not in_code
        if ch == '|' and not in_code:
            cells.append(cur)
            cur = ''
        else:
            cur += ch
        i += 1
    cells.append(cur)
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return cells


def _check_tables_source(src: str, rep: Report) -> None:
    lines = src.split('\n')
    in_fence = False
    i = 0
    while i < len(lines):
        if re.match(r'\s*(```|~~~)', lines[i]):
            in_fence = not in_fence
        if (not in_fence and lines[i].lstrip().startswith('|') and i + 1 < len(lines)
                and re.fullmatch(r'\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*', lines[i + 1])):
            n = len(_split_cells(lines[i]))
            if i > 0 and lines[i - 1].strip():
                rep.add(ERR, '表格前缺空行（会和上一段连在一起，表格不被识别）', lines[i], i + 1,
                        '在表格上方加一个空行')
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith('|'):
                k = len(_split_cells(lines[j]))
                if k != n:
                    rep.add(WARN, f'表格这一行有 {k} 格、表头 {n} 格', lines[j], j + 1,
                            '单元格里的 | 要写成 \\|；多出的格会被丢掉或整张表不被识别')
                j += 1
            i = j
            continue
        i += 1


def check_markdown(src: str, name: str) -> Report:
    from multipub.platforms.zhihu_html import _parse  # multipub 发知乎用的同一个解析器

    src = _blank_front_matter(src)
    rep = Report(name, src)
    tokens, _ = _parse(src)

    def text_issues(raw: str):
        if '**' in raw or '__' in raw:
            mark = '**' if '**' in raw else '__'
            spots = [m.start() for m in re.finditer(re.escape(mark), raw)]
            # 优先指出“标点 + ** + 文字”这种不能闭合的位置
            bad = [i for i in spots if i > 0 and raw[i - 1] in CJK_PUNCT + '.,;:!?)"\''
                   and i + 2 < len(raw) and not raw[i + 2].isspace() and raw[i + 2] not in CJK_PUNCT]
            i = bad[0] if bad else spots[0]
            hint = ('结尾的 ** 紧贴标点、后面又紧跟文字时不算闭合：把标点挪到 ** 外面（**结论**。正文）'
                    if bad else '检查 ** 是否成对、两侧是否紧贴标点或空格')
            rep.add(ERR, f'{mark} 没生效，会原样显示', raw[max(0, i - 15):i + 15], hint=hint)
        elif re.search(r'(?<![*\\])\*(?!\*)', raw):
            i = raw.find('*')
            rep.add(WARN, '有单个 * 原样显示', raw[max(0, i - 15):i + 15], hint='本意是乘号/星号就不用改；本意是强调就检查配对')
        if '~~' in raw:
            i = raw.find('~~')
            rep.add(ERR, '~~ 没生效，会原样显示', raw[max(0, i - 15):i + 15])

    def walk(toks, parent=''):
        table_reported = False  # 同一段里没识别的表格只报一次
        for idx, t in enumerate(toks or []):
            kind = t.get('type')
            ch = t.get('children') or []
            if kind == 'text':
                text_issues(str(t.get('raw') or ''))
            elif kind == 'strikethrough':
                rep.add(WARN, '被渲染成删除线', _plain(ch), hint='本意不是删除线时，把 ~ 写成 \\~ 或改用“至”“到”')
            elif kind == 'emphasis':
                rep.add(WARN, '被渲染成斜体', _plain(ch),
                        hint='本意不是斜体时，把 * 或 _ 写成 \\* \\_，或用反引号包起来')
            elif kind in ('inline_math', 'block_math'):
                rep.add(WARN, '被当成公式', str(t.get('raw') or _plain(ch)), hint='本意是金额/符号时把 $ 写成 \\$')
            elif kind in ('inline_html', 'block_html'):
                raw = str(t.get('raw') or '')
                tag = re.match(r'</?\s*([A-Za-z][\w-]*)', raw)
                if raw.startswith('<!--'):
                    pass
                elif tag and tag.group(1).lower() in KNOWN_TAGS:
                    rep.add(WARN, '当成 HTML 标签处理', raw, hint='确认本意就是 HTML')
                else:
                    rep.add(ERR, '被当成 HTML，正文里会消失', raw, hint='把 < 写成 \\< 或 &lt;，或用反引号包起来')
            elif kind == 'softbreak' and parent in ('paragraph', 'block_text'):
                nxt = _plain(toks[idx + 1:idx + 2]) if idx + 1 < len(toks) else ''
                prev = _plain(toks[max(0, idx - 1):idx])
                if nxt.lstrip().startswith('|'):
                    if not table_reported:
                        first = prev if prev.lstrip().startswith('|') else nxt
                        rep.add(ERR, '表格没被识别（前面缺空行，或某行列数和表头不一致）', first)
                        table_reported = True
                elif re.match(r'\s*([-*+]|\d+[.)])\s', nxt):
                    rep.add(ERR, '列表没被识别（前面缺空行）', nxt, hint='在列表上方加一个空行')
                else:
                    rep.add(WARN, '单个换行会被合并成同一段（知乎上不换行）', f'{prev[-15:]}⏎{nxt[:15]}',
                            line=rep.line_of(nxt), hint='要分段就空一行；要段内换行就在行尾加两个空格或 \\')
            elif kind == 'block_code' and t.get('style') != 'fenced':
                rep.add(WARN, '缩进 4 格以上被当成代码块', str(t.get('raw') or ''), hint='去掉行首缩进；真要代码块就用 ``` 围起来')
            elif kind == 'heading' and len(_plain(ch)) > 40:
                rep.add(WARN, '被识别成标题，但这行很长', _plain(ch), hint='行首 # 本意不是标题时写成 \\#')
            elif kind == 'list' and (t.get('attrs') or {}).get('ordered') and int((t.get('attrs') or {}).get('start') or 1) >= 100:
                start = (t.get('attrs') or {}).get('start')
                rep.add(WARN, f'行首「{start}.」被当成有序列表', f'{start}. ' + _plain(ch)[:20],
                        hint='年份等数字后跟“. ”时写成 2025\\. 或去掉点后的空格')
            elif kind == 'block_quote':
                rep.add(WARN, '行首 > 被当成引用', _plain(ch), hint='本意不是引用时写成 \\>')
            walk(ch, kind)

    # 开头唯一的一级标题会被 multipub 当成文章标题去掉，不检查它
    first = next((t for t in tokens if t.get('type') != 'blank_line'), None)
    h1s = [t for t in tokens if t.get('type') == 'heading' and (t.get('attrs') or {}).get('level') == 1]
    if first is not None and first.get('type') == 'heading' and (first.get('attrs') or {}).get('level') == 1 \
            and len(h1s) == 1:
        tokens = [t for t in tokens if t is not first]
    walk(tokens)
    _check_tables_source(src, rep)
    return rep


# ------------------------------------------------------------------ 纯文本正文

def check_plain(src: str, name: str, limit: int = 0) -> Report:
    rep = Report(name, src)
    for n, line in enumerate(src.split('\n'), 1):
        for pat, level, what, hint in (
                (r'\*\*|__', ERR, 'Markdown 加粗符号会原样显示', '纯文本平台不认 **，删掉；要强调就用「」或 emoji'),
                (r'~~', ERR, 'Markdown 删除线符号会原样显示', '删掉 ~~'),
                (r'\[[^\]\n]+\]\([^)\n]+\)', ERR, 'Markdown 链接会原样显示', '写成纯文字；正文也不要放站外链接'),
                (r'^\s*#{1,6}\s', ERR, 'Markdown 标题符号会原样显示', '删掉行首 #'),
                (r'^\s*\|.*\|\s*$', ERR, 'Markdown 表格会原样显示', '改成分行的文字'),
                (r'`', WARN, '反引号会原样显示', '删掉反引号'),
                (r'^\s*>\s', WARN, '行首 > 会原样显示', '删掉行首 >'),
                (r'(?:^|\s)#[^\s#]+', WARN, '正文里的 #话题 只是普通文字，不会变成话题', '话题写在清单的 tags 里')):
            m = re.search(pat, line)
            if m:
                rep.add(level, what, line[max(0, m.start() - 10):m.end() + 15], n, hint)
    if limit and len(src) > limit:
        rep.add(ERR, f'正文 {len(src)} 字，超过 {limit} 字上限', src[:20], 1)
    return rep


# ------------------------------------------------------------------ 知乎话题（联网、只读）

def check_zhihu_topics(tags: list[str], account: str) -> Report:
    """逐个查知乎话题联想：没有同名话题时，multipub 会换成最接近的（如「量化投资」→「量化投资（书籍）」）。"""
    import asyncio

    from multipub.config import load_settings
    from multipub.platforms import zhihu_api as Z

    rep = Report(f'知乎话题 {tags}', '')

    async def run():
        acc = load_settings().account_file('zhihu', account)  # 数据目录同 mpub（MULTIPUB_HOME 或默认位置）
        async with Z.make_client(Z.load_cookies(acc, account=account)) as client:
            api = Z.ZhihuApi(client, account=account)
            for tag in tags[:3]:  # 知乎只用前 3 个
                data = await api.call('GET', Z.TOPIC_AUTOCOMPLETE_URL, what=f'搜索话题「{tag}」',
                                      referer=f'{Z.ZHUANLAN}/write',
                                      params={'token': tag, 'max_matches': 5, 'use_similar': 0, 'topic_filter': 1})
                data = data.get('data') if isinstance(data, dict) else data
                names = [str(t.get('name') or '') for t in (data or []) if isinstance(t, dict)]
                if tag in names:
                    continue
                close = [n for n in names if n and (tag in n or n in tag)]
                got = (close or names or ['（没有任何结果，会跳过）'])[0]
                level = WARN if '书籍' not in got else ERR
                rep.add(level, f'话题「{tag}」没有同名的，会被换成「{got}」', '、'.join(names[:5]), 0,
                        '换一个有同名话题的词（候选见上），或接受替换')
            if len(tags) > 3:
                rep.add(WARN, f'知乎只用前 3 个话题，后面的 {tags[3:]} 不会用', '', 0)

    asyncio.run(run())
    return rep


# ------------------------------------------------------------------ 按清单检查

def check_post(path: Path, topics: bool = False) -> list[Report]:
    import yaml

    post = path / 'post.yaml' if path.is_dir() else path
    base = post.parent
    data = yaml.safe_load(post.read_text(encoding='utf-8')) or {}
    kind = data.get('kind')
    platforms = data.get('platforms') or {}

    def body_of(block: dict, fallback: tuple[str, str] | None):
        if block.get('body_file'):
            f = base / block['body_file']
            return f.name, f.read_text(encoding='utf-8')
        if block.get('body'):
            return f'{post.name} 的 body', str(block['body'])
        return fallback

    top = body_of(data, None)
    reports, seen = [], set()
    for plat in platforms or {'(顶层)': {}}:
        block = platforms.get(plat) or {} if isinstance(platforms, dict) else {}
        got = body_of(block, top)
        if not got:
            continue
        label, text = got
        if kind == 'article' and plat in ('zhihu', '(顶层)'):
            key = ('md', text)
            if key not in seen:
                seen.add(key)
                reports.append(check_markdown(text, f'{plat} · {label}（知乎 Markdown）'))
        else:
            key = ('plain', plat, text)
            if key not in seen:
                seen.add(key)
                reports.append(check_plain(text, f'{plat} · {label}（纯文本）', PLAIN_LIMIT.get(plat, 0)))
    if topics and 'zhihu' in (platforms or {}):
        zblock = platforms.get('zhihu') or {}
        tags = [str(t) for t in (zblock.get('tags') or data.get('tags') or [])]
        if tags:
            reports.append(check_zhihu_topics(tags, str(zblock.get('account') or data.get('account') or 'default')))
    return reports


def main() -> int:
    ap = argparse.ArgumentParser(description='发布前内容检查（只报告不修改）')
    ap.add_argument('path', help='post.yaml、它所在的目录，或配合 --md / --plain 的正文文件')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--md', action='store_true', help='按知乎 Markdown 检查')
    g.add_argument('--plain', action='store_true', help='按纯文本正文检查')
    ap.add_argument('--limit', type=int, default=0, help='纯文本字数上限（--plain 时用）')
    ap.add_argument('--topics', action='store_true', help='清单里有知乎时，联网（只读）核对话题有没有同名的')
    a = ap.parse_args()
    p = Path(a.path)
    if not p.exists():
        print(f'找不到 {p}', file=sys.stderr)
        return 2
    if a.md:
        reports = [check_markdown(p.read_text(encoding='utf-8'), f'{p.name}（知乎 Markdown）')]
    elif a.plain:
        reports = [check_plain(p.read_text(encoding='utf-8'), f'{p.name}（纯文本）', a.limit)]
    else:
        reports = check_post(p, topics=a.topics)
        if not reports:
            print('清单里没有正文可查')
            return 0
    errs = sum(r.print() for r in reports)
    print('结论：' + ('有错误，修完再发布' if errs else '没有错误（提醒项请逐条确认）'))
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
