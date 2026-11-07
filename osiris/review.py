"""Review: find documents that are probably garbage, and remember what you decided about each one.

Every document gets a badness score from cheap text signals. The score only ranks a list for a
person to read: nothing here deletes or rewrites a document. Results and decisions live in their
own database, review.sqlite, so reviewing never competes with a run for the manifest.

A decision holds for the exact text it was made about. When a document's text changes, its decision
is dropped and it's scored afresh. A fix that was tried and changed nothing comes back as `bad`, so
it can't be forgotten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import clean
from .route import doc_id
from .store import Store, now_iso

log = logging.getLogger(__name__)

FLAGGED = 0.35  # at or above this, a document is on the review list
SAMPLE = 60_000  # characters read from each of three places in a long document
OUTSIDE_LIST = ("fine", "unfixable")
EXCLUDED = ("bad", "unfixable")  # left out of `osiris export` and review-exclude.txt
DECISIONS = ("fine", "bad", "ocr", "refetch", "unfixable")
EXCLUDE_FILE = "review-exclude.txt"
DB_FILE = "review.sqlite"
OLD_NAMES = ("audit.sqlite", "audit-exclude.txt")  # before review was called review everywhere
CANNOT_REFETCH = "singlefile"

# The most frequent words of the languages expected here. Ordinary prose is around 40% these; text
# from a broken PDF font map is close to none.
COMMON = frozenset("""
the of and to in a is that for it as was with be by on not he his but at are this have from or had an they
which one you were her all she there would their we him been has when who will more no if out so said what
up its about into than them can only other new some could time these two may then do first any my now such
like our over me even most made after also did many before must through back years where much your way well
down should because each just those people how too little very make still see own work long get here between
both being under never day same another know while last might us great old year off come since against go
came right used take three few use during without again place however found thought say part once high upon
every does got left number always something fact though less think almost far yet better set why find going
look later next give let using data model results paper based shown value case problem function example
bir ve bu için ile de da olarak daha çok en gibi ama ancak ya kadar olan ne var sonra her hem ki mi ise veya
tarafından göre büyük ilk iki ben sen biz siz o onun bunu şu çünkü nasıl değil
der die das und ist von zu den mit nicht ein eine auf für sich dem des im
le la les et est des une un dans pour que qui sur par pas au du
el los las y es en un una que por con para del se al lo como
""".split())

ENCODING_DAMAGE = re.compile(r"�|\(cid:\d+\)|Ã[\u0080-¿]|â€|Â[\u0080-¿]|[\x00-\x08\x0b\x0e-\x1f]|[-]")
BLOCK_PAGE = re.compile(
    r"enable javascript|javascript is (?:disabled|required)|unusual traffic|are you (?:a )?human|not a robot"
    r"|access denied|verify you are|checking your browser|just a moment|captcha|403 forbidden|404 not found"
    r"|page not found|sign in to continue|log in to continue|subscribe to (?:continue|read)|enable cookies",
    re.I,
)
WORD = re.compile(r"[^\W\d_]+")
MIXED_TOKEN = re.compile(r"\b[^\W\d_]+\d+[^\W\d_]+\b|\b\w*[a-zçğıöşü][A-ZÇĞİÖŞÜ][a-zçğıöşü]\w*\b")
VOWELS = frozenset("aeiouyıöüäéèêàáíóúâîûAEIOUYIÖÜÄÉÈÊÀÁÍÓÚÂÎÛİ")
CODE_LANES = ("github", "stackexchange")  # code is meant to look like this

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    doc_id  TEXT PRIMARY KEY,
    doc_key TEXT, url TEXT, lane TEXT, status TEXT, source TEXT, host TEXT, title TEXT, chars INTEGER,
    mtime   REAL, size INTEGER,
    hash    TEXT,
    score   REAL NOT NULL,
    signals TEXT NOT NULL,  -- JSON {signal: 0-1}
    excerpt TEXT
);
CREATE INDEX IF NOT EXISTS scores_score ON scores(score);
CREATE TABLE IF NOT EXISTS decisions (
    doc_id     TEXT PRIMARY KEY,
    decision   TEXT NOT NULL,  -- fine | bad | ocr | refetch | unfixable
    note       TEXT,
    hash       TEXT,           -- the text this decision was about
    decided_at TEXT,
    applied_at TEXT            -- ocr and refetch: when the fix was attempted
);
"""


