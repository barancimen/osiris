"""Tweets from SQLite databases written by /root/twitterapi/fetch_tweets.py: one `tweets` table
per account, its rows in twitterapi.io's fields. Each tweet becomes a document like the twitter
lane's, keyed by its ID, so a username that changed later still finds the same document.

A tweet already saved, or deleted on purpose, is left alone. One that's known but still
waiting or failed (such as not_in_scry) gets the database's text.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .route import route
from .store import Store
from .twitter import _tweet_markdown

log = logging.getLogger(__name__)

SUFFIXES = {".db", ".sqlite", ".sqlite3"}
REQUIRED = {"id", "text", "created_at", "author_username"}
OPTIONAL = ("url", "author_name", "retweeted_text")
RETWEET = re.compile(r"^RT @(\w{1,15}):")


def find_databases(paths: list[Path]) -> list[Path]:
    """The database files given, and those in the folders given, searched recursively."""
    found = set()
    for p in paths:
        for f in [p] if p.is_file() else p.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUFFIXES and not any(part.startswith(".") for part in f.parts[len(p.parts):]):
                found.add(f)
    return sorted(found)


def _columns(db: sqlite3.Connection) -> set[str] | None:
    """The tweets table's columns, or None if this isn't a tweet database."""
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(tweets)")}
    except sqlite3.DatabaseError:
        return None
    return cols if REQUIRED <= cols else None


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def count_tweets(path: Path) -> int | None:
    """How many tweets a database holds, or None if it isn't a tweet database."""
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


def _date(created_at: str | None) -> str | None:
    """Twitter's 'Wed Sep 16 22:18:18 +0000 2026' as '2026-09-16 22:18:18' (UTC), the form Scry's tweets have."""
    try:
        return datetime.strptime(created_at or "", "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return created_at


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


def _rows(db: sqlite3.Connection, cols: set[str]):
    fields = ", ".join(["CAST(id AS TEXT) AS id", "text", "created_at", "author_username",
                        *(c if c in cols else f"NULL AS {c}" for c in OPTIONAL)])
    cursor = db.execute(f"SELECT {fields} FROM tweets ORDER BY CAST(id AS INTEGER)")
    names = [d[0] for d in cursor.description]
    for values in cursor:
        yield dict(zip(names, values))


def import_databases(store: Store, paths: list[Path]) -> None:
    files = find_databases(paths)
    log.info("%d database files found", len(files))
    # tweets in the manifest: those waiting or failed get the database's text, the rest are left alone
    waiting, settled = set(), set()
    for key, status in store.db.execute("SELECT doc_key, status FROM urls WHERE doc_key LIKE 'tweet:%'"):
        (waiting if status in ("pending", "failed") else settled).add(key)
    waiting -= settled
    outcomes: Counter = Counter()
    try:
        for path in files:
            try:
                db = _open(path)
                cols = _columns(db)
            except sqlite3.Error as e:
                log.warning("%s: can't open it: %s", path, e)
                continue
            if cols is None:
                log.info("%s: no tweets table, skipped", path)
                db.close()
                continue
            try:
                n = _import_one(store, path, db, cols, waiting, settled, outcomes)
            except sqlite3.Error as e:
                log.warning("%s: stopped reading it: %s", path, e)
                continue
            finally:
                db.close()
            log.info("%s: %d tweets read", path, n)
    finally:
        store.commit()
        for outcome, n in outcomes.most_common():
            print(f"{n:>7}  {outcome}")


def _import_one(store: Store, path: Path, db: sqlite3.Connection, cols: set[str],
                waiting: set[str], settled: set[str], outcomes: Counter) -> int:
    rows = [row for row in _rows(db, cols) if str(row["id"]).isdigit()]
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
    return len(rows)
