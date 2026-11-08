"""Hacker News and LessWrong lanes: whole discussions from the sites' own public APIs.

Fetched as ordinary pages these lose most of what matters: a Hacker News page is nearly all
comments, which the extractor drops, and LessWrong's server-rendered page holds only a preview
of the post.

- Hacker News: Algolia's item API returns a story or comment with every reply in one call. A
  link to one comment archives that comment and its replies.
- LessWrong: its GraphQL API returns a post as markdown with all its comments. Alignment Forum
  and GreaterWrong show the same posts under the same ids, so they share one document.

--refetch-comments goes over every finished document these lanes should hold, including pages
the generic lane saved before they existed. A document changes only when the API has at least as
much text as it, so a thread deleted or emptied since keeps what was archived.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable

import aiohttp

from . import review
from .route import route
from .store import Store

log = logging.getLogger(__name__)

UA = "osiris/0.1 (https://github.com/barancimen/osiris)"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{}"
HN_FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
LW_GRAPHQL = "https://www.lesswrong.com/graphql"
LW_COMMENT_LIMIT = 20_000  # one call, far past the largest thread
LW_QUERY = """
query($id: String, $terms: JSON) {
  post(input: {selector: {_id: $id}}) {
    result { title pageUrl url postedAt baseScore user { displayName } contents { markdown } }
  }
  comments(input: {terms: $terms}) {
    results { _id parentCommentId baseScore deleted user { displayName } contents { markdown } }
  }
}"""
MAX_TRIES = 4
DELETED_AUTHOR = "[deleted]"

Found = tuple[dict, str]
Fetch = Callable[[aiohttp.ClientSession, str], Awaitable[Found | None]]


class ForumError(Exception):
    pass


async def _call(session: aiohttp.ClientSession, method: str, url: str, **kwargs) -> dict | None:
    """The decoded response, or None if the site says there's no such item."""
    problem = ""
    for attempt in range(1, MAX_TRIES + 1):
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 404:
                    return None
                status = resp.status
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            problem = repr(e)
        else:
            errors = data.get("errors") if isinstance(data, dict) else None
            if errors and any("missing_document" in str(e.get("message")) for e in errors):
                return None
            if status == 200 and not errors:
                return data
            problem = f"HTTP {status} {errors or ''}"
        log.warning("%s: %s (try %d)", url, problem, attempt)
        await asyncio.sleep(2**attempt)
    raise ForumError(problem)


def comment_lines(comments: list[dict], score_label: str = "points") -> list[str]:
    """Each comment under its parent, highest score first; {id, parent, author, body, score}."""
    ids = {c["id"] for c in comments}
    children: dict[str | None, list[dict]] = defaultdict(list)
    for c in comments:
        # a parent missing from the API: show the reply at top level rather than drop it
        children[c["parent"] if c["parent"] in ids else None].append(c)
    for siblings in children.values():
        siblings.sort(key=lambda c: -(c["score"] or 0))
    authors = {c["id"]: c["author"] for c in comments}
    lines: list[str] = []
    stack = list(reversed(children[None]))  # not recursion: arguments run deep
    while stack:
        c = stack.pop()
        if c["body"].strip():
            about = []
            if c["score"] is not None:
                about.append(f"{c['score']} {score_label}")
            if c["parent"] in ids:
                about.append(f"reply to {authors[c['parent']]}")
            lines += ["", f"**{c['author']}**" + (f" ({', '.join(about)})" if about else "") + ":", "", c["body"].strip()]
        stack.extend(reversed(children.get(c["id"], [])))
    return lines


def _with_comments(lines: list[str], rendered: list[str], heading: str = "Comments") -> str:
    if rendered:
        count = sum(1 for line in rendered if line.startswith("**"))
        lines = [*lines, "", f"## {heading} ({count})", *rendered]
    return "\n".join(lines)


# -- Hacker News ------------------------------------------------------------------------------


def hn_text(text: str | None) -> str:
    """Hacker News's little HTML as markdown. Anything meant literally arrives escaped, so what
    looks like a tag is one."""
    if not text:
        return ""

    def code(m: re.Match) -> str:
        return "\n\n```\n" + m.group(1).strip("\n") + "\n```\n\n"

    text = re.sub(r"<pre><code>(.*?)</code></pre>", code, text, flags=re.S)
    text = re.sub(r'<a [^>]*href="([^"]*)"[^>]*>.*?</a>', r"\1", text, flags=re.S)  # the page shortens the visible text
    text = re.sub(r"</?i>", "*", text.replace("<p>", "\n\n"))
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def hn_thread(item: dict, story_title: str | None = None) -> Found:
    comments: list[dict] = []
    stack = list(item.get("children") or [])
    while stack:
        c = stack.pop(0)
        comments.append({"id": c["id"], "parent": c.get("parent_id"), "author": c.get("author") or DELETED_AUTHOR,
                         "body": hn_text(c.get("text")), "score": None})
        stack[:0] = c.get("children") or []
    is_comment = item.get("type") == "comment"
    author = item.get("author") or DELETED_AUTHOR
    title = item.get("title") or (f"Comment by {author}" + (f" on: {story_title}" if story_title else ""))
    date = (item.get("created_at") or "")[:10]
    points = f"{item['points']} points" if item.get("points") is not None else ""
    lines = [f"# {title}", "", " · ".join(filter(None, ("**Hacker News**", author, date, points)))]
    if item.get("url"):
        lines += ["", f"Link: {item['url']}"]
    if is_comment and item.get("story_id"):
        lines += ["", f"On: https://news.ycombinator.com/item?id={item['story_id']}"]
    if text := hn_text(item.get("text")):
        lines += ["", text]
    meta = {"url": f"https://news.ycombinator.com/item?id={item['id']}", "source": "hackernews", "title": title,
            "author": author, "date": date, "site": "news.ycombinator.com"}
    return meta, _with_comments(lines, comment_lines(comments), "Replies" if is_comment else "Comments")


