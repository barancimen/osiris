"""Every tweet of the accounts named in .txt lists, one username per line.

The lists sit in the same folders as the tweet files (tweetdb.py). Each run of the twitter lane
takes what is new since the run before, from whichever of these is turned on:

1. twitterapi.io, when TWITTERAPI_IO_KEY is set: the account's timeline, replies included, newest
   first, 20 tweets a page. It bills per tweet, so a walk stops where the last finished walk began,
   a walk that was cut short carries on from its last page, and a run stops once it has spent
   about --twitterapi-max-dollars.
2. Scry, when it has a key: the tweets its archive holds for the account, some of them deleted
   since, oldest first. Tweets already saved aren't asked for again, and a run stops asking once
   Scry has cost --scry-tweets-max-dollars.

With both on, twitterapi.io goes first and Scry adds what it has beyond that. With neither, the
accounts are skipped. How far each account has got is kept in the manifest's twitter_accounts table.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import aiohttp

from .scry import ScryError, sql_list
from .store import Store, now_iso
from .twitter import KEY_ENV, PAGE_DELAY, Saver, ScryTweets, _api_get, _from_scry, scry_names

log = logging.getLogger(__name__)

LIST_SUFFIX = ".txt"
LIST_MAX_BYTES = 1 << 20  # a longer file isn't a list of usernames
LINE = re.compile(r"(?:(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/)?@?([A-Za-z0-9_]{1,15})/?(?:\?\S*)?", re.I)
NOT_ACCOUNTS = {"home", "i", "search", "explore", "settings", "notifications", "messages", "compose", "intent",
                "share", "hashtag", "login", "signup"}  # x.com's own pages
STATE = ("checked_at", "api_newest", "api_cursor", "api_top", "scry_after", "scry_count")
API_DOLLARS = 5.0
TWEET_NANODOLLARS = 150_000  # twitterapi.io: $0.15 per 1,000 tweets, and a request costs at least one tweet
PAGES_PER_SAVE = 10  # timeline pages written to disk at a time
SCRY_HANDLES = 200  # usernames looked up at once
SCRY_PAGE = 1000  # tweet ids listed at once: about 25 KB, inside what Scry lets out per query


# -- the lists -----------------------------------------------------------------------------------

class NotAList(ValueError):
    def __init__(self, message: str, names: int = 0):
        super().__init__(message)
        self.names = names  # usernames read before the line that isn't one: some means a list with a slip in it


def parse_list(text: str) -> list[str]:
    """The usernames in a list, without the @. Every line has to be a username, an @username or a
    profile link, blank lines and # comments aside: NotAList names the first line that isn't."""
    names = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = LINE.fullmatch(line)
        if not m or m.group(1).lower() in NOT_ACCOUNTS:
            raise NotAList(f"line {n} isn't a username: {line[:60]}", len(names))
        names.append(m.group(1))
    return names


