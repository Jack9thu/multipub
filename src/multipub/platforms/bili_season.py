"""B站视频合集：投稿成功后立即把稿件加入合集（不用等审核）。

和投稿页的做法一致（创作中心 videoup 前端：投稿成功后调 season/section/episodes/add）：
1. 发布前按合集名查出小节 id（GET /x2/creative/web/seasons）；
2. 投稿成功后用创作中心的稿件接口取 cid（GET /x/vupre/web/archive/view，审核中的自己的稿件也能查。
   biliup season add 用的是公开接口 /x/web-interface/view，审核中返回 -404，所以以前只能过审后再加）；
3. POST /x2/creative/web/season/section/episodes/add（字段是 sectionId + episodes + csrf），
   再看小节的稿件列表里有没有这条：以列表为准，不只看接口返回 OK。

2026-10-26 真实账号实测：审核中（state -30）的稿件取到 cid、加入合集成功，合集里显示为待审核。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from ..models import PublishError
from . import bili_http

MEMBER = "https://member.bilibili.com"
SEASONS_URL = f"{MEMBER}/x2/creative/web/seasons"
ARCHIVE_VIEW_URL = f"{MEMBER}/x/vupre/web/archive/view"
EPISODES_ADD_URL = f"{MEMBER}/x2/creative/web/season/section/episodes/add"
SECTION_URL = f"{MEMBER}/x2/creative/web/season/section"
REFERER = f"{MEMBER}/platform/upload/video/frame"

SEASONS_PAGE_SIZE = 30
SEASONS_MAX_PAGES = 10
# 刚投稿时稿件接口可能还查不到；合集写接口太频繁会返回 20111 / 256000
RETRY_TIMES = 4
RETRY_DELAY = 3.0
RATE_LIMIT_CODES = frozenset({20111, 256000})


@dataclass
class Section:
    season_id: int
    season_title: str
    section_id: int
    section_title: str
    sections: int  # 合集里的小节数（多于 1 个时用第一个，并提示）


def _sections(item: dict[str, Any]) -> list[dict[str, Any]]:
    secs = (item.get("sections") or {}).get("sections") or []
    return [s for s in secs if isinstance(s, dict) and s.get("id")]


async def list_seasons(client: httpx.AsyncClient, *, account: str) -> list[dict[str, Any]]:
    """自己的合集（含草稿），每项是 {season: {...}, sections: {sections: [...]}}。"""
    items: list[dict[str, Any]] = []
    for page in range(1, SEASONS_MAX_PAGES + 1):
        data = await bili_http.request_json(
            client, "GET", SEASONS_URL, what="读取合集列表",
            params={"pn": page, "ps": SEASONS_PAGE_SIZE, "draft": 1})
        bili_http.raise_for_code(data, what="读取合集列表", account=account)
        d = data.get("data") or {}
        batch = [s for s in d.get("seasons") or [] if isinstance(s, dict)]
        items.extend(batch)
        total = d.get("total")
        if len(batch) < SEASONS_PAGE_SIZE or (isinstance(total, int) and len(items) >= total):
            break
    return items


def find_section(items: list[dict[str, Any]], name: str) -> Section | None:
    """按合集名精确匹配（去掉首尾空白），用第一个小节。"""
    name = name.strip()
    for item in items:
        season = item.get("season") or {}
        secs = _sections(item)
        if str(season.get("title") or "").strip() == name and secs:
            return Section(int(season["id"]), str(season.get("title")), int(secs[0]["id"]),
                           str(secs[0].get("title") or ""), len(secs))
    return None


def season_titles(items: list[dict[str, Any]]) -> list[str]:
    return [str((i.get("season") or {}).get("title") or "") for i in items]


async def archive_ids(client: httpx.AsyncClient, *, aid: str | None, bvid: str | None,
                      account: str) -> tuple[int, int]:
    """自己稿件的 (aid, 第一个分 P 的 cid)。创作中心接口，审核中也能查。"""
    params = {"aid": aid} if aid else {"bvid": bvid}
    data = await bili_http.request_json(client, "GET", ARCHIVE_VIEW_URL, what="读取稿件信息", params=params)
    if data.get("code") not in (0, None, -101, -111):  # 刚投稿可能还查不到（如 -404），重试
        raise PublishError(f"读取稿件信息：B站返回 {data.get('code')}: {data.get('message')}", retryable=True)
    bili_http.raise_for_code(data, what="读取稿件信息", account=account)
    d = data.get("data") or {}
    videos = d.get("videos") or []
    cid = videos[0].get("cid") if videos and isinstance(videos[0], dict) else None
    found_aid = (d.get("archive") or {}).get("aid") or aid
    if not cid or not found_aid:
        raise PublishError("读取稿件信息：还查不到 aid / cid", retryable=True)
    return int(found_aid), int(cid)


async def add_episode(client: httpx.AsyncClient, section_id: int, *, aid: int, cid: int, title: str,
                      csrf: str, account: str) -> None:
    body = {"sectionId": section_id, "episodes": [{"aid": aid, "cid": cid, "title": title}], "csrf": csrf}
    data = await bili_http.request_json(client, "POST", EPISODES_ADD_URL, what="加入合集",
                                        params={"csrf": csrf}, json=body)
    if data.get("code") in RATE_LIMIT_CODES:
        raise PublishError(f"加入合集：操作太频繁（{data.get('code')}）", retryable=True)
    bili_http.raise_for_code(data, what="加入合集", account=account)


async def in_section(client: httpx.AsyncClient, section_id: int, aid: int, *, account: str) -> None:
    """核实小节的稿件列表里有这条稿件；还没有就抛可重试的错误。"""
    data = await bili_http.request_json(client, "GET", SECTION_URL, what="核对合集", params={"id": section_id})
    bili_http.raise_for_code(data, what="核对合集", account=account)
    episodes = (data.get("data") or {}).get("episodes") or []
    if not any(isinstance(e, dict) and str(e.get("aid")) == str(aid) for e in episodes):
        raise PublishError("接口返回成功，但合集的稿件列表里还没有这条稿件", retryable=True)


async def _retry(fn, *, log=None, what: str):
    """可重试的错误（网络、还查不到、太频繁）最多试 RETRY_TIMES 次；其余直接抛出。"""
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            return await fn()
        except PublishError as exc:
            if not exc.retryable or attempt == RETRY_TIMES:
                raise
            if log:
                log(f"{what}失败（{exc}），{RETRY_DELAY:g} 秒后重试（{attempt}/{RETRY_TIMES}）", level="debug")
            await asyncio.sleep(RETRY_DELAY)


async def join(client: httpx.AsyncClient, section: Section, *, aid: str | None, bvid: str | None, title: str,
               csrf: str, account: str, log=None) -> None:
    """投稿成功后调用：取 cid → 加入小节 → 核对。失败抛 PublishError / LoginRequired / NeedsAction。"""
    aid_, cid = await _retry(lambda: archive_ids(client, aid=aid, bvid=bvid, account=account),
                             log=log, what="读取稿件 cid")
    await _retry(lambda: add_episode(client, section.section_id, aid=aid_, cid=cid, title=title,
                                     csrf=csrf, account=account), log=log, what="加入合集")
    await _retry(lambda: in_section(client, section.section_id, aid_, account=account), log=log, what="核对合集")


def make_client(cookies: dict[str, str], *, timeout: float = 20) -> httpx.AsyncClient:
    return bili_http.make_client(cookies, referer=REFERER, timeout=timeout)
