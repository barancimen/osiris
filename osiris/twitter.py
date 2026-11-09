"""Twitter lane: tweet text without scraping x.com (it serves an empty JS shell).

1. Scry's Twitter archive (free; it also holds some tweets deleted since), when a Scry key
   is configured (SCRY_API_KEY, ~/.config/scry/env or ~/.scry/.env)
2. twitterapi.io's batch lookup for the tweets Scry lacks, only when TWITTERAPI_IO_KEY is
   set (it bills per tweet). Without it those tweets are recorded as not_in_scry, ready
   for --retry-failed later.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from .scry import ScryClient, ScryError, load_key
from .store import Store

log = logging.getLogger(__name__)

BASE_URL = "https://api.twitterapi.io"
BATCH = 50  # API maximum; more returns HTTP 400
PAGE_DELAY = 0.5  # same pacing as /root/twitterapi/fetch_tweets.py
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
KEY_ENV = "TWITTERAPI_IO_KEY"
SCRY_BATCH = 500  # keyed reads take up to 1000 literal ids


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


def _ids_sql(ids) -> str:
    return ",".join(i for i in ids if i.isdigit())


async def _from_scry(client: ScryClient, ids: list[str]) -> dict[str, dict]:
    """Tweets from Scry's archive in twitterapi.io's shape, so one renderer serves both."""
    cols = ("toString(tweet_id) AS id, toString(author_id) AS author_id, author_handle, author_display_name,"
            " toString(original_timestamp) AS created, text, toString(quoted_tweet_id) AS quoted_id,"
            " article_title, article_text")

    async def read(wanted: list[str]) -> dict[str, dict]:
        rows = {}
        for i in range(0, len(wanted), SCRY_BATCH):
            for r in await client.query(f"SELECT {cols} FROM twitter.tweets_latest"
                                        f" WHERE tweet_id IN ({_ids_sql(wanted[i : i + SCRY_BATCH])})"
                                        f" LIMIT {2 * SCRY_BATCH}", max_seconds=180):
                rows[r["id"]] = r
        return rows

    tweets = await read(ids)
    quoted = await read(sorted({t["quoted_id"] for t in tweets.values() if t["quoted_id"] != "0"} - tweets.keys()))
    everything = {**quoted, **tweets}
    # about 8% of observations carry the author's id but not the handle
    blank = sorted({t["author_id"] for t in everything.values() if not t["author_handle"]})
    names: dict[str, tuple[str, str]] = {}
    for i in range(0, len(blank), SCRY_BATCH):
        for r in await client.query(
            "SELECT toString(author_id) AS author_id, argMax(handle, observed_on) AS handle,"
            " argMax(display_name, observed_on) AS name FROM twitter.users"
            f" WHERE author_id IN ({_ids_sql(blank[i : i + SCRY_BATCH])}) AND handle != ''"
            f" GROUP BY author_id LIMIT {SCRY_BATCH}", max_seconds=120):
            names[r["author_id"]] = (r["handle"], r["name"])

    def author(t: dict) -> dict:
        handle, name = (t["author_handle"], t["author_display_name"]) if t["author_handle"] else \
            names.get(t["author_id"], ("", ""))
        return {"userName": handle or None, "name": name or None}

    out = {}
    for tid, t in tweets.items():
        text = t["text"]
        if t["article_title"]:  # an X article: keep its body, not just the link
            text += f"\n\n## {t['article_title']}\n\n{t['article_text']}".rstrip()
        a = author(t)
        q = everything.get(t["quoted_id"])
        out[tid] = {
            "id": tid,
            "text": text,
            "createdAt": t["created"][:19],  # drop the zero microseconds
            "url": f"https://x.com/{a['userName'] or 'i/web'}/status/{tid}",
            "author": a,
            "quoted_tweet": {"text": q["text"], "author": author(q)} if q else None,
        }
    return out


async def _from_twitterapi(api_key: str, ids: list[str]) -> tuple[dict[str, dict], set[str]]:
    """(tweets found, ids whose batch failed and so stay unresolved)."""
    found, unresolved = {}, set()
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(headers={"X-API-Key": api_key}, timeout=timeout) as session:
        for i in range(0, len(ids), BATCH):
            batch = ids[i : i + BATCH]
            try:
                data = await _api_get(session, "/twitter/tweets", {"tweet_ids": ",".join(batch)})
            except Exception as e:
                log.error("twitter: twitterapi.io batch %d failed, left pending: %s", i // BATCH, e)
                unresolved.update(batch)
                continue
            for t in data.get("tweets") or []:
                found[str(t.get("id"))] = t
            await asyncio.sleep(PAGE_DELAY)
    return found, unresolved


async def run_twitter(store: Store, use_scry: bool = True) -> None:
    pending = store.pending("twitter")
    if not pending:
        return
    ids = [key.split(":", 1)[1] for key, _ in pending]
    scry_key, api_key = load_key() if use_scry else None, os.environ.get(KEY_ENV)
    if not scry_key and not api_key:
        log.warning("twitter: %s and %s not set; leaving %d tweets pending",
                    "Scry has no key" if use_scry else "Scry is turned off", KEY_ENV, len(ids))
        return

    found: dict[str, tuple[dict, str]] = {}
    unresolved: set[str] = set()
    scry_answered = False
    if scry_key:
        async with aiohttp.ClientSession() as session:
            try:
                for tid, t in (await _from_scry(ScryClient(session, scry_key), ids)).items():
                    found[tid] = (t, "scry")
                scry_answered = True
            except ScryError as e:
                log.error("twitter: Scry lookup failed: %s", e)
        log.info("twitter: %d of %d tweets found in Scry's archive", len(found), len(ids))
    missing = [tid for tid in ids if tid not in found]
    if missing and api_key:
        log.info("twitter: looking up %d tweets on twitterapi.io (%d calls)", len(missing), -(-len(missing) // BATCH))
        tweets, unresolved = await _from_twitterapi(api_key, missing)
        found.update((tid, (t, "twitterapi")) for tid, t in tweets.items())
    elif missing:
        log.info("twitter: %d tweets not in Scry; set %s to look them up on twitterapi.io", len(missing), KEY_ENV)

    for tid in ids:
        if tid in found:
            tweet, source = found[tid]
            meta, body = _tweet_markdown(tweet)
            meta["source"] = f"tweet:{source}"
            chars = store.write_doc(f"tweet:{tid}", meta, body)
            store.record(f"tweet:{tid}", "ok", chars=chars, final_url=meta["url"], http_status=200)
        elif tid in unresolved:
            continue
        elif api_key:
            store.record(f"tweet:{tid}", "failed", "not_found")  # deleted, protected or suspended
        elif scry_answered:
            store.record(f"tweet:{tid}", "failed", "not_in_scry")
    store.commit()
