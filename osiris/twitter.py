"""Twitter lane: tweet text without scraping x.com (it serves an empty JS shell).

1. Scry's Twitter archive (it also holds some tweets deleted since), when a Scry key is
   configured (SCRY_API_KEY in .env). Scry lets about 35 KB of tweets out per query and
   charges for what a day takes past about 2,000 tweets, so tweets are asked for a few
   dozen at a time, and a run stops asking once it has spent --scry-tweets-max-dollars.
2. twitterapi.io's batch lookup for the tweets Scry lacks, only when TWITTERAPI_IO_KEY is
   set (it bills per tweet). Without it those tweets are recorded as not_in_scry, ready
   for --retry-failed later.

Before either, the accounts listed with --twitter-accounts get all their tweets (accounts.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable

import aiohttp

from .route import route
from .scry import ScryClient, ScryError, load_key
from .store import Store

log = logging.getLogger(__name__)

BASE_URL = "https://api.twitterapi.io"
BATCH = 50  # API maximum; more returns HTTP 400
PAGE_DELAY = 0.5  # same pacing as /root/twitterapi/fetch_tweets.py
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
KEY_ENV = "TWITTERAPI_IO_KEY"
SLICE = 2000  # waiting tweets looked up and saved at a time, so a stopped run keeps what it has
SCRY_DOLLARS = 1.0
SCRY_BATCH = 80  # tweets asked for at once: Scry refuses a result much over 35 KB of them
SCRY_NAMES = 300  # accounts' names asked for at once
SCRY_TOO_MUCH = "query_resource_limit_exceeded"
SCRY_REFUSALS = 3  # single tweets refused in a row before Scry is left alone for the run
# The newest observation of each tweet, from the raw relation: twitter.tweets_latest does the same
# fold but reads every column, which cost 13 times as much for the same tweets. A tweet's whole text
# and its author's name can be on different observations, so each column takes its own best one.
SCRY_COLUMNS = ("toString(tweet_id) AS id, toString(any(author_id)) AS author_id,"
                " argMax(author_handle, (author_handle != '', version)) AS author_handle,"
                " argMax(author_display_name, (author_display_name != '', version)) AS author_display_name,"
                " toString(argMax(original_timestamp, version)) AS created, argMax(text, version) AS text,"
                " toString(argMax(quoted_tweet_id, version)) AS quoted_id,"
                " argMax(article_title, (article_title != '', version)) AS article_title,"
                " argMax(article_text, (article_text != '', version)) AS article_text")


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


class Saver:
    """Writes tweets that come from somewhere other than a link you saved (a file, or an account's
    timeline), each once, keyed by its ID, so a username that changed later still finds the same
    document. A tweet already saved, or deleted on purpose, is left alone. One that's known but
    still waiting or failed (such as not_in_scry) gets the text."""

    def __init__(self, store: Store):
        self.store = store
        self.waiting: set[str] = set()
        self.settled: set[str] = set()
        for key, status in store.db.execute("SELECT doc_key, status FROM urls WHERE doc_key LIKE 'tweet:%'"):
            (self.waiting if status in ("pending", "failed") else self.settled).add(key)
        self.waiting -= self.settled

    def has(self, tweet_id: str) -> bool:
        return f"tweet:{tweet_id}" in self.settled

    def save(self, tweets: Iterable[dict], source: str) -> int:
        """Tweets in twitterapi.io's shape. Returns how many were written."""
        tweets = [t for t in tweets if str(t.get("id") or "").isdigit()]
        new = []
        for t in tweets:
            key = f"tweet:{t['id']}"
            if key not in self.waiting and key not in self.settled:
                user = (t.get("author") or {}).get("userName")
                r = route(t.get("url") or f"https://x.com/{user or 'i/web'}/status/{t['id']}")
                if r is None or r.doc_key != key:  # an address that isn't this tweet's: use the plain one
                    r = route(f"https://x.com/i/web/status/{t['id']}")
                new.append(r)
        if new:  # into the manifest first, all at once, so each document below has its rows to record against
            self.store.add(new)
            self.waiting.update(r.doc_key for r in new)
        written = 0
        for t in tweets:
            key = f"tweet:{t['id']}"
            if key in self.settled:
                continue
            meta, body = _tweet_markdown(t)
            meta["source"] = source
            chars = self.store.write_doc(key, meta, body)
            self.store.record(key, "ok", chars=chars, final_url=meta["url"], http_status=200)
            self.waiting.discard(key)
            self.settled.add(key)
            written += 1
        return written


