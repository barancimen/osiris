"""Full-text search over the archive: a SQLite FTS5 index in search.sqlite.

Like the garbage check, it's kept up to date incrementally: a document is read again only when its
file changed, so refreshing after a run costs a stat per document plus whatever is new. Results are
ranked with BM25, with a match in the title worth more than one in the URL, and that more than one in
the text.

The index is contentless, so it holds no second copy of your text for display. A contentless FTS5
table can only forget a document when it's told exactly what was indexed, so each document's
indexed text is also kept zstd-compressed, at about a quarter of its size. Snippets are cut from the
markdown files themselves.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .store import md_path, read_doc_file

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    rowid   INTEGER PRIMARY KEY,
    doc_id  TEXT NOT NULL UNIQUE,
    url     TEXT,     -- the document's first URL, as in the manifest
    lane    TEXT, status TEXT, host TEXT, chars INTEGER,
    title   TEXT,     -- as indexed
    url_text TEXT,    -- as indexed: the URL made readable, for matching parts of it
    body_z  BLOB,     -- as indexed, zstd: needed to take the document out again
    mtime   REAL, size INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    title, url_text, body, content='', tokenize='unicode61 remove_diacritics 2'
);
"""
WEIGHTS = (10.0, 4.0, 1.0)  # title, URL, text
COMMIT_EVERY = 5.0
OPTIMIZE_AFTER = 2000  # documents changed in one refresh before the index is merged into one b-tree
SNIPPET_BEFORE, SNIPPET_AFTER = 90, 210
PAGE = 50
LOCK_FILE = "search.lock"

# a phrase in quotes, or a bare word; either may have a leading - to exclude it
QUERY_PART = re.compile(r'(-?)"([^"]*)"?|(-?)(\S+)')
WORD = re.compile(r"\w+")


def connect(out: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(out) / "search.sqlite"
    if readonly:
        return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=10)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def url_text(url: str) -> str:
    """Words to match a URL by: its host and path, with file names readable."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    return " ".join(filter(None, (host, unquote(parts.path), unquote(parts.query))))


# -- keeping it up to date ------------------------------------------------------------------------


def _manifest(out: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(out / 'manifest.sqlite'))}?mode=ro", uri=True, timeout=10)


@contextmanager
def _one_at_a_time(out: Path) -> Iterator[None]:
    """Two refreshes at once could both take the same document out of the index, which corrupts a
    contentless one, so the web page's and the command line's take turns. The lock is a file of its
    own: closing another handle on the database file would drop SQLite's locks on it."""
    with open(out / LOCK_FILE, "a") as lock:
        try:
            import fcntl
        except ImportError:  # Windows: one process at a time is up to you
            yield
            return
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def refresh(out: str | Path, progress: Callable[[int, int], None] | None = None,
            stop: Callable[[], bool] | None = None) -> dict[str, int]:
    """Index new and changed documents and forget ones that are gone. `stop` ends it early,
    keeping what's done; the next refresh carries on from there."""
    out = Path(out)
    if not (out / "manifest.sqlite").exists():
        return {"documents": 0, "indexed": 0, "removed": 0}
    with _one_at_a_time(out):
        return _refresh(out, progress, stop)