async def _fetch_hn(session: aiohttp.ClientSession, key: str) -> Found | None:
    item = await _call(session, "GET", HN_ITEM.format(key.split(":", 1)[1]))
    if item is None or not (item.get("author") or item.get("children")):  # deleted
        return None
    story_title = None
    if item.get("type") == "comment" and item.get("story_id"):
        try:  # only for the heading, and this API sends one item, not the whole story's thread
            story = await _call(session, "GET", HN_FIREBASE_ITEM.format(item["story_id"]))
            story_title = (story or {}).get("title")
        except ForumError:
            pass
    return hn_thread(item, story_title)


# -- LessWrong --------------------------------------------------------------------------------


def _lw_author(thing: dict) -> str:
    return (thing.get("user") or {}).get("displayName") or DELETED_AUTHOR


def _lw_markdown(thing: dict) -> str:
    return (thing.get("contents") or {}).get("markdown") or ""


def lw_thread(post: dict, raw_comments: list[dict]) -> Found:
    comments = [{"id": c["_id"], "parent": c.get("parentCommentId"), "author": _lw_author(c),
                 "body": "" if c.get("deleted") else _lw_markdown(c), "score": c.get("baseScore")}
                for c in raw_comments]
    date = (post.get("postedAt") or "")[:10]
    author = _lw_author(post)
    lines = [f"# {post.get('title') or ''}", "",
             " · ".join(filter(None, ("**LessWrong**", author, date, f"{post.get('baseScore') or 0} karma")))]
    if post.get("url"):
        lines += ["", f"Link: {post['url']}"]
    if text := _lw_markdown(post).strip():
        lines += ["", text]
    meta = {"url": post.get("pageUrl"), "source": "lesswrong", "title": post.get("title"), "author": author,
            "date": date, "site": "lesswrong.com"}
    return meta, _with_comments(lines, comment_lines(comments, "karma"))


async def _fetch_lw(session: aiohttp.ClientSession, key: str) -> Found | None:
    post_id = key.split(":", 1)[1]
    terms = {"view": "postCommentsOld", "postId": post_id, "limit": LW_COMMENT_LIMIT}
    data = await _call(session, "POST", LW_GRAPHQL, json={"query": LW_QUERY, "variables": {"id": post_id, "terms": terms}})
    post = data and ((data.get("data") or {}).get("post") or {}).get("result")
    if not post:
        return None
    return lw_thread(post, data["data"]["comments"]["results"] or [])


# -- both --------------------------------------------------------------------------------------


async def _run(store: Store, lane: str, fetch: Fetch, concurrency: int, refetch: bool) -> None:
    pending = {key for key, _ in store.pending(lane)}
    archived = store.archived_in(lane, route) if refetch else {}
    if not pending and not archived:
        return
    log.info("%s: %d to fetch, %d already archived to update", lane, len(pending), len(archived))
    slots = asyncio.Semaphore(concurrency)
    replaced: set[str] = set()
    outcome = {"written": 0, "kept": 0}

    async def archive(session: aiohttp.ClientSession, key: str) -> None:
        async with slots:
            try:
                found = await fetch(session, key)
            except ForumError as e:
                log.warning("%s: %s failed: %s", lane, key, e)
                found = None
                reason = "api_error"
            else:
                reason = "not_found"
        chars_before, urls = archived.get(key, (0, []))
        if found is None or (key not in pending and len(found[1].strip()) < chars_before):
            if key in pending:
                store.record(key, "failed", reason)
            else:
                log.info("%s: keeping what's archived for %s", lane, key)
                outcome["kept"] += 1
            return
        meta, body = found
        chars = store.write_doc(key, meta, body)
        if urls:
            replaced.update(store.adopt(key, lane, urls))
        store.record(key, "ok", chars=chars, final_url=meta["url"], http_status=200)
        outcome["written"] += 1

    timeout = aiohttp.ClientTimeout(total=180)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=timeout) as session:
            keys = sorted(pending | set(archived))
            results = await asyncio.gather(*(archive(session, key) for key in keys), return_exceptions=True)
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                log.error("%s: %s failed", lane, key, exc_info=result)
        log.info("%s: %d written, %d kept as they were", lane, outcome["written"], outcome["kept"])
    finally:
        store.commit()
        review.forget(store, replaced)


async def run_hackernews(store: Store, refetch: bool = False) -> None:
    await _run(store, "hackernews", _fetch_hn, 4, refetch)


async def run_lesswrong(store: Store, refetch: bool = False) -> None:
    await _run(store, "lesswrong", _fetch_lw, 2, refetch)
