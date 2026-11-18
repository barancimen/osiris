"""Tweets from files of an account's own tweets:
- SQLite databases written by /root/twitterapi/fetch_tweets.py: a `tweets` table in twitterapi.io's fields
- Community Archive uploads: Twitter's account archive as one JSON file, with `account` and `tweets`
  at the top, and the full text of long tweets under `note-tweet`

Each tweet becomes a document like the twitter lane's, keyed by its ID, so a username that changed
later still finds the same document. A tweet already saved, or deleted on purpose, is left alone.
One that's known but still waiting or failed (such as not_in_scry) gets the file's text.
Archives are read as a stream, a tweet at a time: they can be far bigger than this machine's memory.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from .route import route
from .store import Store
from .twitter import _tweet_markdown

log = logging.getLogger(__name__)

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SUFFIXES = DB_SUFFIXES | {".json"}
REQUIRED = {"id", "text", "created_at", "author_username"}
OPTIONAL = ("url", "author_name", "retweeted_text")
RETWEET = re.compile(r"^RT @(\w{1,15}):")
ARCHIVE_TWEETS = ("tweets", "community-tweet")
MENTIONS = re.compile(r"^(?:@\w{1,15}\s+)+")  # a reply's leading @names, which a long tweet's full text leaves out
BATCH = 5000  # tweets held in memory at once
CHUNK = 1 << 20


def find_files(paths: list[Path]) -> list[Path]:
    """The files given, and the database and JSON files in the folders given, searched recursively."""
    found = set()
    for p in paths:
        for f in [p] if p.is_file() else p.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUFFIXES and not any(part.startswith(".") for part in f.parts[len(p.parts):]):
                found.add(f)
    return sorted(found)


def _date(created_at: str | None) -> str | None:
    """Twitter's 'Wed Sep 16 22:18:18 +0000 2026' as '2026-09-16 22:18:18' (UTC), the form Scry's tweets have."""
    try:
        return datetime.strptime(created_at or "", "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return created_at


# -- fetch_tweets.py's databases ------------------------------------------------------------------

def _columns(db: sqlite3.Connection) -> set[str] | None:
    """The tweets table's columns, or None if this isn't a tweet database."""
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(tweets)")}
    except sqlite3.DatabaseError:
        return None
    return cols if REQUIRED <= cols else None


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _count_db(path: Path) -> int | None:
    try:
        db = _open(path)
    except sqlite3.Error:
        return None
    try:
        return db.execute("SELECT COUNT(*) FROM tweets").fetchone()[0] if _columns(db) else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def _db_rows(db: sqlite3.Connection, cols: set[str]) -> Iterator[dict]:
    fields = ", ".join(["CAST(id AS TEXT) AS id", "text", "created_at", "author_username",
                        *(c if c in cols else f"NULL AS {c}" for c in OPTIONAL)])
    cursor = db.execute(f"SELECT {fields} FROM tweets ORDER BY CAST(id AS INTEGER)")
    names = [d[0] for d in cursor.description]
    for values in cursor:
        yield dict(zip(names, values))


# -- Community Archive uploads --------------------------------------------------------------------

def _stream(path: Path) -> Iterator[tuple[str, object]]:
    """(key, value) for each value at the top of a JSON object, and (key, element) for each element
    of an array there, decoding one at a time. ValueError if the file isn't a JSON object."""
    decoder, gap = json.JSONDecoder(), re.compile(r"[\s,:]*")
    with open(path, encoding="utf-8") as f:
        buf, pos, eof = "", 0, False

        def fill() -> bool:
            nonlocal buf, pos, eof
            more = f.read(CHUNK)
            if not more:
                eof = True
                return False
            buf, pos = buf[pos:] + more, 0
            return True

        def peek() -> str:
            nonlocal pos
            while True:
                pos = gap.match(buf, pos).end()
                if pos < len(buf):
                    return buf[pos]
                if not fill():
                    raise ValueError("the file ends too soon")

        def decode():
            nonlocal pos
            while True:
                try:
                    value, end = decoder.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    if fill():
                        continue
                    raise
                if end == len(buf) and not eof and fill():  # a number may go on in the next chunk
                    continue
                pos = end
                return value

        if peek() != "{":
            raise ValueError("not a JSON object")
        pos += 1
        while peek() != "}":
            key = decode()
            if not isinstance(key, str):
                raise ValueError("not a JSON object")
            if peek() == "[":
                pos += 1
                while peek() != "]":
                    yield key, decode()
                pos += 1
            else:
                yield key, decode()


def _archive_account(path: Path) -> dict | None:
    """The archive's account, read from its start, or None if this isn't a Community Archive upload."""
    account = None
    try:
        for key, value in _stream(path):
            if key == "account" and isinstance(value, dict):
                account = value.get("account") or value
            elif key in ARCHIVE_TWEETS:
                return account
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return None


def _count_archive(path: Path) -> int | None:
    """How many tweets an archive holds, counted without decoding it."""
    if _archive_account(path) is None:
        return None
    start, n, tail = re.compile(rb'\{\s*"tweet"\s*:\s*\{'), 0, b""  # quotes inside strings are escaped
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            data = tail + chunk
            n += len(start.findall(data))
            last = data.rfind(b"{")
            tail = data[last:] if last >= len(data) - 64 else b""
            n -= len(start.findall(tail))
    return n


