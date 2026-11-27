"""What a run added: the documents that have text now and didn't when it started, by source, with those
that had failed counted apart, and the thin documents that now have full text. Every one is listed in
data/added.tsv, which each run overwrites.

The state of each document is noted in a temporary table when the run starts, before failed URLs are
queued again, so a document a retry or a fallback brings back counts as one that had failed. A run on
the page notes it before its imports, so new saved pages, tweets and chats count too.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

from .store import md_path

FILE = "added.tsv"
RANK = "CASE status WHEN 'ok' THEN 4 WHEN 'thin' THEN 3 WHEN 'failed' THEN 2 WHEN 'pending' THEN 1 ELSE 0 END"
THIN, OK, FAILED = 3, 4, 2
NAMES = {"generic": "Web pages and PDFs", "localfile": "Local files", "youtube": "YouTube", "github": "GitHub",
         "reddit": "Reddit", "hackernews": "Hacker News", "lesswrong": "LessWrong", "stackexchange": "Stack Exchange",
         "arxiv": "arXiv", "twitter": "X (Twitter)", "singlefile": "Saved pages", "chat": "AI chats"}
HOW = {"new": "new", "failed": "failed before", "thin": "thin before"}


class RunReport:
    """Notes the state of every document when made; finish() says what has text since."""

    def __init__(self, out: str | Path):
        self.out = Path(out)
        self.db = sqlite3.connect(self.out / "manifest.sqlite", timeout=30, check_same_thread=False)
        self.db.execute("CREATE TEMP TABLE before (doc_id TEXT PRIMARY KEY, rank INTEGER)")
        if self._has_manifest():
            self.db.execute(f"INSERT INTO before SELECT doc_id, MAX({RANK}) FROM urls WHERE doc_id IS NOT NULL GROUP BY doc_id")
        self.db.commit()  # an open transaction would keep seeing the manifest as it is now

    def _has_manifest(self) -> bool:
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'urls'").fetchone() is not None

    def finish(self) -> list[str]:
        """The summary's lines, once the list is written."""
        try:
            rows = [] if not self._has_manifest() else self.db.execute(
                f"SELECT n.doc_id, n.rank, b.rank, n.lane, n.url, n.title FROM"
                f" (SELECT doc_id, MAX({RANK}) AS rank, MIN(CASE WHEN status IN ('ok', 'thin') THEN lane END) AS lane,"
                f"  MIN(CASE WHEN status IN ('ok', 'thin') THEN url END) AS url, MAX(title) AS title"
                f"  FROM urls WHERE doc_id IS NOT NULL GROUP BY doc_id) n LEFT JOIN before b USING (doc_id)"
                f" WHERE n.rank >= {THIN} AND (b.rank IS NULL OR b.rank < {THIN} OR (b.rank = {THIN} AND n.rank = {OK}))"
                ).fetchall()
        finally:
            self.db.close()
        listed = []
        for did, now, before, lane, url, title in rows:
            how = "thin" if before == THIN else "failed" if before == FAILED else "new"
            listed.append((how, NAMES.get(lane, lane or ""), title or "", url or "", str(md_path(self.out, did))))
        listed.sort(key=lambda row: (row[1], row[2].lower()))
        path = self.out / FILE
        with open(path, "w", encoding="utf-8") as f:
            f.write("how\tsource\ttitle\taddress\tfile\n")
            for how, *rest in listed:
                f.write("\t".join(" ".join(str(x).split()) for x in (HOW[how], *rest)) + "\n")
        return summary(listed, path)


def summary(listed: list[tuple], path: Path) -> list[str]:
    added = Counter(source for how, source, *_ in listed if how != "thin")
    failed = Counter(source for how, source, *_ in listed if how == "failed")
    fuller = Counter(source for how, source, *_ in listed if how == "thin")
    total, back = sum(added.values()), sum(failed.values())
    if not total:
        lines = ["--- This run added no documents."]
    else:
        lines = [f"--- This run added {total:,} document{'s' * (total != 1)}"
                 + (f", {back:,} of which had failed before:" if back else ":")]
        for source, n in added.most_common():
            lines.append(f"{n:>9,}  {source}" + (f", {failed[source]:,} of which had failed before" if failed[source] else ""))
    if fuller:
        n = sum(fuller.values())
        lines.append(f"--- {n:,} thin document{'s' * (n != 1)} now {'have' if n != 1 else 'has'} full text:")
        lines += [f"{k:>9,}  {source}" for source, k in fuller.most_common()]
    if listed:
        lines.append(f"--- They're listed in {path}")
    return lines
