"""Tweets from files you already have:
- SQLite databases written by osiris/deprecated/fetch_tweets.py: a `tweets` table in twitterapi.io's fields
- Community Archive uploads: Twitter's account archive as one JSON file, with `account` and `tweets`
  at the top, and the full text of long tweets under `note-tweet`
- Twitter's own export, as the .zip it comes in: the same tweets, replies and retweets, read inside
  the zip without unpacking it. Its likes, messages and deleted tweets are never read, and it holds
  no bookmarks
- Bookmarks saved with the twitter-web-exporter browser extension, as .json or .html, each with the
  tweet it quotes

Each tweet becomes a document like the twitter lane's, keyed by its ID, so a username that changed
later still finds the same document. A tweet already saved, or deleted on purpose, is left alone.
One that's known but still waiting or failed (such as not_in_scry) gets the file's text.
Files are read as a stream, a tweet at a time: they can be far bigger than this machine's memory.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path
from typing import TextIO

from .store import Store
from .twitter import Saver

log = logging.getLogger(__name__)

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
HTML_SUFFIXES = {".html", ".htm"}
SUFFIXES = DB_SUFFIXES | HTML_SUFFIXES | {".json", ".zip"}
KINDS = {"database": "database", "archive": "archive", "export": "Twitter export", "bookmarks": "bookmarks file"}  # as the page names them
REQUIRED = {"id", "text", "created_at", "author_username"}
OPTIONAL = ("url", "author_name", "retweeted_text")
RETWEET = re.compile(r"^RT @(\w{1,15}):")
ARCHIVE_TWEETS = ("tweets", "community-tweet")
NOTES = "note-tweet"
MENTIONS = re.compile(r"^(?:@\w{1,15}\s+)+")  # a reply's leading @names, which a long tweet's full text leaves out
BATCH = 5000  # tweets held in memory at once
CHUNK = 1 << 20
DATES = ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S %z")  # Twitter's own, and the bookmark exporter's
GAP = re.compile(r"[\s,:]*")
ASSIGNED = re.compile(r"\s*(?:[\w.$]+\s*=\s*)?")  # what may come before an array: `window.YTD.tweets.part0 = `
EXPORT_MANIFEST = "data/manifest.js"
EXPORT_MANIFEST_BYTES = 16 << 20
# the export's data types that are the account's own tweets, and the key each has in a Community Archive upload
EXPORT_TYPES = {"tweets": "tweets", "tweet": "tweets", "communityTweet": "community-tweet", "noteTweet": NOTES}
# the bookmark exporter's table headings, and the field each has in its JSON
BOOKMARK_COLUMNS = {"ID": "id", "Date": "created_at", "Content": "full_text", "Screen Name": "screen_name",
                    "Profile Name": "name", "metadata": "metadata"}
HTML_HEAD = 1 << 16  # a bookmarks table starts within this much of the page
TABLE_ROW = re.compile(rb"<tr[\s>]", re.I)
READ_ERRORS = (OSError, ValueError, sqlite3.Error, RuntimeError, EOFError, NotImplementedError, zipfile.BadZipFile, zlib.error)


def find_files(paths: list[Path]) -> list[Path]:
    """The files given, and the database, JSON, HTML and zip files in the folders given, searched recursively."""
    found = set()
    for p in paths:
        for f in [p] if p.is_file() else p.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUFFIXES and not any(part.startswith(".") for part in f.parts[len(p.parts):]):
                found.add(f)
    return sorted(found)


def _date(created_at: str | None) -> str | None:
    """Twitter's 'Wed Sep 16 22:18:18 +0000 2026' as '2026-09-16 22:18:18' (UTC), the form Scry's tweets have."""
    for form in DATES:
        try:
            return datetime.strptime(created_at or "", form).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return created_at


def _dig(value, *keys):
    """value[key][key]..., or None where a level is missing or isn't an object."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


# -- JSON, a value at a time -----------------------------------------------------------------------

class _Json:
    def __init__(self, f: TextIO):
        self.f, self.buf, self.pos, self.eof = f, "", 0, False
        self.decoder = json.JSONDecoder()

    def fill(self) -> bool:
        more = self.f.read(CHUNK)
        if not more:
            self.eof = True
            return False
        self.buf, self.pos = self.buf[self.pos:] + more, 0
        return True

    def peek(self) -> str:
        while True:
            self.pos = GAP.match(self.buf, self.pos).end()
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self.fill():
                raise ValueError("the file ends too soon")

    def decode(self):
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if self.fill():
                    continue
                raise
            if end == len(self.buf) and not self.eof and self.fill():  # a number may go on in the next chunk
                continue
            self.pos = end
            return value


