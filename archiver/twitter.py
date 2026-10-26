"""Twitter lane: batch tweet lookup via twitterapi.io (x.com serves an empty JS shell to scrapers)."""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from .store import Store

log = logging.getLogger(__name__)

BASE_URL = "https://api.twitterapi.io"
BATCH = 50  # API maximum; more returns HTTP 400
PAGE_DELAY = 0.5  # same pacing as /root/twitterapi/fetch_tweets.py
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
KEY_ENV = "TWITTERAPI_IO_KEY"


async def _api_get(session: aiohttp.ClientSession, path: str, params: dict) -> dict:
    """Single API call with retries on 429/5xx/network errors."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(f"{BASE_URL}{path}", params=params) as resp:
                if resp.status == 429 or resp.status >= 500:
                    last_exc = aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                else:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            last_exc = e
        if attempt < MAX_RETRIES:
            delay = RETRY_BACKOFF**attempt
            log.warning("twitter: retry %d/%d in %.0fs: %s", attempt, MAX_RETRIES, delay, last_exc)
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _tweet_markdown(t: dict) -> tuple[dict, str]:
    author = t.get("author") or {}
    user = author.get("userName") or "unknown"
    name = author.get("name")
    url = t.get("url") or f"https://x.com/{user}/status/{t['id']}"
    lines = [f"**{name} (@{user})**" if name else f"**@{user}**", "", t.get("text") or ""]
    for key, label in (("retweeted_tweet", "Retweeted"), ("quoted_tweet", "Quoting")):
        inner = t.get(key) or {}
        if inner.get("text"):
            inner_user = (inner.get("author") or {}).get("userName") or "unknown"
            quoted = "\n".join(f"> {ln}" for ln in inner["text"].splitlines())
            lines += ["", f"{label} @{inner_user}:", "", quoted]
    first_line = (t.get("text") or "").strip().splitlines()[0][:100] if (t.get("text") or "").strip() else ""
    meta = {
        "url": url,
        "source": "tweet",
        "title": f"@{user}: {first_line}",
        "author": f"{name} (@{user})" if name else f"@{user}",
        "date": t.get("createdAt"),
        "site": "x.com",
    }
    return meta, "\n".join(lines)


async def run_twitter(store: Store) -> None:
    pending = store.pending("twitter")
    if not pending:
        return
    api_key = os.environ.get(KEY_ENV)
    if not api_key:
        log.warning("twitter: %s not set; leaving %d tweets pending", KEY_ENV, len(pending))
        return
    ids = [key.split(":", 1)[1] for key, _ in pending]
    log.info("twitter: %d tweets in %d calls", len(ids), -(-len(ids) // BATCH))

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(headers={"X-API-Key": api_key}, timeout=timeout) as session:
        for i in range(0, len(ids), BATCH):
            batch = ids[i : i + BATCH]
            try:
                data = await _api_get(session, "/twitter/tweets", {"tweet_ids": ",".join(batch)})
            except Exception as e:
                log.error("twitter: batch %d failed, left pending: %s", i // BATCH, e)
                continue
            found = {str(t.get("id")): t for t in data.get("tweets") or []}
            for tid in batch:
                tweet = found.get(tid)
                if tweet is None:
                    store.record(f"tweet:{tid}", "failed", "not_found")  # deleted, protected or suspended
                    continue
                meta, body = _tweet_markdown(tweet)
                chars = store.write_doc(f"tweet:{tid}", meta, body)
                store.record(f"tweet:{tid}", "ok", chars=chars, final_url=meta["url"], http_status=200)
            store.commit()
            await asyncio.sleep(PAGE_DELAY)