def _archive_rows(path: Path, account: dict) -> Iterator[dict]:
    # long tweets: the tweet holds the start of the text, and note-tweet the whole of it, posted the same second
    notes: dict[str, list[str]] = {}
    for key, value in _stream(path):
        if key == "note-tweet" and isinstance(value, dict):
            note = value.get("noteTweet") or {}
            text = (note.get("core") or {}).get("text")
            if text and note.get("createdAt"):
                notes.setdefault(note["createdAt"][:19].replace("T", " "), []).append(text)
    username, name = account.get("username"), account.get("accountDisplayName")
    for key, value in _stream(path):
        if key not in ARCHIVE_TWEETS or not isinstance(value, dict):
            continue
        t = value.get("tweet") or {}
        text = t.get("full_text") or t.get("text") or ""
        created = _date(t.get("created_at"))
        if created in notes:
            mentions = (MENTIONS.match(text) or [""])[0]
            start = text[len(mentions):][:20]
            for note in notes[created]:
                if note.startswith(start) and len(note) > len(text) - len(mentions):
                    text = mentions + note
                    break
        yield {"id": str(t.get("id_str") or t.get("id") or ""), "text": text, "created_at": t.get("created_at"),
               "url": None, "author_username": username, "author_name": name, "retweeted_text": None}


# -- both -----------------------------------------------------------------------------------------

def count_tweets(path: Path) -> tuple[str, int] | None:
    """("database" or "archive", how many tweets it holds), or None if it's neither."""
    if path.suffix.lower() == ".json":
        n = _count_archive(path)
        return ("archive", n) if n is not None else None
    n = _count_db(path)
    return ("database", n) if n is not None else None


def _tweet(row: dict) -> dict:
    """A row in twitterapi.io's shape, as the twitter lane renders it."""
    tweet = {
        "id": row["id"],
        "text": row["text"],
        "createdAt": _date(row["created_at"]),
        "url": row["url"],
        "author": {"userName": row["author_username"], "name": row["author_name"]},
    }
    if row["retweeted_text"]:
        m = RETWEET.match(row["text"] or "")
        tweet["retweeted_tweet"] = {"text": row["retweeted_text"], "author": {"userName": m.group(1) if m else None}}
    return tweet


def import_files(store: Store, paths: list[Path]) -> None:
    files = find_files(paths)
    log.info("%d database and JSON files found", len(files))
    # tweets in the manifest: those waiting or failed get the file's text, the rest are left alone
    waiting, settled = set(), set()
    for key, status in store.db.execute("SELECT doc_key, status FROM urls WHERE doc_key LIKE 'tweet:%'"):
        (waiting if status in ("pending", "failed") else settled).add(key)
    waiting -= settled
    outcomes: Counter = Counter()
    try:
        for path in files:
            if path.suffix.lower() == ".json":
                _import_archive(store, path, waiting, settled, outcomes)
            else:
                _import_db(store, path, waiting, settled, outcomes)
    finally:
        store.commit()
        for outcome, n in outcomes.most_common():
            print(f"{n:>7}  {outcome}")


def _import_db(store: Store, path: Path, waiting: set[str], settled: set[str], outcomes: Counter) -> None:
    try:
        db = _open(path)
        cols = _columns(db)
    except sqlite3.Error as e:
        log.warning("%s: can't open it: %s", path, e)
        return
    try:
        if cols is None:
            log.info("%s: no tweets table, skipped", path)
            return
        n = _import_rows(store, _db_rows(db, cols), waiting, settled, outcomes)
        log.info("%s: %d tweets read", path, n)
    except sqlite3.Error as e:
        log.warning("%s: stopped reading it: %s", path, e)
    finally:
        db.close()


def _import_archive(store: Store, path: Path, waiting: set[str], settled: set[str], outcomes: Counter) -> None:
    account = _archive_account(path)
    if account is None:
        log.info("%s: not a Community Archive upload, skipped", path)
        return
    try:
        n = _import_rows(store, _archive_rows(path, account), waiting, settled, outcomes)
        log.info("%s: %d tweets read", path, n)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        log.warning("%s: stopped reading it: %s", path, e)


def _import_rows(store: Store, rows: Iterable[dict], waiting: set[str], settled: set[str], outcomes: Counter) -> int:
    n, rows = 0, iter(rows)
    while batch := list(islice(rows, BATCH)):
        n += len(batch)
        _import_batch(store, [row for row in batch if str(row["id"]).isdigit()], waiting, settled, outcomes)
    return n


def _import_batch(store: Store, rows: list[dict], waiting: set[str], settled: set[str], outcomes: Counter) -> None:
    new = []
    for row in rows:
        key = f"tweet:{row['id']}"
        row["url"] = row["url"] or f"https://x.com/{row['author_username'] or 'i/web'}/status/{row['id']}"
        if key not in waiting and key not in settled:
            r = route(row["url"])
            if r is None or r.doc_key != key:  # an address that isn't this tweet's: use the plain one
                r = route(f"https://x.com/i/web/status/{row['id']}")
            new.append(r)
    # into the manifest first, all at once, so each document below has its rows to record against
    store.add(new)
    waiting.update(r.doc_key for r in new)
    for row in rows:
        key = f"tweet:{row['id']}"
        if key in settled:
            outcomes["already saved"] += 1
            continue
        meta, body = _tweet_markdown(_tweet(row))
        meta["source"] = "tweet:import"
        chars = store.write_doc(key, meta, body)
        store.record(key, "ok", chars=chars, final_url=meta["url"], http_status=200)
        waiting.discard(key)
        settled.add(key)
        outcomes["imported"] += 1