def _refresh(out: Path, progress: Callable[[int, int], None] | None, stop: Callable[[], bool] | None) -> dict[str, int]:
    import zstandard

    manifest = _manifest(out)
    try:
        rows = manifest.execute(
            "SELECT doc_id, MIN(url), lane, status, MAX(chars) FROM urls"
            " WHERE status IN ('ok', 'thin') AND doc_id IS NOT NULL GROUP BY doc_id"
        ).fetchall()
    finally:
        manifest.close()
    db = connect(out)
    compress, decompress = zstandard.ZstdCompressor(level=3), zstandard.ZstdDecompressor()

    def forget(rowid: int, title: str, url_words: str, body_z: bytes) -> None:
        db.execute("INSERT INTO fts (fts, rowid, title, url_text, body) VALUES ('delete', ?, ?, ?, ?)",
                   (rowid, title, url_words, decompress.decompress(body_z).decode("utf-8")))

    known = {did: (rowid, mtime, size) for rowid, did, mtime, size in
             db.execute("SELECT rowid, doc_id, mtime, size FROM docs")}
    current: set[str] = set()
    indexed = 0
    last = time.monotonic()
    try:
        for i, (did, url, lane, status, chars) in enumerate(rows, 1):
            if stop and stop():
                break
            if progress and i % 200 == 0:
                progress(i, len(rows))
            path = md_path(out, did)
            try:
                stat = path.stat()
            except OSError:
                continue
            current.add(did)
            old = known.get(did)
            if old and old[1:] == (stat.st_mtime, stat.st_size):
                db.execute("UPDATE docs SET url = ?, lane = ?, status = ?, chars = ? WHERE rowid = ?"
                           " AND (url IS NOT ? OR lane IS NOT ? OR status IS NOT ? OR chars IS NOT ?)",
                           (url, lane, status, chars, old[0], url, lane, status, chars))
                continue
            doc = read_doc_file(path)
            if doc is None:
                current.discard(did)
                continue
            meta, body = doc
            if old:
                forget(*db.execute("SELECT rowid, title, url_text, body_z FROM docs WHERE rowid = ?",
                                   (old[0],)).fetchone())
            title, words = meta.get("title") or "", url_text(meta.get("url") or url)
            if old:
                db.execute("UPDATE docs SET url = ?, lane = ?, status = ?, host = ?, chars = ?, title = ?, url_text = ?,"
                           " body_z = ?, mtime = ?, size = ? WHERE rowid = ?",
                           (url, lane, status, urlsplit(url).hostname or "local file", chars, title, words,
                            compress.compress(body.encode("utf-8")), stat.st_mtime, stat.st_size, old[0]))
                rowid = old[0]
            else:
                rowid = db.execute(
                    "INSERT INTO docs (doc_id, url, lane, status, host, chars, title, url_text, body_z, mtime, size)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (did, url, lane, status, urlsplit(url).hostname or "local file", chars, title, words,
                     compress.compress(body.encode("utf-8")), stat.st_mtime, stat.st_size)).lastrowid
            db.execute("INSERT INTO fts (rowid, title, url_text, body) VALUES (?, ?, ?, ?)", (rowid, title, words, body))
            indexed += 1
            if time.monotonic() - last > COMMIT_EVERY:
                db.commit()
                last = time.monotonic()
        removed = 0
        if not (stop and stop()):  # only a full pass knows what's gone
            for did in set(known) - current:
                row = db.execute("SELECT rowid, title, url_text, body_z FROM docs WHERE doc_id = ?", (did,)).fetchone()
                forget(*row)
                db.execute("DELETE FROM docs WHERE rowid = ?", (row[0],))
                removed += 1
        if indexed + removed >= OPTIMIZE_AFTER:
            db.commit()
            db.execute("INSERT INTO fts (fts) VALUES ('optimize')")
        db.commit()
        total = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    finally:
        db.close()
    if progress:
        progress(len(rows), len(rows))
    return {"documents": total, "indexed": indexed, "removed": removed}


# -- searching ------------------------------------------------------------------------------------

def parse(text: str) -> tuple[str | None, list[tuple[str, bool]]]:
    """A forgiving FTS5 query from what someone typed: every word must appear (a word of three or
    more letters also matches longer words starting with it), "quoted words" must appear together,
    and -word or -"some words" must not appear as such. Returns (MATCH expression or None, words to highlight
    with whether each is a prefix)."""
    wanted, unwanted, terms = [], [], []
    for m in QUERY_PART.finditer(text):
        negative = bool(m.group(1) or m.group(3))
        phrase = m.group(2) is not None
        words = WORD.findall(m.group(2) if phrase else m.group(4))
        if not words:
            continue
        if phrase or len(words) > 1:  # a-b, foo.com/bar: its parts in that order
            part = '"' + " ".join(words) + '"'
            prefix = False
        else:
            prefix = len(words[0]) >= 3 and not negative
            part = f'"{words[0]}"' + ("*" if prefix else "")
        if negative:
            unwanted.append(part)
        else:
            wanted.append(part)
            terms += [(w, prefix and len(words) == 1) for w in words]
    if not wanted:
        return None, []
    return " AND ".join(wanted) + "".join(f" NOT {p}" for p in unwanted), terms


