"""`osiris clean`: clean.py's repairs for documents archived before them, and `--undo`.

First the archive's vocabulary is counted into data/lexicon.sqlite, which rejoins words broken at
line ends where a document alone can't tell. Then every document is cleaned. Only files whose text
changes are rewritten; their front matter stays as it was, fetched_at included, apart from a
repaired title. Each original is kept, compressed, in data/clean-backup.sqlite before its file is
replaced, and `--undo` puts back every one that hasn't changed since.

The manifest's character counts follow the new text, and review decisions are carried over to it:
a decision holds for the exact text it was made about, so without that a cleaned document marked
Bad would be voided by the next garbage check and come back into exports. Every step is safe to
interrupt: a file replaced without its bookkeeping is noticed and finished by the next run.

Local text files with replacement characters (U+FFFD) are read again from the original, when it's
still there, with the encoding detected rather than assumed to be UTF-8.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import clean, extract, review
from .localfile import candidate_paths
from .store import Store, now_iso

log = logging.getLogger(__name__)

BACKUP_FILE = "clean-backup.sqlite"
REPORT_FILE = "clean-report.tsv"
FLUSH_AT = 300_000  # distinct words held in memory before they're added to the lexicon on disk
COMMIT_EVERY = 5.0
REPORT_COLUMNS = ("doc_id", "url", "rule", "before", "after", "context")


@dataclass
class Totals:
    checked: int = 0
    changed: int = 0
    finished: int = 0  # cleaned by an interrupted run whose bookkeeping this one completed
    rules: Counter = field(default_factory=Counter)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _split(raw: str) -> tuple[str, dict, str]:
    """(front matter block as written, its values, body) of a stored document."""
    if raw.startswith("---\n") and (end := raw.find("\n---\n", 3)) >= 0:
        head = raw[:end + 5]
        meta = {}
        for line in head[4:-5].splitlines():
            key, _, value = line.partition(": ")
            meta[key] = json.loads(value)
        return head, meta, raw[end + 5:].strip()
    return "", {}, raw.strip()


def _join(head: str, title: str | None, body: str) -> str:
    """The document as Store.write_doc writes it, with only the title line replaced."""
    if title is not None:
        head = "".join(f"title: {json.dumps(title, ensure_ascii=False)}\n" if line.startswith("title: ") else line
                       for line in head.splitlines(keepends=True))
    return (head + "\n" if head else "") + body + "\n"


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _documents(store: Store) -> Iterator[tuple[str, str]]:
    """(doc_id, first URL) of every stored document."""
    yield from store.db.execute("SELECT doc_id, MIN(url) FROM urls WHERE status IN ('ok', 'thin')"
                                " AND doc_id IS NOT NULL GROUP BY doc_id ORDER BY MIN(rowid)").fetchall()


class _Every:
    def __init__(self, seconds: float):
        self.seconds, self.last = seconds, time.monotonic()

    def __call__(self) -> bool:
        if time.monotonic() - self.last < self.seconds:
            return False
        self.last = time.monotonic()
        return True


# -- the lexicon ----------------------------------------------------------------------------------

def build_lexicon(store: Store) -> Path:
    """Count every word and hyphenated pair in the archive, keeping those seen at least twice."""
    path = store.out / clean.LEXICON_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("CREATE TABLE words (word TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;"
                     "CREATE TABLE pairs (l TEXT, r TEXT, n INTEGER NOT NULL, PRIMARY KEY (l, r)) WITHOUT ROWID;")
    words, pairs = Counter(), Counter()

    def flush() -> None:
        db.executemany("INSERT INTO words VALUES (?, ?) ON CONFLICT (word) DO UPDATE SET n = n + excluded.n",
                       words.items())
        db.executemany("INSERT INTO pairs VALUES (?, ?, ?) ON CONFLICT (l, r) DO UPDATE SET n = n + excluded.n",
                       ((l, r, n) for (l, r), n in pairs.items()))
        db.commit()
        words.clear()
        pairs.clear()

    docs = list(_documents(store))
    report = _Every(COMMIT_EVERY)
    try:
        for i, (did, _) in enumerate(docs, 1):
            try:
                _, meta, body = _split(store.md_path(did).read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            w, p = clean.vocabulary(body, clean.is_pdf_source(meta.get("source")))
            words.update(w)
            pairs.update(p)
            if len(words) + len(pairs) > FLUSH_AT:
                flush()
            if report():
                log.info("lexicon: %d of %d documents counted", i, len(docs))
        flush()
        db.execute("DELETE FROM words WHERE n < 2")  # once is a typo or a fragment as often as a word
        db.execute("DELETE FROM pairs WHERE n < 2")
        db.commit()
        db.execute("VACUUM")
        size = db.execute("SELECT (SELECT COUNT(*) FROM words), (SELECT COUNT(*) FROM pairs)").fetchone()
    finally:
        db.close()
    os.replace(tmp, path)
    log.info("lexicon: %d words and %d hyphenated pairs seen at least twice", *size)
    return path


# -- cleaning -------------------------------------------------------------------------------------

def _rows(store: Store, did: str) -> list[tuple[str, str | None]]:
    return store.db.execute("SELECT url, raw_path FROM urls WHERE doc_id = ? ORDER BY rowid", (did,)).fetchall()


def _producer(store: Store, did: str) -> str | None:
    """The Producer field of the PDF a document came from, if a copy is on this machine."""
    import pypdfium2 as pdfium

    for url, raw_path in _rows(store, did):
        paths = candidate_paths(url) if url.startswith("file:") else []
        if raw_path and raw_path.endswith(".pdf"):
            paths.append(store.out / raw_path)
        for path in paths:
            if path.is_file():
                try:
                    pdf = pdfium.PdfDocument(str(path))
                    try:
                        return pdf.get_metadata_dict(skip_empty=True).get("Producer")
                    finally:
                        pdf.close()
                except Exception:  # unreadable now: decide without it
                    return None
    return None


def _reread(store: Store, did: str, source: str | None, body: str, lexicon: clean.Lexicon) -> str | None:
    """A text file's text read again with its encoding detected, if that has fewer replacement
    characters than what's stored: from your original for a local file, or a raw copy from an earlier run."""
    import zstandard

    if source not in ("file:text", "text"):
        return None
    for url, raw_path in _rows(store, did):
        data = None
        if source == "file:text":
            path = next((p for p in candidate_paths(url) if p.is_file()), None)
            if path is not None:
                data = path.read_bytes()
        elif raw_path and raw_path.endswith(".txt.zst") and (store.out / raw_path).is_file():
            data = zstandard.ZstdDecompressor().stream_reader((store.out / raw_path).read_bytes()).read()
        if data is not None:
            text = extract.decode_text(data, lexicon=lexicon).strip()
            return text if text.count("�") < body.count("�") else None
    return None