def _ids_sql(ids) -> str:
    return ",".join(i for i in ids if i.isdigit())


class ScryTweets:
    """Scry's tweets, asked for a batch at a time until the run's money for them is spent."""

    def __init__(self, client: ScryClient, max_dollars: float = SCRY_DOLLARS):
        self.client = client
        self.max_dollars = max_dollars
        self.stop_at = ScryClient.spent_nanodollars + int(max_dollars * 1e9)
        self.batch = SCRY_BATCH
        self.refused = 0

    @property
    def spent(self) -> bool:
        return ScryClient.spent_nanodollars >= self.stop_at

    async def rows(self, ids: list[str], author_id: str | None = None) -> tuple[dict[str, dict], list[str]]:
        """(a row for each tweet Scry has, the ids not asked about because the money ran out).
        `author_id`: the ids are all this account's, which Scry then finds far more cheaply."""
        found: dict[str, dict] = {}
        todo = [i for i in ids if i.isdigit()]
        where = f"author_id = {author_id} AND " if author_id else ""
        while todo and not self.spent:
            batch = todo[: self.batch]
            try:
                got = await self.client.query(
                    f"SELECT {SCRY_COLUMNS} FROM twitter.tweets WHERE {where}tweet_id IN ({','.join(batch)})"
                    f" GROUP BY tweet_id LIMIT {len(batch)}", max_seconds=120)
            except ScryError as e:
                if e.code != SCRY_TOO_MUCH:
                    raise
                if len(batch) > 1:  # too much text for one answer: ask for less
                    self.batch = len(batch) // 2
                    continue
                self.refused += 1
                if self.refused >= SCRY_REFUSALS:  # it isn't the size of one tweet, then
                    raise
                log.warning("twitter: Scry won't send tweet %s, which is too long for one answer; skipped", batch[0])
                todo = todo[1:]
                continue
            self.refused = 0
            self.batch = min(SCRY_BATCH, self.batch + 5)
            found.update((r["id"], r) for r in got)
            todo = todo[len(batch):]
        return found, todo


async def _from_scry(scry: ScryTweets, ids: list[str],
                     author: tuple[str, str, str | None] | None = None) -> tuple[dict[str, dict], list[str]]:
    """(tweets from Scry's archive in twitterapi.io's shape, so one renderer serves both; the ids
    not asked about because the money ran out). `author`: (id, username, name) when the ids are
    all one account's."""
    tweets, left = await scry.rows(ids, author[0] if author else None)
    quoted, unasked = await scry.rows(sorted({t["quoted_id"] for t in tweets.values() if t["quoted_id"] != "0"} - tweets.keys()))
    if unasked:  # the money ran out between the two: a tweet waits rather than lose what it quotes
        cut = set(unasked)
        left = [*left, *(tid for tid, t in tweets.items() if t["quoted_id"] in cut)]
        tweets = {tid: t for tid, t in tweets.items() if t["quoted_id"] not in cut}
    everything = {**quoted, **tweets}
    # some observations carry the author's id but not the handle
    blank = sorted({t["author_id"] for t in everything.values() if not t["author_handle"]} - ({author[0]} if author else set()))
    names: dict[str, tuple[str, str]] = {}
    for i in range(0, len(blank), SCRY_NAMES):
        names.update(await scry_names(scry.client, blank[i : i + SCRY_NAMES]))

    def who(t: dict) -> dict:
        if author and t["author_id"] == author[0]:
            handle, name = author[1], author[2] or t["author_display_name"]
        elif t["author_handle"]:
            handle, name = t["author_handle"], t["author_display_name"]
        else:
            handle, name = names.get(t["author_id"], ("", ""))
        return {"userName": handle or None, "name": name or None}

    out = {}
    for tid, t in tweets.items():
        text = t["text"]
        if t["article_title"]:  # an X article: keep its body, not just the link
            text += f"\n\n## {t['article_title']}\n\n{t['article_text']}".rstrip()
        a = who(t)
        q = everything.get(t["quoted_id"])
        out[tid] = {
            "id": tid,
            "text": text,
            "createdAt": (t["created"] or "")[:19] or None,  # drop the zero microseconds; a rare tweet has no time
            "url": f"https://x.com/{a['userName'] or 'i/web'}/status/{tid}",
            "author": a,
            "quoted_tweet": {"text": q["text"], "author": who(q)} if q else None,
        }
    return out, left