def _stream(f: TextIO) -> Iterator[tuple[str, object]]:
    """(key, value) for each value at the top of a JSON object, and (key, element) for each element
    of an array there, decoding one at a time. ValueError if the file isn't a JSON object."""
    text = _Json(f)
    if text.peek() != "{":
        raise ValueError("not a JSON object")
    text.pos += 1
    while text.peek() != "}":
        key = text.decode()
        if not isinstance(key, str):
            raise ValueError("not a JSON object")
        if text.peek() == "[":
            text.pos += 1
            while text.peek() != "]":
                yield key, text.decode()
            text.pos += 1
        else:
            yield key, text.decode()


def _items(f: TextIO) -> Iterator[object]:
    """Each element of a JSON array, decoding one at a time. The array may be assigned to a name, as
    in the export's `window.YTD.tweets.part0 = [`. ValueError if the file isn't such an array."""
    text = _Json(f)
    text.fill()
    start = text.buf.find("[")
    if start < 0 or not ASSIGNED.fullmatch(text.buf, 0, start):
        raise ValueError("not a JSON array")
    text.pos = start + 1
    while text.peek() != "]":
        yield text.decode()


# -- fetch_tweets.py's databases ------------------------------------------------------------------

def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _db_columns(path: Path) -> set[str] | None:
    """The tweets table's columns, or None if this isn't a tweet database."""
    try:
        db = _open(path)
    except sqlite3.Error:
        return None
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(tweets)")}
    except sqlite3.DatabaseError:
        return None
    finally:
        db.close()
    return cols if REQUIRED <= cols else None


def _count_db(path: Path) -> int:
    db = _open(path)
    try:
        return db.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
    finally:
        db.close()


def _db_rows(path: Path, cols: set[str]) -> Iterator[dict]:
    fields = ", ".join(["CAST(id AS TEXT) AS id", "text", "created_at", "author_username",
                        *(c if c in cols else f"NULL AS {c}" for c in OPTIONAL)])
    db = _open(path)
    try:
        cursor = db.execute(f"SELECT {fields} FROM tweets ORDER BY CAST(id AS INTEGER)")
        names = [d[0] for d in cursor.description]
        for values in cursor:
            yield dict(zip(names, values))
    finally:
        db.close()


# -- Community Archive uploads, and Twitter's export -----------------------------------------------

def _archive_account(path: Path) -> dict | None:
    """The archive's account, read from its start, or None if this isn't a Community Archive upload."""
    account = None
    try:
        with open(path, encoding="utf-8") as f:
            for key, value in _stream(f):
                if key == "account" and isinstance(value, dict):
                    account = value.get("account") or value
                elif key in ARCHIVE_TWEETS:
                    return account
    except (OSError, ValueError):
        return None
    return None


def _archive_pairs(path: Path, wanted: tuple[str, ...]) -> Iterator[tuple[str, object]]:
    with open(path, encoding="utf-8") as f:
        for key, value in _stream(f):
            if key in wanted:
                yield key, value


def _count_archive(path: Path) -> int:
    """How many tweets an archive holds, counted without decoding it."""
    start, n, tail = re.compile(rb'\{\s*"tweet"\s*:\s*\{'), 0, b""  # quotes inside strings are escaped
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            data = tail + chunk
            n += len(start.findall(data))
            last = data.rfind(b"{")
            tail = data[last:] if last >= len(data) - 64 else b""
            n -= len(start.findall(tail))
    return n