class _Bookkeeping:
    """What has to follow a document's text when it's rewritten in place."""

    def __init__(self, store: Store):
        self.store = store
        self.review = review.connect(store.out) if review.exists(store.out) else None

    def follow(self, did: str, before: str, after: str, title: str | None) -> None:
        self.store.db.execute("UPDATE urls SET chars = ?, title = ? WHERE doc_id = ? AND status IN ('ok', 'thin')",
                              (len(after), title or "", did))
        if self.review is not None:
            review.carry_decision(self.review, did, review.text_hash(before), review.text_hash(after))
            review.rescore(self.review, did, title, after, self.store.md_path(did))

    def needed(self, did: str, before: str, after: str) -> bool:
        """Whether an interrupted run left this document's bookkeeping undone."""
        chars = self.store.db.execute("SELECT MAX(chars) FROM urls WHERE doc_id = ? AND status IN ('ok', 'thin')",
                                      (did,)).fetchone()[0]
        if chars is not None and chars != len(after):
            return True
        if self.review is None:
            return False
        stale = review.text_hash(before)
        return bool(self.review.execute("SELECT 1 FROM decisions WHERE doc_id = ? AND hash = ? UNION ALL"
                                       " SELECT 1 FROM scores WHERE doc_id = ? AND hash = ?",
                                       (did, stale, did, stale)).fetchone())

    def commit(self) -> None:
        self.store.commit()
        if self.review is not None:
            self.review.commit()

    def close(self) -> None:
        self.commit()
        if self.review is not None:
            review.write_exclude(self.review, self.store)
            self.review.close()


def _backups(out: Path) -> sqlite3.Connection:
    db = sqlite3.connect(out / BACKUP_FILE)
    db.execute("CREATE TABLE IF NOT EXISTS originals (doc_id TEXT PRIMARY KEY, original BLOB NOT NULL,"
               " cleaned TEXT NOT NULL, cleaned_at TEXT)")  # cleaned: sha1 of the file this run wrote
    return db


def _original(blob: bytes) -> str:
    import zstandard

    return zstandard.ZstdDecompressor().decompress(blob).decode("utf-8")