async def scry_names(client: ScryClient, author_ids: list[str]) -> dict[str, tuple[str, str]]:
    """{author id: (username, name)} as Scry last saw them."""
    return {r["author_id"]: (r["handle"], r["name"]) for r in await client.query(
        "SELECT toString(author_id) AS author_id, argMax(handle, observed_on) AS handle,"
        " argMax(display_name, observed_on) AS name FROM twitter.users"
        f" WHERE author_id IN ({_ids_sql(author_ids)}) AND handle != ''"
        f" GROUP BY author_id LIMIT {len(author_ids)}", max_seconds=120)} if author_ids else {}


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


async def run_twitter(store: Store, use_scry: bool = True, accounts: Iterable[str] = (),
                      scry_dollars: float = SCRY_DOLLARS, api_dollars: float = 5.0) -> None:
    """`accounts`: usernames whose every tweet is wanted, taken before the tweets that are waiting."""
    scry_key, api_key = load_key() if use_scry else None, os.environ.get(KEY_ENV)
    accounts = list(accounts)
    async with aiohttp.ClientSession() as session:
        scry = ScryTweets(ScryClient(session, scry_key), scry_dollars) if scry_key else None
        if accounts:
            from .accounts import run_accounts

            await run_accounts(store, accounts, scry, api_key, api_dollars)
        pending = store.pending("twitter")
        if not pending:
            return
        ids = [key.split(":", 1)[1] for key, _ in pending]
        if not scry and not api_key:
            log.warning("twitter: %s and %s not set; leaving %d tweets pending",
                        "Scry has no key" if use_scry else "Scry is turned off", KEY_ENV, len(ids))
            return
        await _look_up(store, ids, scry, api_key)


async def _look_up(store: Store, ids: list[str], scry: ScryTweets | None, api_key: str | None) -> None:
    asked = in_scry = waiting = 0
    for start in range(0, len(ids), SLICE):
        part = ids[start : start + SLICE]
        if not api_key and (scry is None or scry.spent):
            waiting += len(ids) - start
            break
        found: dict[str, tuple[dict, str]] = {}
        unasked = set(part)  # what Scry wasn't asked about, or didn't answer
        if scry:
            try:
                tweets, left = await _from_scry(scry, part)
                found = {tid: (t, "scry") for tid, t in tweets.items()}
                unasked = set(left)
                asked += len(part) - len(unasked)
                in_scry += len(tweets)
            except ScryError as e:
                log.error("twitter: Scry lookup failed: %s", e)
                scry = None  # for the rest of the run
        missing = [tid for tid in part if tid not in found]
        unresolved: set[str] = set()
        if missing and api_key:
            log.info("twitter: looking up %d tweets on twitterapi.io (%d calls)", len(missing), -(-len(missing) // BATCH))
            tweets, unresolved = await _from_twitterapi(api_key, missing)
            found.update((tid, (t, "twitterapi")) for tid, t in tweets.items())
        for tid in part:
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
            elif tid not in unasked:
                store.record(f"tweet:{tid}", "failed", "not_in_scry")
            else:
                waiting += 1
        store.commit()
    if asked:
        log.info("twitter: %d of %d tweets found in Scry's archive", in_scry, asked)
    if scry and scry.spent and waiting:
        log.warning("twitter: reached the $%g limit for Scry; %d tweets wait for the next run", scry.max_dollars, waiting)
    elif waiting:
        log.info("twitter: %d tweets wait for the next run", waiting)
    if in_scry < asked and not api_key:
        log.info("twitter: %d tweets not in Scry; set %s to look them up on twitterapi.io", asked - in_scry, KEY_ENV)
