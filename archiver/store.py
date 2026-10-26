"""SQLite manifest (one row per input URL) and the markdown document store."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from .route import Route, doc_id, safe_dedup_key

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
    dedup_key    TEXT,  -- archiver.dedup.dedup_key of the URL as it appeared in the input
    raw_path     TEXT,  -- relative to the output dir, only with --keep-raw
    tried        TEXT NOT NULL DEFAULT ''  -- fallback passes already run on it, e.g. ",impersonate,wayback"
);
CREATE INDEX IF NOT EXISTS urls_lane_status ON urls(lane, status);
CREATE INDEX IF NOT EXISTS urls_doc_key ON urls(doc_key);
"""
MIGRATIONS = {"dedup_key": "ALTER TABLE urls ADD COLUMN dedup_key TEXT",
              "raw_path": "ALTER TABLE urls ADD COLUMN raw_path TEXT",
              "tried": "ALTER TABLE urls ADD COLUMN tried TEXT NOT NULL DEFAULT ''"}

FRONT_MATTER_KEYS = ("id", "url", "final_url", "source", "title", "author", "date", "site", "fetched_at")
DONE = ("ok", "thin")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, out_dir: str | os.PathLike):
        self.out = Path(out_dir)
        self.md_dir = self.out / "md"
        self.tmp_dir = self.out / "tmp"
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

    def pending(self, lane: str) -> list[tuple[str, str]]:
        """(doc_key, url) for each pending document in a lane; one URL per doc_key."""
        return self.db.execute(
            "SELECT doc_key, MIN(url) FROM urls WHERE lane = ? AND status = 'pending'"
            " GROUP BY doc_key ORDER BY MIN(rowid)",
            (lane,),
        ).fetchall()

    def requeue(self, lanes: Iterable[str], statuses: Iterable[str] = ("failed",)) -> int:
        lanes, statuses = list(lanes), list(statuses)
        cur = self.db.execute(
            f"UPDATE urls SET status = 'pending', tried = '' WHERE lane IN ({','.join('?' * len(lanes))})"
            f" AND status IN ({','.join('?' * len(statuses))})",
            (*lanes, *statuses),
        )
        self.db.commit()
        return cur.rowcount

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

    def commit(self) -> None:
        self.db.commit()
        self._last_commit = time.monotonic()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- documents ----------------------------------------------------------

    def md_path(self, did: str) -> Path:
        return self.md_dir / did[:2] / f"{did}.md"

    def write_doc(self, doc_key: str, meta: dict, body: str) -> int:
        """Write md/<id[:2]>/<id>.md with YAML front matter; returns body length."""
        did = doc_id(doc_key)
        meta = {"id": did, **meta, "fetched_at": now_iso()}
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

    def save_raw(self, doc_key: str, ext: str, data: bytes | None = None, move_from: str | None = None) -> str:
        """Keep a fetched body under raw/<id[:2]>/<id>.<ext>; returns the path relative to the output dir.
        Bytes are zstd-compressed (ext should end in .zst); move_from (a finished PDF download) is kept as-is."""
        did = doc_id(doc_key)
        path = self.out / "raw" / did[:2] / f"{did}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if move_from is not None:
            os.replace(move_from, path)
        else:
            import zstandard

            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(zstandard.ZstdCompressor(level=3).compress(data))
            os.replace(tmp, path)
        return str(path.relative_to(self.out))

    def read_doc(self, did: str) -> tuple[dict, str] | None:
        path = self.md_path(did)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        meta: dict = {}
        if raw.startswith("---\n"):
            header, _, body = raw[4:].partition("\n---\n")
            for line in header.splitlines():
                key, _, value = line.partition(": ")
                meta[key] = json.loads(value)
        else:
            body = raw
        return meta, body.strip()

    def done_docs(self) -> Iterator[tuple[str, str, list[str]]]:
        """(doc_id, lane, urls) for every document with status ok/thin."""
        rows = self.db.execute(
            "SELECT doc_id, lane, GROUP_CONCAT(url, char(10)) FROM urls"
            " WHERE status IN ('ok', 'thin') GROUP BY doc_id ORDER BY MIN(rowid)"
        )
        for did, lane, urls in rows:
            yield did, lane, urls.split("\n")