def clean_archive(store: Store, dry_run: bool = False) -> Totals:
    """Clean every stored document; with dry_run, only write the report of what would change."""
    import zstandard

    lexicon = clean.Lexicon(build_lexicon(store))
    backups = _backups(store.out)
    backed_up = dict(backups.execute("SELECT doc_id, cleaned FROM originals"))
    books = _Bookkeeping(store)
    compress = zstandard.ZstdCompressor(level=6)
    totals = Totals()
    docs = list(_documents(store))
    rows_path = store.out / (REPORT_FILE + ".tmp")
    progress, commit = _Every(COMMIT_EVERY), _Every(COMMIT_EVERY)
    finished = False
    with open(rows_path, "w", encoding="utf-8") as report:

        def row(did: str, url: str, rule: str, before: str, after: str, context: str) -> None:
            fields = (did, url, rule, before, after, context)
            report.write("\t".join(str(f).replace("\t", " ").replace("\n", "⏎") for f in fields) + "\n")

        try:
            for i, (did, url) in enumerate(docs, 1):
                path = store.md_path(did)
                try:
                    raw = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                totals.checked += 1
                head, meta, body = _split(raw)
                source = meta.get("source")
                pdf = clean.is_pdf_source(source)
                text = body
                if "�" in body and (reread := _reread(store, did, source, body, lexicon)) is not None:
                    row(did, url, "text re-read with its encoding detected", f"{body.count(chr(0xfffd))} × U+FFFD",
                        f"{reread.count(chr(0xfffd))} × U+FFFD", "")
                    totals.rules["text re-read with its encoding detected"] += 1
                    text = reread
                producer = None
                if pdf and not source.endswith(":ocr") and any(c in body for c in "￾\x02‐"):
                    producer = _producer(store, did)
                result = clean.clean(text, pdf=pdf, producer=producer, lexicon=lexicon)
                title = meta.get("title")
                new_title = clean.clean_title(title) if isinstance(title, str) else title
                if result.text == body and new_title == title:
                    if not dry_run and backed_up.get(did) == _sha(raw):  # this file came from a cleanup
                        before = _split(_original(backups.execute("SELECT original FROM originals WHERE doc_id = ?",
                                                                  (did,)).fetchone()[0]))[2]
                        if books.needed(did, before, body):
                            books.follow(did, before, body, title)
                            totals.finished += 1
                    continue
                totals.changed += 1
                totals.rules.update(result.counts)
                for example in result.examples:
                    row(did, url, *example)
                if new_title != title:
                    row(did, url, "title", title, new_title, "")
                if dry_run:
                    continue
                new_raw = _join(head, new_title if new_title != title else None, result.text)
                # the original first, so that no file is ever replaced without a way back
                backups.execute("INSERT OR IGNORE INTO originals VALUES (?, ?, ?, ?)",
                                (did, compress.compress(raw.encode("utf-8")), "", now_iso()))
                backups.execute("UPDATE originals SET cleaned = ?, cleaned_at = ? WHERE doc_id = ?",
                                (_sha(new_raw), now_iso(), did))
                backups.commit()
                _write(path, new_raw)
                books.follow(did, body, result.text, new_title)
                if commit():
                    books.commit()
                if progress():
                    log.info("clean: %d of %d documents checked, %d changed", i, len(docs), totals.changed)
            finished = True
        finally:
            books.close()
            backups.close()
            report.close()
            _write_report(store.out, rows_path, dry_run, finished, totals)
    return totals


def _write_report(out: Path, rows_path: Path, dry_run: bool, finished: bool, totals: Totals) -> None:
    """The report: a line saying what run it's from, the column names, then a row per change."""
    summary = {"mode": "preview" if dry_run else "clean", "at": now_iso(), "finished": finished,
               "checked": totals.checked, "changed": totals.changed, "rules": dict(totals.rules)}
    path = out / REPORT_FILE
    tmp = path.with_name(path.name + ".new")
    with open(tmp, "w", encoding="utf-8") as report, open(rows_path, encoding="utf-8") as rows:
        report.write("# " + json.dumps(summary, ensure_ascii=False) + "\n" + "\t".join(REPORT_COLUMNS) + "\n")
        shutil.copyfileobj(rows, report)
    os.replace(tmp, path)
    rows_path.unlink()


def read_report(out: Path) -> tuple[dict, list[tuple[str, ...]]] | None:
    """(what run it's from, its rows) of the last report, or None if there isn't one."""
    try:
        with open(out / REPORT_FILE, encoding="utf-8") as f:
            first = f.readline()
            summary = json.loads(first[2:]) if first.startswith("# ") else {}
            if summary:
                f.readline()  # the column names
            return summary, [tuple(line.rstrip("\n").split("\t")) for line in f]
    except (OSError, ValueError):
        return None


def kept_originals(out: Path) -> int:
    """How many originals `undo` could put back."""
    if not (out / BACKUP_FILE).exists():
        return 0
    db = _backups(out)
    try:
        return db.execute("SELECT COUNT(*) FROM originals").fetchone()[0]
    finally:
        db.close()


def undo(store: Store) -> tuple[int, int]:
    """Put back every original whose cleaned file hasn't changed since. Returns (restored, skipped)."""
    path = store.out / BACKUP_FILE
    if not path.exists():
        return 0, 0
    backups = _backups(store.out)
    books = _Bookkeeping(store)
    restored = skipped = 0
    try:
        for did, blob, cleaned in backups.execute("SELECT doc_id, original, cleaned FROM originals").fetchall():
            md = store.md_path(did)
            try:
                raw = md.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            if raw is None or _sha(raw) != cleaned:  # fetched again or changed since: that's newer
                skipped += 1
                continue
            original = _original(blob)
            _write(md, original)
            _, meta, before = _split(original)
            books.follow(did, _split(raw)[2], before, meta.get("title"))
            backups.execute("DELETE FROM originals WHERE doc_id = ?", (did,))
            backups.commit()
            books.commit()
            restored += 1
    finally:
        books.close()
        backups.close()
    return restored, skipped
