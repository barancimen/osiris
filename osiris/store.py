"""SQLite manifest (one row per input URL) and the markdown document store."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlsplit

from . import clean
from .route import FORUM_LANES, Route, doc_id, safe_dedup_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    url          TEXT PRIMARY KEY,
    lane         TEXT NOT NULL,
    doc_key      TEXT,
    doc_id       TEXT,
    status       TEXT NOT NULL,  -- pending | ok | thin | failed | skipped
    reason       TEXT,
    http_status  INTEGER,
    final_url    TEXT,
    content_type TEXT,
    chars        INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT,
    dedup_key    TEXT,  -- osiris.dedup.dedup_key of the URL as it appeared in the input
    raw_path     TEXT,  -- relative to the output dir, only with --keep-raw
    tried        TEXT NOT NULL DEFAULT ''  -- fallback passes already run on it, e.g. ",impersonate,wayback"
);
CREATE INDEX IF NOT EXISTS urls_lane_status ON urls(lane, status);
CREATE INDEX IF NOT EXISTS urls_doc_key ON urls(doc_key);
CREATE TABLE IF NOT EXISTS imported_files (  -- saved pages `osiris import` has handled, by content
    sha1        TEXT PRIMARY KEY,
    name        TEXT,
    url         TEXT,
    imported_at TEXT
);
"""
MIGRATIONS = {"dedup_key": "ALTER TABLE urls ADD COLUMN dedup_key TEXT",
              "raw_path": "ALTER TABLE urls ADD COLUMN raw_path TEXT",
              "tried": "ALTER TABLE urls ADD COLUMN tried TEXT NOT NULL DEFAULT ''"}

FRONT_MATTER_KEYS = ("id", "url", "final_url", "source", "title", "author", "date", "site", "fetched_at")
DONE = ("ok", "thin")
TMP_DIR = ".osiris-tmp"
REFRESH = "refresh"  # in `tried`: this PDF is done in a refresh that hasn't finished


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def md_path(out: Path, did: str) -> Path:
    return out / "md" / did[:2] / f"{did}.md"