def _export(path: Path) -> dict | None:
    """What one of Twitter's exports holds: the account, how many tweets, and the files inside the zip
    that have them, as {key in a Community Archive upload: [file]}. None if the zip isn't an export."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return None
    with zf:
        members = set(zf.namelist())
        named = [m for m in members if m == EXPORT_MANIFEST or m.endswith("/" + EXPORT_MANIFEST)]
        if not named:
            return None
        manifest = min(named, key=len)
        if zf.getinfo(manifest).file_size > EXPORT_MANIFEST_BYTES:
            return None
        try:
            config = json.loads(zf.read(manifest).decode("utf-8").partition("=")[2])
        except ValueError:
            return None
    folder, types = manifest[: -len(EXPORT_MANIFEST)], _dig(config, "dataTypes")
    if not isinstance(types, dict):
        return None
    files: dict[str, list[str]] = {}
    tweets = 0
    for data_type, key in EXPORT_TYPES.items():
        for entry in _dig(types, data_type, "files") or []:
            if folder + str(_dig(entry, "fileName")) in members:
                files.setdefault(key, []).append(folder + entry["fileName"])
                if key != NOTES:
                    tweets += int(str(entry.get("count") or 0)) if str(entry.get("count") or 0).isdigit() else 0
    return {"username": _dig(config, "userInfo", "userName"), "name": _dig(config, "userInfo", "displayName"),
            "files": files, "tweets": tweets,
            "bookmarks": any("bookmark" in name.lower() and _dig(found, "files") for name, found in types.items())}


def _export_pairs(path: Path, files: dict[str, list[str]], wanted: tuple[str, ...]) -> Iterator[tuple[str, object]]:
    with zipfile.ZipFile(path) as zf:
        for key in wanted:
            for member in files.get(key, ()):
                with io.TextIOWrapper(zf.open(member), encoding="utf-8") as f:
                    for value in _items(f):
                        yield key, value


def _archive_rows(pairs: Callable[[tuple[str, ...]], Iterable[tuple[str, object]]],
                  username: str | None, name: str | None) -> Iterator[dict]:
    """The account's tweets, from `pairs(keys)`: the (key, value) under those keys of an archive."""
    # long tweets: the tweet holds the start of the text, and note-tweet the whole of it, posted the same second
    notes: dict[str, list[str]] = {}
    for _, value in pairs((NOTES,)):
        text, created = _dig(value, "noteTweet", "core", "text"), _dig(value, "noteTweet", "createdAt")
        if isinstance(text, str) and isinstance(created, str) and text:
            notes.setdefault(created[:19].replace("T", " "), []).append(text)
    for _, value in pairs(ARCHIVE_TWEETS):
        t = _dig(value, "tweet")
        if not isinstance(t, dict):
            continue
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


# -- bookmarks from twitter-web-exporter ------------------------------------------------------------

def _inner(tweet) -> dict:
    """A tweet as X's API nests it: sometimes inside a wrapper saying who may see it."""
    tweet = _dig(tweet, "tweet") or tweet
    return tweet if isinstance(tweet, dict) else {}


def _whole_text(tweet: dict) -> str:
    return _dig(tweet, "note_tweet", "note_tweet_results", "result", "text") or _dig(tweet, "legacy", "full_text") or ""


def _user(tweet: dict, field: str) -> str | None:
    user = _dig(tweet, "core", "user_results", "result")
    return _dig(user, "core", field) or _dig(user, "legacy", field)


def _bookmark(item) -> dict | None:
    """A row from one bookmark as the exporter writes it: its own columns, and under `metadata`
    the tweet as X's API gave it, which has the quoted tweet and the time in UTC."""
    if not isinstance(item, dict):
        return None
    tweet = _inner(item.get("metadata"))
    quoted, retweeted = _inner(_dig(tweet, "quoted_status_result", "result")), _inner(_dig(tweet, "legacy", "retweeted_status_result", "result"))
    return {"id": str(item.get("id") or tweet.get("rest_id") or ""),
            "text": max(str(item.get("full_text") or ""), _whole_text(tweet), key=len),
            "created_at": _dig(tweet, "legacy", "created_at") or item.get("created_at"), "url": None,
            "author_username": item.get("screen_name") or _user(tweet, "screen_name"),
            "author_name": item.get("name") or _user(tweet, "name"),
            "retweeted_text": _whole_text(retweeted) or None, "retweeted_username": _user(retweeted, "screen_name"),
            "quoted_text": _whole_text(quoted) or None, "quoted_username": _user(quoted, "screen_name")}


def _is_bookmark(item) -> bool:
    return isinstance(item, dict) and (("id" in item and "screen_name" in item) or bool(_dig(item, "metadata", "rest_id")))


def _json_bookmarks(path: Path) -> Iterator[object]:
    with open(path, encoding="utf-8-sig") as f:
        yield from _items(f)


def _is_json_bookmarks(path: Path) -> bool:
    try:
        return _is_bookmark(next(_json_bookmarks(path), None))
    except (OSError, ValueError):
        return False