def term_pattern(terms: list[tuple[str, bool]]) -> re.Pattern | None:
    if not terms:
        return None
    parts = sorted({re.escape(w) + (r"\w*" if prefix else r"(?!\w)") for w, prefix in terms}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")", re.I)


def snippet(body: str, pattern: re.Pattern | None) -> list[tuple[str, bool]]:
    """A short passage around the first match, as (text, is a match) pieces."""
    m = pattern.search(body) if pattern else None
    at = m.start() if m else 0
    start = max(at - SNIPPET_BEFORE, 0)
    end = min(at + SNIPPET_AFTER, len(body))
    if start:  # whole words at both ends
        space = body.find(" ", start, at)
        start = space + 1 if space >= 0 else start
    if end < len(body):
        space = body.rfind(" ", at, end)
        end = space if space > at else end
    text = ("…" if start else "") + " ".join(body[start:end].split()) + ("…" if end < len(body) else "")
    pieces, pos = [], 0
    for x in (pattern.finditer(text) if pattern else ()):
        if x.start() > pos:
            pieces.append((text[pos:x.start()], False))
        pieces.append((x.group(), True))
        pos = x.end()
    if pos < len(text):
        pieces.append((text[pos:], False))
    return pieces


def search(out: str | Path, text: str, lane: str = "", status: str = "", limit: int = PAGE,
           offset: int = 0, garbage_first: bool = False, decision: tuple[str, list] | None = None) -> dict:
    """Matches with a snippet each: {"items", "total", "terms"}, best first, or most likely garbage first.
    `decision` is a condition on the garbage check's `d.decision` with its parameters."""
    out = Path(out)
    expr, terms = parse(text)
    if expr is None:
        return {"items": [], "total": 0, "terms": []}
    where, params = ["fts MATCH ?"], [expr]
    for column, value in (("lane", lane), ("status", status)):
        if value:
            where.append(f"d.{column} = ?")
            params.append(value)
    if decision:
        where.append(decision[0].replace("d.decision", "dec.decision").replace("d.doc_id", "dec.doc_id"))
        params += decision[1]
    clause = " AND ".join(where)
    joins = " LEFT JOIN r.scores s ON s.doc_id = d.doc_id LEFT JOIN r.decisions dec ON dec.doc_id = d.doc_id"
    # worst first for Review; otherwise documents waiting for you (a version to choose, a fix to
    # check) come first, then the best matches
    order = "COALESCE(s.score, -1) DESC, d.doc_id" if garbage_first else \
        f"(dec.decision IN ('choose', 'fixed')) DESC, bm25(fts, {', '.join(map(str, WEIGHTS))})"
    db = connect(out, readonly=True)
    try:
        db.execute("ATTACH DATABASE ? AS r", (f"file:{quote(str(out / 'review.sqlite'))}?mode=ro",))
        total = db.execute(f"SELECT COUNT(*) FROM fts JOIN docs d ON d.rowid = fts.rowid{joins} WHERE {clause}",
                           params).fetchone()[0]
        rows = db.execute(
            f"SELECT d.doc_id, d.url, d.lane, d.status, d.host, d.title, d.chars FROM fts JOIN docs d ON d.rowid = fts.rowid"
            f"{joins} WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
    finally:
        db.close()
    pattern = term_pattern(terms)
    items = []
    for did, url, lane_, status_, host, title, chars in rows:
        doc = read_doc_file(md_path(out, did))
        items.append({"id": did, "url": url, "lane": lane_, "status": status_, "host": host, "title": title or None,
                      "chars": chars, "snippet": snippet(doc[1], pattern) if doc else None})
    return {"items": items, "total": total, "terms": [{"word": w, "prefix": p} for w, p in terms]}