def read_doc_file(path: Path) -> tuple[dict, str] | None:
    """(front matter, body) of a stored document, or None if it isn't there."""
    try:
        return parse_doc(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def parse_doc(raw: str) -> tuple[dict, str]:
    """(front matter, body) of a stored document's text."""
    meta: dict = {}
    if raw.startswith("---\n"):
        header, _, body = raw[4:].partition("\n---\n")
        for line in header.splitlines():
            key, _, value = line.partition(": ")
            meta[key] = json.loads(value)
    else:
        body = raw
    return meta, body.strip()


class Store:
    def __init__(self, out_dir: str | os.PathLike):
        self.out = Path(out_dir)
        self.md_dir = self.out / "md"
        self.tmp_dir = self.out / TMP_DIR  # its own name: --out may be a folder with a tmp/ of yours
        self.md_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.out / "manifest.sqlite")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self._last_commit = time.monotonic()

    def _migrate(self) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(urls)")}
        for column, ddl in MIGRATIONS.items():
            if column not in columns:
                self.db.execute(ddl)
        missing = self.db.execute("SELECT url FROM urls WHERE dedup_key IS NULL").fetchall()
        self.db.executemany("UPDATE urls SET dedup_key = ? WHERE url = ?", ((safe_dedup_key(u), u) for (u,) in missing))
        self.db.execute("CREATE INDEX IF NOT EXISTS urls_dedup_key ON urls(dedup_key)")
        self.db.commit()

    # -- manifest -----------------------------------------------------------

    def add(self, routes: Iterable[Route]) -> tuple[int, int]:
        """Queue routes whose dedup key isn't in the manifest yet. Returns (added, already known)."""
        known = {key for (key,) in self.db.execute("SELECT dedup_key FROM urls")}
        rows, seen = [], 0
        for r in routes:
            if r.dedup_key in known:
                seen += 1
                continue
            known.add(r.dedup_key)
            status = "skipped" if r.lane == "skip" else "pending"
            rows.append((r.url, r.lane, r.doc_key, r.doc_id, status, r.reason, now_iso(), r.dedup_key))
        before = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO urls (url, lane, doc_key, doc_id, status, reason, updated_at, dedup_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.db.commit()
        added = self.db.total_changes - before
        return added, seen + len(rows) - added  # INSERT OR IGNORE: same normalized URL, different key

    def reroute(self, route_fn: Callable[[str], Route | None]) -> int:
        """Apply the current routing rules to URLs not yet archived: pending ones, and failed or
        thin ones whose lane changed (e.g. Reddit posts that failed in the generic lane before
        the Reddit lane existed), and GitHub links and local files skipped before their lane or
        format was supported.
        Finished documents and imported pages are never touched."""
        changed = []
        for url, lane, doc_key, reason, status in self.db.execute(
            "SELECT url, lane, doc_key, reason, status FROM urls"
            " WHERE (status IN ('pending', 'failed', 'thin') AND lane != 'singlefile')"
            " OR (status = 'skipped' AND reason IN ('github', 'local_other'))"
        ).fetchall():
            r = route_fn(url)
            if r is None or (status in ("failed", "thin") and r.lane == lane):
                continue
            if status == "thin" and r.lane in FORUM_LANES:  # it has a file: --refetch-comments moves it safely
                continue
            if (r.lane, r.doc_key, r.reason) != (lane, doc_key, reason):
                new_status = "skipped" if r.lane == "skip" else "pending"
                changed.append((r.lane, r.doc_key, r.doc_id, new_status, r.reason, url))
        self.db.executemany(
            "UPDATE urls SET lane = ?, doc_key = ?, doc_id = ?, status = ?, reason = ?, tried = '' WHERE url = ?",
            changed,
        )
        self.db.commit()
        return len(changed)

    def pending(self, lane: str) -> list[tuple[str, str]]:
        """(doc_key, url) for each pending document in a lane; one URL per doc_key."""
        return self.db.execute(
            "SELECT doc_key, MIN(url) FROM urls WHERE lane = ? AND status = 'pending'"
            " GROUP BY doc_key ORDER BY MIN(rowid)",
            (lane,),
        ).fetchall()

    def done_without(self, lane: str, pass_name: str) -> list[tuple[str, str]]:
        """(doc_key, url) for each finished document in a lane that this pass hasn't touched."""
        return self.db.execute(
            "SELECT doc_key, MIN(url) FROM urls WHERE lane = ? AND status = 'ok' AND instr(tried, ?) = 0"
            " GROUP BY doc_key ORDER BY MIN(rowid)",
            (lane, f",{pass_name}"),
        ).fetchall()

    def archived_in(self, lane: str, route_fn: Callable[[str], Route | None]) -> dict[str, tuple[int, list[str]]]:
        """Finished documents that belong in this lane, whether it fetched them or the generic lane
        did before it existed: {doc_key in this lane: (longest text so far, URLs)}. Pages you saved
        yourself are left alone."""
        found: dict[str, tuple[int, list[str]]] = {}
        for url, chars in self.db.execute(
            "SELECT url, COALESCE(chars, 0) FROM urls WHERE status IN ('ok', 'thin') AND lane IN ('generic', ?)", (lane,)
        ).fetchall():
            r = route_fn(url)
            if r is not None and r.lane == lane:
                longest, urls = found.get(r.doc_key, (0, []))
                found[r.doc_key] = (max(longest, chars), [*urls, url])
        return found

    def adopt(self, doc_key: str, lane: str, urls: list[str]) -> set[str]:
        """Point URLs at a document just written in this lane, and remove the files it replaces.
        Returns the ids of those documents."""
        new_id = doc_id(doc_key)
        marks = ",".join("?" * len(urls))
        old = {did for (did,) in self.db.execute(f"SELECT DISTINCT doc_id FROM urls WHERE url IN ({marks})", urls)} - {new_id}
        self.db.execute(f"UPDATE urls SET lane = ?, doc_key = ?, doc_id = ?, tried = '' WHERE url IN ({marks})",
                        (lane, doc_key, new_id, *urls))
        self.commit()  # first: a crash now leaves a spare file, not a manifest row with no file
        replaced = {did for did in old if did and not self.db.execute(
            "SELECT 1 FROM urls WHERE doc_id = ? LIMIT 1", (did,)).fetchone()}
        for did in replaced:
            self.md_path(did).unlink(missing_ok=True)
        return replaced

    def requeue(self, lanes: Iterable[str], statuses: Iterable[str] = ("failed",), hosts: Iterable[str] = ()) -> int:
        """Back to pending with no fallback passes tried; `hosts` (fnmatch globs) narrows it to those sites."""
        lanes, statuses, hosts = list(lanes), list(statuses), [h.lower() for h in hosts]
        where = f"lane IN ({','.join('?' * len(lanes))}) AND status IN ({','.join('?' * len(statuses))})"
        if not hosts:
            cur = self.db.execute(f"UPDATE urls SET status = 'pending', tried = '' WHERE {where}", (*lanes, *statuses))
            self.db.commit()
            return cur.rowcount
        urls = [(url,) for (url,) in self.db.execute(f"SELECT url FROM urls WHERE {where}", (*lanes, *statuses))
                if any(fnmatch(urlsplit(url).hostname or "", h) for h in hosts)]
        self.db.executemany("UPDATE urls SET status = 'pending', tried = '' WHERE url = ?", urls)
        self.db.commit()
        return len(urls)

    def start_refresh(self, lane: str) -> list[tuple[str, str, int]]:
        """Begin or resume refreshing every PDF in a lane. Failed ones go back to pending. Returns
        (doc_key, url, chars) for those that have text, which must only ever be replaced by more of
        it; a thin one also gets its fallback passes again."""
        docs: dict[str, tuple[str, str, int]] = {}
        requeue = set()
        for url, doc_key, status, content_type, chars, tried in self.db.execute(
            "SELECT url, doc_key, status, content_type, COALESCE(chars, 0), tried FROM urls"
            " WHERE lane = ? AND status IN ('ok', 'thin', 'failed') ORDER BY rowid", (lane,)
        ).fetchall():
            is_pdf = content_type == "application/pdf" or urlsplit(url).path.lower().endswith(".pdf")
            if not is_pdf or f",{REFRESH}" in tried:
                continue
            if status == "failed":
                requeue.add(doc_key)
            else:
                docs.setdefault(doc_key, (doc_key, url, chars))
        self.db.executemany("UPDATE urls SET status = 'pending', tried = ? WHERE doc_key = ?",
                            ((f",{REFRESH}", key) for key in requeue))
        self.db.executemany("UPDATE urls SET tried = '' WHERE doc_key = ?", ((key,) for key in docs))
        self.db.commit()
        return list(docs.values())

    def finish_refresh(self, lane: str) -> None:
        """So the next refresh starts over rather than resuming this one."""
        self.db.execute("UPDATE urls SET tried = replace(tried, ?, '') WHERE lane = ?", (f",{REFRESH}", lane))
        self.db.commit()

    def retry_candidates(self, lane: str, pass_name: str, eligible: Callable[[str], bool]) -> list[tuple[str, int]]:
        """(doc_key, chars so far; -1 if failed) for thin docs and failures whose reason is
        eligible, skipping those this fallback pass already tried."""
        rows = self.db.execute(
            "SELECT doc_key, status, reason, COALESCE(chars, -1) FROM urls WHERE lane = ?"
            " AND status IN ('thin', 'failed') AND instr(tried, ?) = 0 ORDER BY rowid",
            (lane, f",{pass_name}"),
        )
        return [(key, chars) for key, status, reason, chars in rows if status == "thin" or eligible(reason or "")]

    def mark_tried(self, doc_key: str, pass_name: str) -> None:
        self.db.execute("UPDATE urls SET tried = tried || ? WHERE doc_key = ? AND instr(tried, ?) = 0",
                        (f",{pass_name}", doc_key, f",{pass_name}"))

    def record(self, doc_key: str, status: str, reason: str | None = None, **fields) -> None:
        """Set the outcome for every URL sharing doc_key. Commits at most once a second."""
        cols = {"status": status, "reason": reason, "updated_at": now_iso(), **fields}
        assignments = ", ".join(f"{c} = ?" for c in cols)
        self.db.execute(
            f"UPDATE urls SET {assignments}, attempts = attempts + 1 WHERE doc_key = ?",
            (*cols.values(), doc_key),
        )
        if time.monotonic() - self._last_commit > 1.0:
            self.commit()

    def find_url(self, r: Route) -> dict | None:
        """The manifest row for this page: same dedup key, same document or same URL."""
        row = self.db.execute(
            "SELECT url, lane, doc_key, status, chars FROM urls WHERE dedup_key = ? OR url = ?"
            " OR (doc_key IS NOT NULL AND doc_key = ?) LIMIT 1",
            (r.dedup_key, r.url, r.doc_key),
        ).fetchone()
        return dict(zip(("url", "lane", "doc_key", "status", "chars"), row)) if row else None

    def record_import(self, url: str, doc_key: str, dedup_key: str, status: str, reason: str | None, chars: int,
                      final_url: str | None) -> None:
        """A saved page now holds this document, for every URL that shares it."""
        self.db.execute("INSERT OR IGNORE INTO urls (url, lane, status, dedup_key) VALUES (?, 'singlefile', ?, ?)",
                        (url, status, dedup_key))
        self.db.execute(
            "UPDATE urls SET lane = 'singlefile', doc_key = ?, doc_id = ?, status = ?, reason = ?, chars = ?,"
            " final_url = ?, http_status = NULL, tried = '', updated_at = ?, attempts = attempts + 1"
            " WHERE url = ? OR doc_key = ?",
            (doc_key, doc_id(doc_key), status, reason, chars, final_url, now_iso(), url, doc_key),
        )
        if time.monotonic() - self._last_commit > 1.0:
            self.commit()

    def imported_files(self) -> set[str]:
        return {sha for (sha,) in self.db.execute("SELECT sha1 FROM imported_files")}

    def mark_imported(self, sha1: str, name: str, url: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO imported_files VALUES (?, ?, ?, ?)", (sha1, name, url, now_iso()))

    def commit(self) -> None:
        self.db.commit()
        self._last_commit = time.monotonic()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- documents ----------------------------------------------------------

    def md_path(self, did: str) -> Path:
        return md_path(self.out, did)

    def write_doc(self, doc_key: str, meta: dict, body: str) -> int:
        """Write md/<id[:2]>/<id>.md with YAML front matter; returns body length. The text gets the
        repairs in clean.py: extraction workers have done them already, and the API lanes haven't."""
        did = doc_id(doc_key)
        meta = {"id": did, "fetched_at": now_iso(), **meta}
        body = clean.clean_text(body.strip(), pdf=clean.is_pdf_source(meta.get("source")),
                                lexicon=clean.lexicon_at(self.out / clean.LEXICON_FILE))
        if isinstance(meta.get("title"), str):
            meta["title"] = clean.clean_title(meta["title"])
        lines = ["---"]
        for key in FRONT_MATTER_KEYS:
            value = meta.get(key)
            if value:
                # JSON strings are valid YAML scalars, so any title/author is safe
                lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
        lines.append("---")
        text = "\n".join(lines) + "\n\n" + body.strip() + "\n"

        path = self.md_path(did)
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return len(body)

    def save_raw(self, doc_key: str, ext: str, data: bytes | None = None, move_from: str | None = None,
                 copy_from: str | None = None) -> str:
        """Keep a fetched body under raw/<id[:2]>/<id>.<ext>; returns the path relative to the output dir.
        Bytes are zstd-compressed (ext should end in .zst); move_from (a finished PDF download) is kept
        as-is, and copy_from (one of your local files) is copied, leaving the original where it is."""
        did = doc_id(doc_key)
        path = self.out / "raw" / did[:2] / f"{did}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if copy_from is not None:
            tmp = path.with_name(path.name + ".tmp")
            shutil.copyfile(copy_from, tmp)
            os.replace(tmp, path)
        elif move_from is not None:
            os.replace(move_from, path)
        else:
            import zstandard

            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(zstandard.ZstdCompressor(level=3).compress(data))
            os.replace(tmp, path)
        return str(path.relative_to(self.out))

    def read_doc(self, did: str) -> tuple[dict, str] | None:
        return read_doc_file(self.md_path(did))

    def done_docs(self) -> Iterator[tuple[str, str, list[str]]]:
        """(doc_id, lane, urls) for every document with status ok/thin."""
        rows = self.db.execute(
            "SELECT doc_id, lane, GROUP_CONCAT(url, char(10)) FROM urls"
            " WHERE status IN ('ok', 'thin') GROUP BY doc_id ORDER BY MIN(rowid)"
        )
        for did, lane, urls in rows:
            yield did, lane, urls.split("\n")