class _Table(HTMLParser):
    """The rows of a page's tables as it's fed, each (whether it's a heading row, the text of its cells)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[bool, list[str]]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.heading = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row, self.cell, self.heading = [], None, False
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            self.heading = self.heading or tag == "th"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append((self.heading, self.row))
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def take(self) -> list[tuple[bool, list[str]]]:
        rows, self.rows = self.rows, []
        return rows


def _html_rows(path: Path, most: int | None = None) -> Iterator[tuple[bool, list[str]]]:
    parser, read = _Table(), 0
    with open(path, encoding="utf-8", errors="replace") as f:
        while (most is None or read < most) and (chunk := f.read(CHUNK if most is None else most)):
            read += len(chunk)
            parser.feed(chunk)
            yield from parser.take()


def _html_columns(path: Path) -> list[str | None] | None:
    """The bookmark field in each column of the page's table, or None if the page isn't a bookmarks export."""
    try:
        for heading, cells in _html_rows(path, HTML_HEAD):
            columns = [BOOKMARK_COLUMNS.get(cell.strip()) for cell in cells]
            return columns if heading and "id" in columns and ("screen_name" in columns or "metadata" in columns) else None
    except OSError:
        pass
    return None


def _html_bookmarks(path: Path, columns: list[str | None]) -> Iterator[dict]:
    for heading, cells in _html_rows(path):
        if heading:
            continue
        item: dict = {field: cell for field, cell in zip(columns, cells) if field}
        try:  # the cell holds a label to click, then the tweet as JSON
            item["metadata"] = json.loads(item["metadata"][item["metadata"].index("{"):])
        except (KeyError, ValueError):
            item.pop("metadata", None)
        yield item


def _count_html(path: Path) -> int:
    """How many rows the table has under its headings, counted without reading them."""
    n, tail = 0, b""
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            data = tail + chunk
            n += len(TABLE_ROW.findall(data))
            tail = data[-3:]  # too short to hold a row's start by itself, so none is counted twice
    return max(n - 1, 0)


# -- all of them ----------------------------------------------------------------------------------

def _read(path: Path) -> tuple[str, Callable[[], int], Iterator[dict]] | None:
    """(what kind of file it is, how to count its tweets, its tweets), or None if it's none of them."""
    suffix = path.suffix.lower()
    if suffix in DB_SUFFIXES:
        cols = _db_columns(path)
        return ("database", lambda: _count_db(path), _db_rows(path, cols)) if cols else None
    if suffix == ".zip":
        export = _export(path)
        if export is None:
            return None
        if export["bookmarks"]:
            log.warning("%s: this export has bookmarks, which osiris doesn't know how to read yet", path)
        return ("export", lambda: export["tweets"],
                _archive_rows(lambda wanted: _export_pairs(path, export["files"], wanted), export["username"], export["name"]))
    if suffix in HTML_SUFFIXES:
        columns = _html_columns(path)
        return ("bookmarks", lambda: _count_html(path),
                filter(None, map(_bookmark, _html_bookmarks(path, columns)))) if columns else None
    account = _archive_account(path)
    if account is not None:
        return ("archive", lambda: _count_archive(path), _archive_rows(lambda wanted: _archive_pairs(path, wanted),
                                                                       account.get("username"), account.get("accountDisplayName")))
    if _is_json_bookmarks(path):
        return ("bookmarks", lambda: sum(1 for _ in _json_bookmarks(path)), filter(None, map(_bookmark, _json_bookmarks(path))))
    return None


def count_tweets(path: Path) -> tuple[str, int] | None:
    """(the kind of file, as in KINDS, and how many tweets it holds), or None if it's none of them."""
    try:
        found = _read(path)
        return (found[0], found[1]()) if found else None
    except READ_ERRORS:
        return None


def _tweet(row: dict) -> dict:
    """A row in twitterapi.io's shape, as the twitter lane renders it."""
    tweet = {
        "id": row["id"],
        "text": row["text"],
        "createdAt": _date(row["created_at"]),
        "url": row["url"] or f"https://x.com/{row['author_username'] or 'i/web'}/status/{row['id']}",
        "author": {"userName": row["author_username"], "name": row["author_name"]},
    }
    if row["retweeted_text"]:
        m = RETWEET.match(row["text"] or "")
        tweet["retweeted_tweet"] = {"text": row["retweeted_text"],
                                    "author": {"userName": row.get("retweeted_username") or (m.group(1) if m else None)}}
    if row.get("quoted_text"):
        tweet["quoted_tweet"] = {"text": row["quoted_text"], "author": {"userName": row.get("quoted_username")}}
    return tweet


def import_files(store: Store, paths: list[Path]) -> None:
    files = find_files(paths)
    log.info("%d database, JSON, HTML and zip files found", len(files))
    saver = Saver(store)
    outcomes: Counter = Counter()
    try:
        for path in files:
            try:
                found = _read(path)
                if found is None:
                    log.info("%s: no tweets in a form osiris reads, skipped", path)
                    continue
                log.info("%s: %d tweets read", path, _import_rows(saver, found[2], outcomes))
            except READ_ERRORS as e:
                log.warning("%s: stopped reading it: %s", path, e)
    finally:
        store.commit()
        for outcome, n in outcomes.most_common():
            print(f"{n:>7}  {outcome}")


def _import_rows(saver: Saver, rows: Iterable[dict], outcomes: Counter) -> int:
    n, rows = 0, iter(rows)
    while batch := list(islice(rows, BATCH)):
        n += len(batch)
        tweets = [_tweet(row) for row in batch if str(row["id"]).isdigit()]
        written = saver.save(tweets, "tweet:import")
        outcomes["imported"] += written
        outcomes["already saved"] += len(tweets) - written
    return n
