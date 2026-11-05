"""Reddit lane: posts and their comment trees from archives instead of reddit.com.

reddit.com refuses datacenter IPs (even old.reddit and .json), and new Data API apps
need Reddit's approval. Two archives cover nearly every post:

- Scry (reddit.posts / reddit.comments), used first when a key is available
  (SCRY_API_KEY, ~/.config/scry/env or ~/.scry/.env)
- Arctic Shift (arctic-shift.photon-reddit.com), a public research archive, no key

A post missing from one source is looked up in the other, and so is a comment tree
that comes back empty although the post has comments.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

from .scry import ScryClient, ScryError, load_key
from .store import Store

log = logging.getLogger(__name__)

POST_ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
DELETED = {"[deleted]", "[removed]", ""}
UA = "osiris/0.1 (one-off archive of saved Reddit threads)"


@dataclass
class Post:
    id: str
    subreddit: str
    author: str
    created: datetime
    title: str
    selftext: str
    score: int
    num_comments: int
    url: str
    is_self: bool
    comments: list[dict] = field(default_factory=list)  # {id, parent, author, body, score}


class SourceError(Exception):
    pass


def _parent(parent_id: str | None) -> str | None:
    """t1_<comment> -> comment id; t3_<post> (top level) -> None."""
    return parent_id[3:] if parent_id and parent_id.startswith("t1_") else None


class ScrySource:
    name = "scry"
    BATCH = 100
    COMMENT_WINDOW = timedelta(days=190)  # Reddit archives threads after 6 months by default

    def __init__(self, session: aiohttp.ClientSession, key: str):
        self.client = ScryClient(session, key)

    async def _query(self, sql: str, max_seconds: int) -> list[dict]:
        try:
            return await self.client.query(sql, max_seconds)
        except ScryError as e:
            raise SourceError(str(e)) from e

    async def posts(self, ids: list[str]) -> dict[str, Post]:
        found = {}
        for i in range(0, len(ids), self.BATCH):
            in_list = ",".join(f"'{pid}'" for pid in ids[i : i + self.BATCH])
            rows = await self._query(
                "SELECT id, subreddit, author, created_utc, title, selftext, score, num_comments, url, is_self"
                f" FROM reddit.posts WHERE id IN ({in_list})"
                " ORDER BY state_observed_at DESC LIMIT 1 BY id LIMIT 1000",
                max_seconds=180,
            )
            for r in rows:
                found[r["id"]] = Post(
                    id=r["id"], subreddit=r["subreddit"], author=r["author"],
                    created=datetime.strptime(r["created_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                    title=r["title"], selftext=r["selftext"], score=r["score"], num_comments=r["num_comments"],
                    url=r["url"], is_self=bool(r["is_self"]),
                )
        return found

    async def comments(self, post: Post) -> list[dict]:
        # link_id is a point lookup only inside a closed created_utc window
        lo = post.created.strftime("%Y-%m-%d")
        hi = (post.created + self.COMMENT_WINDOW).strftime("%Y-%m-%d")
        rows = await self._query(
            "SELECT id, parent_id, author, body, score FROM reddit.comments"
            f" WHERE link_id = 't3_{post.id}' AND created_utc >= '{lo}' AND created_utc < '{hi}'"
            " ORDER BY state_observed_at DESC, retrieved_on DESC LIMIT 1 BY id LIMIT 10000",
            max_seconds=120,
        )
        return [{"id": r["id"], "parent": _parent(r["parent_id"]), "author": r["author"],
                 "body": r["body"], "score": r["score"]} for r in rows]


class ArcticShiftSource:
    name = "arctic_shift"
    API_URL = "https://arctic-shift.photon-reddit.com/api"
    BATCH = 100

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.slots = asyncio.Semaphore(2)  # volunteer-run service: stay gentle

    async def _get(self, path: str, params: dict) -> list:
        async with self.slots:
            for attempt in range(1, 5):
                try:
                    async with self.session.get(f"{self.API_URL}{path}", params=params,
                                                timeout=aiohttp.ClientTimeout(total=90)) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            wait = min(float(resp.headers.get("x-ratelimit-reset") or 2**attempt), 120.0)
                            log.warning("reddit/arctic_shift: HTTP %d, waiting %.0fs", resp.status, wait)
                            await asyncio.sleep(wait)
                            continue
                        data = await resp.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    log.warning("reddit/arctic_shift: %s (try %d)", e, attempt)
                    await asyncio.sleep(2**attempt)
                    continue
                if data.get("error"):
                    raise SourceError(str(data["error"]))
                return data.get("data") or []
        raise SourceError("arctic shift unreachable")

    async def posts(self, ids: list[str]) -> dict[str, Post]:
        found = {}
        for i in range(0, len(ids), self.BATCH):
            for p in await self._get("/posts/ids", {"ids": ",".join(ids[i : i + self.BATCH])}):
                found[p["id"]] = Post(
                    id=p["id"], subreddit=p.get("subreddit") or "", author=p.get("author") or "",
                    created=datetime.fromtimestamp(int(p["created_utc"]), timezone.utc),
                    title=p.get("title") or "", selftext=p.get("selftext") or "", score=p.get("score") or 0,
                    num_comments=p.get("num_comments") or 0, url=p.get("url") or "", is_self=bool(p.get("is_self")),
                )
        return found

    async def comments(self, post: Post) -> list[dict]:
        out: list[dict] = []

        def walk(node: dict) -> None:
            if node.get("kind") != "t1":
                return  # "more" stubs only appear past the limit
            c = node["data"]
            out.append({"id": c["id"], "parent": _parent(c.get("parent_id")), "author": c.get("author") or "",
                        "body": c.get("body") or "", "score": c.get("score") or 0})
            replies = c.get("replies")
            if isinstance(replies, dict):
                for child in replies.get("data", {}).get("children", []):
                    walk(child)

        for node in await self._get("/comments/tree", {"link_id": post.id, "limit": "9999"}):
            walk(node)
        return out


def thread_markdown(post: Post) -> tuple[dict, str]:
    permalink = f"https://www.reddit.com/r/{post.subreddit}/comments/{post.id}/"
    lines = [
        f"# {post.title}",
        "",
        f"**r/{post.subreddit}** · u/{post.author} · {post.created:%Y-%m-%d} · "
        f"{post.score} points · {post.num_comments} comments",
    ]
    if not post.is_self and post.url and "reddit.com" not in post.url:
        lines += ["", f"Link: {post.url}"]
    if post.selftext.strip() not in DELETED:
        lines += ["", post.selftext.strip()]

    children: dict[str | None, list[dict]] = defaultdict(list)
    ids = {c["id"] for c in post.comments}
    for c in post.comments:
        # a parent missing from the archive: show the reply at top level rather than drop it
        children[c["parent"] if c["parent"] in ids else None].append(c)
    authors = {c["id"]: c["author"] for c in post.comments}
    rendered: list[str] = []

    def walk(parent: str | None) -> None:
        for c in sorted(children.get(parent, []), key=lambda c: -(c["score"] or 0)):
            body = (c["body"] or "").strip()
            if body not in DELETED:
                to = f", reply to u/{authors[parent]}" if parent else ""
                rendered.extend(["", f"**u/{c['author']}** ({c['score']} points{to}):", "", body])
            walk(c["id"])

    walk(None)
    if rendered:
        lines += ["", f"## Comments ({len(post.comments)})", *rendered]
    meta = {
        "url": permalink,
        "source": "reddit",
        "title": post.title,
        "author": f"u/{post.author}",
        "date": f"{post.created:%Y-%m-%d}",
        "site": f"reddit.com/r/{post.subreddit}",
    }
    return meta, "\n".join(lines)


async def run_reddit(store: Store) -> None:
    pending = store.pending("reddit")
    if not pending:
        return
    ids = sorted({key.split(":", 1)[1] for key, _ in pending if POST_ID_RE.match(key.split(":", 1)[1])})

    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
        sources = [ArcticShiftSource(session)]
        key = load_key()
        if key:
            sources.insert(0, ScrySource(session, key))
        else:
            log.info("reddit: no SCRY_API_KEY, using Arctic Shift only")
        log.info("reddit: %d posts via %s", len(ids), " then ".join(s.name for s in sources))

        found: dict[str, tuple[Post, object]] = {}
        unresolved = False  # a source failed: unfound posts stay pending instead of "not_found"
        for source in sources:
            missing = [pid for pid in ids if pid not in found]
            if not missing:
                break
            try:
                posts = await source.posts(missing)
            except SourceError as e:
                log.error("reddit/%s: post lookup failed: %s", source.name, e)
                unresolved = True
                continue
            log.info("reddit/%s: %d of %d posts found", source.name, len(posts), len(missing))
            for pid, post in posts.items():
                found[pid] = (post, source)

        async def archive(pid: str) -> None:
            post, first = found[pid]
            used = first.name
            for source in [first, *(s for s in sources if s is not first)]:
                try:
                    post.comments = await source.comments(post)
                except SourceError as e:
                    log.warning("reddit/%s: comments of %s failed: %s", source.name, pid, e)
                    continue
                used = source.name if source is first else f"{first.name}+{source.name}"
                if post.comments or post.num_comments == 0:
                    break  # an empty tree for a post with comments: ask the next source
            meta, body = thread_markdown(post)
            meta["source"] = f"reddit:{used}"
            chars = store.write_doc(f"reddit:{pid}", meta, body)
            store.record(f"reddit:{pid}", "ok", chars=chars, final_url=meta["url"], http_status=200)

        results = await asyncio.gather(*(archive(pid) for pid in found), return_exceptions=True)
        for pid, result in zip(found, results):
            if isinstance(result, Exception):
                log.error("reddit: %s failed", pid, exc_info=result)
        if not unresolved:
            for pid in ids:
                if pid not in found:
                    store.record(f"reddit:{pid}", "failed", "not_found")  # deleted before any archive saw it
        store.commit()
