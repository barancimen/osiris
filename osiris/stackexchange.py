"""Stack Exchange lane: questions + answers via the public API.

The sites sit behind a Cloudflare challenge for datacenter IPs, but api.stackexchange.com
does not, and it takes 100 question IDs per call. Without a key the quota is 300
calls/day per IP; set STACKEXCHANGE_KEY for 10,000.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp

from .store import Store

log = logging.getLogger(__name__)

API_URL = "https://api.stackexchange.com/2.3"
# default filter + body_markdown on questions, answers and comments (filters are permanent).
# comment.body must be in it too: the API leaves out a comment's body_markdown otherwise. And
# a filter can embed comments in their posts, but those come back with no text at all.
FILTER = "!)RL-JogHwoZuazwxM.JDy-xQ"
WITH_COMMENTS = "comments"  # in the manifest's `tried`: this document was fetched with its comments
BATCH = 100
MIN_DELAY = 0.1
MAX_TRIES = 4
KEY_ENV = "STACKEXCHANGE_KEY"


class QuotaExhausted(Exception):
    pass


class SEClient:
    def __init__(self, session: aiohttp.ClientSession, key: str | None):
        self.session = session
        self.key = key
        self.quota_remaining = None

    async def get(self, path: str, site: str, **params) -> dict:
        params = {"site": site, "filter": FILTER, "pagesize": "100", **params}
        if self.key:
            params["key"] = self.key
        for attempt in range(1, MAX_TRIES + 1):
            try:
                async with self.session.get(f"{API_URL}{path}", params=params) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                log.warning("stackexchange: %s (try %d)", e, attempt)
                await asyncio.sleep(2**attempt)
                continue
            # the API asks clients to honor "backoff" before calling the same method again
            await asyncio.sleep(max(MIN_DELAY, data.get("backoff") or 0))
            if "error_id" not in data:
                self.quota_remaining = data.get("quota_remaining", self.quota_remaining)
                if data.get("quota_remaining") == 0:
                    log.warning("stackexchange: daily quota used up after this call")
                return data
            if data.get("error_name") == "throttle_violation":
                # covers both short throttles and an exhausted daily quota
                m = re.search(r"available in (\d+) seconds", data.get("error_message") or "")
                wait = int(m.group(1)) if m else 30 * attempt
                if wait > 300:
                    raise QuotaExhausted(data.get("error_message"))
                await asyncio.sleep(wait)
                continue
            raise ValueError(f"{data.get('error_name')}: {data.get('error_message')}")
        raise RuntimeError(f"stackexchange: giving up on {path}")

    async def all_pages(self, path: str, site: str, **params) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            data = await self.get(path, site, page=str(page), **params)
            items += data["items"]
            if not data.get("has_more"):
                return items
            page += 1

    async def comments(self, kind: str, site: str, ids: list[str]) -> dict[int, list[dict]]:
        """Comments by the id of their post, oldest first."""
        by_post: dict[int, list[dict]] = defaultdict(list)
        for i in range(0, len(ids), BATCH):
            joined = ";".join(ids[i : i + BATCH])
            for c in await self.all_pages(f"/{kind}/{joined}/comments", site, sort="creation", order="asc"):
                by_post[c["post_id"]].append(c)
        return by_post

    async def thread(self, site: str, ids: list[str]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        """Questions by id and their answers (best first), with comments, for up to BATCH ids."""
        joined = ";".join(ids)
        questions = {str(q["question_id"]): q for q in (await self.get(f"/questions/{joined}", site))["items"]}
        answers: dict[str, list[dict]] = defaultdict(list)
        for a in await self.all_pages(f"/questions/{joined}/answers", site, sort="votes", order="desc"):
            answers[str(a["question_id"])].append(a)
        on_questions = await self.comments("questions", site, list(questions))
        for q in questions.values():
            q["comments"] = on_questions.get(q["question_id"], [])
        on_answers = await self.comments("answers", site, [str(a["answer_id"]) for ans in answers.values() for a in ans])
        for ans in answers.values():
            for a in ans:
                a["comments"] = on_answers.get(a["answer_id"], [])
        return questions, answers


def _date(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d") if epoch else None


def _owner(post: dict) -> str:
    return html.unescape((post.get("owner") or {}).get("display_name") or "anonymous")


def _comments(post: dict) -> list[str]:
    lines = []
    for c in post.get("comments") or []:
        text = " ".join(html.unescape(c.get("body_markdown") or "").split())
        if text:
            score = f" (score {c['score']})" if c.get("score") else ""
            lines.append(f"- **{_owner(c)}**{score}: {text}")
    return ["", "**Comments:**", "", *lines] if lines else []


def thread_markdown(site: str, q: dict, answers: list[dict]) -> tuple[dict, str]:
    title = html.unescape(q.get("title") or "")
    lines = [
        f"# {title}",
        "",
        f"**Asked by** {_owner(q)} · score {q.get('score', 0)} · tags: {', '.join(q.get('tags') or [])}",
        "",
        html.unescape(q.get("body_markdown") or "").strip(),
        *_comments(q),
    ]
    for a in sorted(answers, key=lambda a: (not a.get("is_accepted"), -a.get("score", 0))):
        accepted = ", accepted" if a.get("is_accepted") else ""
        lines += ["", f"## Answer by {_owner(a)} (score {a.get('score', 0)}{accepted})", "",
                  html.unescape(a.get("body_markdown") or "").strip(), *_comments(a)]
    meta = {
        "url": q.get("link"),
        "source": "stackexchange",
        "title": title,
        "author": _owner(q),
        "date": _date(q.get("creation_date")),
        "site": site,
    }
    return meta, "\n".join(lines)


async def run_stackexchange(store: Store, refetch: bool = False) -> None:
    """With refetch, also update finished documents saved before comments were collected. They
    stay `ok` throughout, so a question since deleted keeps its document, and a run stopped by
    the daily quota picks up where it left off."""
    pending = store.pending("stackexchange")
    refetching = set()
    if refetch:
        stale = store.done_without("stackexchange", WITH_COMMENTS)
        refetching = {key for key, _ in stale}
        pending = pending + stale
        log.info("stackexchange: re-fetching %d documents to add comments", len(stale))
    if not pending:
        return
    by_site: dict[str, list[str]] = defaultdict(list)
    for key, _ in pending:  # se:<site>:<question id>
        _, site, qid = key.split(":")
        by_site[site].append(qid)
    log.info("stackexchange: %d questions on %d sites", len(pending), len(by_site))

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = SEClient(session, os.environ.get(KEY_ENV))
        for site, qids in sorted(by_site.items(), key=lambda kv: -len(kv[1])):
            for i in range(0, len(qids), BATCH):
                batch = qids[i : i + BATCH]
                try:
                    questions, answers = await client.thread(site, batch)
                except QuotaExhausted as e:
                    log.error("stackexchange: quota exhausted (%s); the rest stays pending, re-run tomorrow", e)
                    return
                except (ValueError, RuntimeError) as e:
                    log.error("stackexchange: %s batch failed: %s", site, e)
                    for qid in batch:
                        if f"se:{site}:{qid}" not in refetching:
                            store.record(f"se:{site}:{qid}", "failed", "api_error")
                    continue
                for qid in batch:
                    key = f"se:{site}:{qid}"
                    q = questions.get(qid)
                    if q is None:
                        if key in refetching:  # gone since it was archived: what we have is now the only copy
                            log.info("stackexchange: %s is gone; keeping the archived document", key)
                            store.mark_tried(key, WITH_COMMENTS)
                        else:
                            store.record(key, "failed", "not_found")  # deleted or migrated
                        continue
                    meta, body = thread_markdown(site, q, answers.get(qid, []))
                    chars = store.write_doc(key, meta, body)
                    store.record(key, "ok", chars=chars, final_url=meta["url"], http_status=200)
                    store.mark_tried(key, WITH_COMMENTS)
                store.commit()