def _rename_old(out: Path) -> None:
    """Archives from before the rename keep their decisions: audit.sqlite becomes review.sqlite."""
    old = out / OLD_NAMES[0]
    if old.exists() and not (out / DB_FILE).exists():
        for suffix in ("-wal", "-shm", ""):  # the database last, so it's never there without its journal
            if (out / f"{OLD_NAMES[0]}{suffix}").exists():
                os.replace(out / f"{OLD_NAMES[0]}{suffix}", out / f"{DB_FILE}{suffix}")


def exists(out: str | Path) -> bool:
    out = Path(out)
    return (out / DB_FILE).exists() or (out / OLD_NAMES[0]).exists()


def connect(out: str | Path) -> sqlite3.Connection:
    _rename_old(Path(out))
    db = sqlite3.connect(Path(out) / DB_FILE, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def text_hash(body: str) -> str:
    return hashlib.blake2b(body.encode("utf-8", "replace"), digest_size=16).hexdigest()


def _sample(text: str) -> str:
    if len(text) <= 3 * SAMPLE:
        return text
    mid = len(text) // 2
    return "\n".join((text[:SAMPLE], text[mid - SAMPLE // 2:mid + SAMPLE // 2], text[-SAMPLE:]))


def _ramp(value: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (value - low) / (high - low)))


def signals(text: str) -> dict[str, float]:
    """Each signal runs from 0 (looks fine) to 1 (looks broken)."""
    sample = _sample(text)
    size = max(len(sample), 1)
    found: dict[str, float] = {"encoding": _ramp(len(ENCODING_DAMAGE.findall(sample)) * 1000 / size, 0.2, 5.0)}

    words = WORD.findall(sample)
    latin = [w for w in words if w[0].isascii() or w[0] in "çğıöşüÇĞİÖŞÜäéèêàáíóúñ"]
    letters = sum(map(len, words)) or 1
    # the word checks only mean something for scripts these lists were written for
    if len(latin) >= 50 and sum(map(len, latin)) / letters >= 0.5:
        common = sum(w.lower() in COMMON for w in latin) / len(latin)
        found["few common words"] = 1.0 - _ramp(common, 0.05, 0.22)
        odd = sum(len(w) >= 3 and not VOWELS.intersection(w) for w in latin) + len(MIXED_TOKEN.findall(sample))
        found["odd words"] = _ramp(odd / len(latin), 0.08, 0.30)

    if size >= 200:
        symbols = sum(not (c.isalnum() or c.isspace()) for c in sample)
        found["symbols"] = _ramp(symbols / size, 0.14, 0.35)
        tokens = sample.split()
        found["no spaces"] = _ramp(sum(len(t) for t in tokens if len(t) > 30) / size, 0.10, 0.50)

    lines = [ln.strip() for ln in sample.splitlines() if ln.strip()]
    if len(lines) >= 30:
        found["repeated lines"] = _ramp(1.0 - len(set(lines)) / len(lines), 0.40, 0.90)

    # a long article may well mention a captcha; a short page that does is probably the wall itself
    if len(text) < 4000 and BLOCK_PAGE.search(text):
        found["block page"] = 1.0
    return {name: round(value, 3) for name, value in found.items() if value > 0}


WEIGHTS = {"encoding": 1.0, "few common words": 0.8, "odd words": 0.7, "symbols": 0.6, "no spaces": 0.7,
           "repeated lines": 0.4, "block page": 0.9}


def score(found: dict[str, float], lane: str = "") -> float:
    """Combined so that any one strong signal is enough, and weak ones add up."""
    remaining = 1.0
    for name, value in found.items():
        weight = WEIGHTS[name]
        if lane in CODE_LANES and name in ("few common words", "odd words", "symbols", "no spaces"):
            weight /= 2
        remaining *= 1.0 - weight * value
    return round(1.0 - remaining, 3)


def score_text(text: str, lane: str = "") -> float:
    return score(signals(text), lane)


def _excerpt(body: str) -> str:
    """From the middle: the start of a document is often front matter that reads fine."""
    if len(body) <= 700:
        return body
    start = len(body) // 2 - 350
    return body[start:start + 700]


def scan(store: Store, out: str | Path) -> dict[str, int]:
    """Score new and changed documents, then settle decisions against the current text."""
    db = connect(out)
    known = {did: (mtime, size) for did, mtime, size in db.execute("SELECT doc_id, mtime, size FROM scores")}
    rows = store.db.execute(
        "SELECT doc_id, MIN(doc_key), MIN(url), lane, status, MAX(chars) FROM urls"
        " WHERE status IN ('ok', 'thin') GROUP BY doc_id"
    ).fetchall()
    log.info("review: checking %d documents", len(rows))
    current, scored, missing = set(), 0, 0
    last = time.monotonic()
    for i, (did, key, url, lane, status, chars) in enumerate(rows, 1):
        path = store.md_path(did)
        try:
            stat = path.stat()
        except OSError:
            missing += 1
            continue
        current.add(did)
        if known.get(did) == (stat.st_mtime, stat.st_size):
            db.execute("UPDATE scores SET status = ?, lane = ? WHERE doc_id = ?", (status, lane, did))
            continue
        doc = store.read_doc(did)
        if doc is None:
            missing += 1
            current.discard(did)
            continue
        meta, body = doc
        found = signals(body)
        db.execute(
            "INSERT OR REPLACE INTO scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, key, url, lane, status, meta.get("source"), urlsplit(url).hostname or "local file",
             meta.get("title"), len(body), stat.st_mtime, stat.st_size, text_hash(body), score(found, lane),
             json.dumps(found), _excerpt(body)),
        )
        scored += 1
        if time.monotonic() - last > 5:
            db.commit()
            last = time.monotonic()
            log.info("review: %d of %d checked, %d scored", i, len(rows), scored)
    db.commit()

    settled = _settle(db, current)
    # a document that's gone keeps its row only while a decision still refers to it
    db.execute("CREATE TEMP TABLE here (doc_id TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO here VALUES (?)", ((d,) for d in current))
    db.execute("DELETE FROM scores WHERE doc_id NOT IN (SELECT doc_id FROM here)"
               " AND doc_id NOT IN (SELECT doc_id FROM decisions)")
    db.commit()
    write_exclude(db, store)
    result = {"documents": len(current), "scored": scored, "missing": missing, **settled, **counts(db)}
    db.close()
    return result


def _settle(db: sqlite3.Connection, current: set[str]) -> dict[str, int]:
    hashes = dict(db.execute("SELECT doc_id, hash FROM scores"))
    changed = no_change = 0
    for did, decision, note, old_hash, applied in db.execute(
        "SELECT doc_id, decision, note, hash, applied_at FROM decisions"
    ).fetchall():
        if did in current and hashes.get(did) != old_hash:
            db.execute("DELETE FROM decisions WHERE doc_id = ?", (did,))  # new text gets a new look
            changed += 1
        elif applied and decision in ("ocr", "refetch"):
            label = "OCR" if decision == "ocr" else "Fetching again"
            result = "changed nothing" if did in current else "failed"
            db.execute("UPDATE decisions SET decision = 'bad', note = ?, applied_at = NULL WHERE doc_id = ?",
                       ("; ".join(filter(None, (note, f"{label} {result}"))), did))
            no_change += 1
    db.commit()
    return {"text_changed": changed, "fix_did_nothing": no_change}


def carry_decision(db: sqlite3.Connection, did: str, old_hash: str, new_hash: str) -> None:
    """A document's text was repaired in place, not replaced: what was decided about it still holds."""
    db.execute("UPDATE decisions SET hash = ? WHERE doc_id = ? AND hash = ?", (new_hash, did, old_hash))


def rescore(db: sqlite3.Connection, did: str, title: str | None, body: str, path: Path) -> None:
    """Score a document rewritten in place now, so the next check doesn't take it for a new text."""
    row = db.execute("SELECT lane FROM scores WHERE doc_id = ?", (did,)).fetchone()
    if row is None:  # never scored: the next check does it
        return
    stat, found = path.stat(), signals(body)
    db.execute("UPDATE scores SET title = ?, chars = ?, mtime = ?, size = ?, hash = ?, score = ?, signals = ?,"
               " excerpt = ? WHERE doc_id = ?", (title, len(body), stat.st_mtime, stat.st_size, text_hash(body),
                                                 score(found, row[0]), json.dumps(found), _excerpt(body), did))


def counts(db: sqlite3.Connection) -> dict[str, int]:
    decided = dict(db.execute("SELECT decision, COUNT(*) FROM decisions GROUP BY decision"))
    to_review = db.execute(
        "SELECT COUNT(*) FROM scores s LEFT JOIN decisions d USING (doc_id) WHERE s.score >= ? AND d.doc_id IS NULL",
        (FLAGGED,),
    ).fetchone()[0]
    scored = db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    return {"to_review": to_review, "scored_total": scored,
            **{name: decided.get(name, 0) for name in DECISIONS}}


def decide(db: sqlite3.Connection, doc_ids: list[str], decision: str | None, note: str | None = None) -> int:
    """Record a decision, or clear it with None. Nothing happens to the document itself."""
    if decision is None:
        db.executemany("DELETE FROM decisions WHERE doc_id = ?", ((d,) for d in doc_ids))
    else:
        if decision not in DECISIONS:
            raise ValueError(decision)
        db.executemany(
            "INSERT OR REPLACE INTO decisions (doc_id, decision, note, hash, decided_at)"
            " SELECT doc_id, ?, ?, hash, ? FROM scores WHERE doc_id = ?",
            ((decision, note, now_iso(), d) for d in doc_ids),
        )
    db.commit()
    return len(doc_ids)


def excluded(out: str | Path) -> dict[str, str]:
    """doc_id -> the text hash a bad or unfixable decision was made about."""
    if not exists(out):
        return {}
    db = connect(out)
    rows = dict(db.execute(f"SELECT doc_id, hash FROM decisions WHERE decision IN {EXCLUDED}"))
    db.close()
    return rows


def write_exclude(db: sqlite3.Connection, store: Store) -> Path:
    """Paths of bad and unfixable documents, for indexers that read data/md directly."""
    path = store.out / EXCLUDE_FILE
    ids = [did for (did,) in db.execute(f"SELECT doc_id FROM decisions WHERE decision IN {EXCLUDED} ORDER BY doc_id")]
    path.write_text("".join(f"{store.md_path(did)}\n" for did in ids), encoding="utf-8")
    (store.out / OLD_NAMES[1]).unlink(missing_ok=True)  # replaced by the file above
    return path


is_pdf_source = clean.is_pdf_source


def pending_fixes(db: sqlite3.Connection) -> tuple[list[str], list[tuple[str, str]]]:
    """Doc keys queued for forced OCR, and (doc_key, lane) queued for fetching again."""
    rows = db.execute(
        "SELECT s.doc_key, s.lane, d.decision FROM decisions d JOIN scores s USING (doc_id)"
        " WHERE d.decision IN ('ocr', 'refetch') AND d.applied_at IS NULL"
    ).fetchall()
    return ([key for key, _, decision in rows if decision == "ocr"],
            [(key, lane) for key, lane, decision in rows if decision == "refetch"])


def apply_refetch(store: Store, db: sqlite3.Connection) -> list[str]:
    """Queue documents for fetching again. Returns the lanes that now have work to do."""
    _, refetch = pending_fixes(db)
    lanes = set()
    for key, lane in refetch:
        if lane == CANNOT_REFETCH:  # no source to go back to, so queueing it would only strand it
            db.execute("UPDATE decisions SET decision = 'bad', note = 'A saved page can only be imported again'"
                       " WHERE doc_id = ?", (doc_id(key),))
            continue
        store.db.execute("UPDATE urls SET status = 'pending', tried = '' WHERE doc_key = ?", (key,))
        db.execute("UPDATE decisions SET applied_at = ? WHERE doc_id = ?", (now_iso(), doc_id(key)))
        lanes.add(lane)
    store.commit()
    db.commit()
    return sorted(lanes)


def mark_ocr_applied(db: sqlite3.Connection, keys: list[str]) -> None:
    db.executemany("UPDATE decisions SET applied_at = ? WHERE doc_id = ?", ((now_iso(), doc_id(k)) for k in keys))
    db.commit()