def read_list(path: Path) -> list[str]:
    """NotAList if the file isn't a list of usernames. A file with anything else in it, such as a
    README, is left alone as a whole rather than looked up at a price."""
    try:
        if path.stat().st_size > LIST_MAX_BYTES:
            raise NotAList("it's too long to be a list of usernames")
        return parse_list(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        raise NotAList("it isn't text") from None
    except OSError as e:
        raise NotAList(e.strerror or str(e)) from None


def find_lists(paths: list[Path]) -> list[Path]:
    """The files given, and the .txt files in the folders given, searched recursively."""
    found = set()
    for p in paths:
        for f in [p] if p.is_file() else p.rglob(f"*{LIST_SUFFIX}"):
            if f.is_file() and not any(part.startswith(".") for part in f.parts[len(p.parts):]):
                found.add(f)
    return sorted(found)


def listed(paths: list[Path], quiet: bool = False) -> list[str]:
    """The usernames in every list under these paths, each once, as first written."""
    names: dict[str, str] = {}
    for f in find_lists(paths):
        try:
            for name in read_list(f):
                names.setdefault(name.lower(), name)
        except NotAList as e:
            if not quiet:  # loudly if it began as a list: a slip in one line leaves out every account in it
                log.log(logging.WARNING if e.names else logging.INFO, "%s: not read as a list of accounts: %s", f, e)
    return list(names.values())


# -- how far each account has got ----------------------------------------------------------------

def _state(store: Store, handle: str) -> dict:
    row = store.db.execute(f"SELECT {', '.join(STATE)} FROM twitter_accounts WHERE handle = ?", (handle.lower(),)).fetchone()
    return dict(zip(STATE, row or (None,) * len(STATE)))


def _remember(store: Store, handle: str, **fields) -> None:
    store.db.execute("INSERT OR IGNORE INTO twitter_accounts (handle) VALUES (?)", (handle.lower(),))
    store.db.execute(f"UPDATE twitter_accounts SET {', '.join(f'{c} = ?' for c in fields)} WHERE handle = ?",
                     (*fields.values(), handle.lower()))


# -- twitterapi.io -------------------------------------------------------------------------------

class Budget:
    """What a run may spend on twitterapi.io, counted from its prices: the service doesn't say."""

    def __init__(self, dollars: float):
        self.dollars = dollars
        self.left = int(dollars * 1e9)

    @property
    def spent(self) -> bool:
        return self.left <= 0

    def charge(self, tweets: int) -> None:
        self.left -= max(tweets, 1) * TWEET_NANODOLLARS


def _tweets(found) -> list[dict]:
    return [t for t in found or [] if isinstance(t, dict) and str(t.get("id") or "").isdigit()]


async def _timeline(store: Store, saver: Saver, session: aiohttp.ClientSession, budget: Budget, handle: str) -> bool:
    """Save the account's tweets from twitterapi.io, newest first, down to where a finished walk
    began. False if it had to stop before that."""
    state = _state(store, handle)
    newest, top, cursor = state["api_newest"] or 0, state["api_top"] or 0, state["api_cursor"] or ""
    held: list[dict] = []
    seen, pages, read, saved, restarted = {cursor}, 0, 0, 0, False

    def keep(done: bool = False) -> None:
        """Write what's been read since the last time, then where the walk is."""
        nonlocal saved, held
        saved += saver.save(held, "tweet:twitterapi")
        held = []
        if done:
            _remember(store, handle, api_newest=max(newest, top) or None, api_cursor=None, api_top=None)
        else:
            _remember(store, handle, api_cursor=cursor or None, api_top=top or None)
        store.commit()

    def said() -> str:
        return f"{saved:,} tweets saved from twitterapi.io, of {read:,} it sent"

    try:
        while True:
            if budget.spent:
                keep()
                log.warning("twitter: @%s: reached the $%g limit for twitterapi.io after %s; the next run carries on",
                            handle, budget.dollars, said())
                return False
            try:
                data = await _api_get(session, "/twitter/user/last_tweets",
                                      {"userName": handle, "includeReplies": "true", "cursor": cursor})
            except aiohttp.ClientResponseError as e:
                if not cursor or restarted or e.status not in (400, 404):
                    raise
                log.info("twitter: @%s: twitterapi.io no longer takes the place the last run stopped at; starting from the newest", handle)
                cursor, restarted = "", True
                continue
            if not isinstance(data, dict):
                raise ValueError("twitterapi.io sent something that isn't a page of tweets")
            if data.get("status") == "error":  # no such account, or one twitterapi.io can't read
                budget.charge(0)
                keep()
                log.warning("twitter: @%s: twitterapi.io says: %s", handle, data.get("msg") or data.get("message") or "error")
                return True
            body = data.get("data") if isinstance(data.get("data"), dict) else data
            tweets = _tweets(body.get("tweets"))
            page = [*tweets, *_tweets([body.get("pin_tweet")])]
            budget.charge(len(page))
            read += len(page)
            held += page
            top = max([top, *(int(t["id"]) for t in tweets)])
            following = data.get("next_cursor") or ""
            if (not tweets or not data.get("has_next_page") or not following or following in seen
                    or (newest and all(int(t["id"]) <= newest for t in tweets))):  # the last finished walk has the rest
                break
            seen.add(following)
            cursor = following
            pages += 1
            if pages % PAGES_PER_SAVE == 0:
                keep()
            await asyncio.sleep(PAGE_DELAY)
    except BaseException:  # stopped, or the connection failed: what was read is paid for, so it's kept
        keep()
        raise
    keep(done=True)
    log.info("twitter: @%s: %s", handle, said())
    return True


# -- Scry ----------------------------------------------------------------------------------------

async def _scry_authors(scry: ScryTweets, handles: list[str]) -> dict[str, tuple[str, str, str | None]]:
    """{lowercase username: (account id, username, name)} for the accounts Scry has tweets from."""
    ids: dict[str, str] = {}
    shared = set()
    for i in range(0, len(handles), SCRY_HANDLES):
        batch = [h.lower() for h in handles[i : i + SCRY_HANDLES]]
        for r in await scry.client.query(
                "SELECT author_handle_lc AS handle, toString(author_id) AS author_id FROM twitter.author_handles"
                f" WHERE author_handle_lc IN ({sql_list(batch)}) ORDER BY last_seen DESC LIMIT {5 * len(batch)}"):
            if ids.setdefault(r["handle"], r["author_id"]) != r["author_id"]:
                shared.add(r["handle"])
    for handle in sorted(shared):
        log.info("twitter: more than one account has posted as @%s; taking the one that did so last", handle)
    names: dict[str, tuple[str, str]] = {}
    wanted = sorted(set(ids.values()))
    for i in range(0, len(wanted), SCRY_HANDLES):
        names.update(await scry_names(scry.client, wanted[i : i + SCRY_HANDLES]))
    typed = {h.lower(): h for h in handles}  # the name it goes by now, if Scry knows: a list can hold an earlier one
    return {handle: (aid, names.get(aid, ("", ""))[0] or typed[handle], names.get(aid, ("", ""))[1] or None)
            for handle, aid in ids.items() if handle in typed}


async def _archive(store: Store, saver: Saver, scry: ScryTweets, handle: str, author: tuple[str, str, str | None]) -> bool:
    """Save the tweets Scry's archive has for the account and that aren't saved yet, oldest first.
    False if the money for Scry ran out before the end."""
    aid = author[0]
    state = _state(store, handle)
    after, count = state["scry_after"] or 0, state["scry_count"] or 0
    if after:  # Scry finds older tweets as it goes on: if it has more up to there than last time, look again
        had = await scry.client.query(f"SELECT uniqExact(tweet_id) AS n FROM twitter.tweets_by_author"
                                      f" WHERE author_id = {aid} AND tweet_id <= {after} LIMIT 1")
        if int(had[0]["n"]) != count:
            after = count = 0
    saved = 0
    while not scry.spent:
        ids = [r["id"] for r in await scry.client.query(
            f"SELECT toString(tweet_id) AS id FROM twitter.tweets_by_author WHERE author_id = {aid}"
            f" AND tweet_id > {after} GROUP BY tweet_id ORDER BY tweet_id LIMIT {SCRY_PAGE}")]
        tweets, left = await _from_scry(scry, [i for i in ids if not saver.has(i)], author)
        saved += saver.save(tweets.values(), "tweet:scry")
        reached = min(map(int, left)) - 1 if left else int(ids[-1]) if ids else after
        count += sum(int(i) <= reached for i in ids)
        after = reached
        _remember(store, handle, scry_after=after, scry_count=count)
        store.commit()
        if left:
            break
        if len(ids) < SCRY_PAGE:
            log.info("twitter: @%s: %s tweets saved from Scry's archive, which has %s", handle, f"{saved:,}", f"{count:,}")
            return True
    log.warning("twitter: @%s: reached the $%g limit for Scry after %s tweets; the next run carries on",
                handle, scry.max_dollars, f"{saved:,}")
    return False


# -- both ----------------------------------------------------------------------------------------

def _accounts(n: int) -> str:
    return f"{n:,} account{'' if n == 1 else 's'}"


async def run_accounts(store: Store, handles: list[str], scry: ScryTweets | None, api_key: str | None,
                       api_dollars: float = API_DOLLARS) -> None:
    if not scry and not api_key:
        log.warning("twitter: %s skipped: %s isn't set, and Scry has no key or is turned off", _accounts(len(handles)), KEY_ENV)
        return
    log.info("twitter: taking the tweets of %s from %s", _accounts(len(handles)),
             " and ".join(name for name, on in (("twitterapi.io", api_key), ("Scry", scry)) if on))
    saver = Saver(store)
    authors: dict[str, tuple[str, str, str | None]] = {}
    if scry:
        try:
            authors = await _scry_authors(scry, handles)
        except ScryError as e:
            log.error("twitter: Scry couldn't look the accounts up: %s", e)
            scry = None
    budget = Budget(api_dollars)
    session = aiohttp.ClientSession(headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=60)) if api_key else None
    try:
        for n, handle in enumerate(handles):
            if (not session or budget.spent) and (not scry or scry.spent):
                log.warning("twitter: %s left for the next run", _accounts(len(handles) - n))
                break
            done = True
            if session:
                try:
                    done = await _timeline(store, saver, session, budget, handle)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError, AttributeError) as e:
                    log.error("twitter: @%s: twitterapi.io failed, so the next run tries again: %s", handle, e)
                    done = False
            if scry:
                author = authors.get(handle.lower())
                try:
                    if author:
                        done = await _archive(store, saver, scry, handle, author) and done
                    elif not session:
                        log.info("twitter: @%s: Scry has no tweets under that name", handle)
                except ScryError as e:
                    log.error("twitter: Scry failed on @%s, so it's left alone for the rest of this run: %s", handle, e)
                    scry, done = None, False
            if done:
                _remember(store, handle, checked_at=now_iso())
            store.commit()
    finally:
        store.commit()
        if session:
            await session.close()
