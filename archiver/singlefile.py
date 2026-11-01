"""`archiver import`: pages you saved yourself with the SingleFile browser extension, stored
as markdown like every other document.

SingleFile records where a page came from in a comment at the top of the file. That URL goes
through the same dedup as `archiver run`: a page already in the manifest is upgraded when the
saved copy has more text, and a URL imported here is never queued for fetching afterwards.
Saved pages are often logged-in or paywalled ones no fetcher can reach, so URLs that `archiver
run` skips on purpose (chat apps, GitHub, X profiles) are stored too.

The files are mostly inlined CSS and images (a 20 MB page can hold 100 KB of text), which are
cut with regexes before parsing to keep extraction fast and memory flat.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import extract
from .route import route
from .store import DONE, Store

log = logging.getLogger(__name__)

SUFFIXES = {".html", ".htm"}
MAX_BYTES = 512 * 2**20
HEAD_BYTES = 16 * 1024  # SingleFile's comment follows the <html> tag; don't match page text about it
# lanes whose API documents hold the complete post: a saved page only adds site chrome
COMPLETE_LANES = {"twitter", "stackexchange", "reddit"}

HEADER_RE = re.compile(rb"(?:Page saved with|Archive processed by) SingleFile\s+url:\s*(\S+)"
                       rb"(?:\s+saved date:\s*([^\r\n]+))?")
CANONICAL_RE = re.compile(rb"<link\b[^>]*\brel=[\"']?canonical\b[^>]*>", re.I)
HREF_RE = re.compile(rb"\bhref=[\"']?([^\"'\s>]+)", re.I)
# JSON-LD stays: trafilatura reads author and date from it
NOISE_RE = re.compile(rb"<style\b[^>]*>.*?</style\s*>|<script\b(?![^>]*ld\+json)[^>]*>.*?</script\s*>", re.I | re.S)
DATA_URI_RE = re.compile(rb"data:[a-z]+/[a-z0-9.+-]+(?:;[a-z0-9=-]+)*,[^\"'\s>)]{256,}", re.I)


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(2**20):
            digest.update(chunk)
    return digest.hexdigest()


def _saved_at(value: bytes | None) -> str | None:
    """'Wed Jul 08 2026 13:35:25 GMT+0300 (Türkiye Standard Time)' -> UTC ISO 8601."""
    if not value:
        return None
    try:
        when = datetime.strptime(value.decode("utf-8", "replace").split(" (")[0].strip(), "%a %b %d %Y %H:%M:%S GMT%z")
    except ValueError:
        return None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_dom(data: bytes) -> bytes:
    """Drop what SingleFile kept but marked hidden, and expose saved shadow DOM, which
    extractors skip as an inert <template>."""
    import lxml.html

    try:
        tree = lxml.html.fromstring(data, parser=lxml.html.HTMLParser(encoding="utf-8", huge_tree=True))
        for el in tree.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' sf-hidden ')]"):
            if el.getparent() is not None:
                el.drop_tree()
        for el in tree.xpath("//template[@shadowrootmode or @shadowmode]"):
            el.tag = "div"
        return lxml.html.tostring(tree, encoding="utf-8")
    except Exception:  # keep the page as it is rather than lose it
        return data


def saved_page_to_doc(path: str) -> dict:
    """Runs in an extraction worker. Text and metadata of one saved page, plus its origin."""
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            return {"text": "", "error": "too_large"}
        data = Path(path).read_bytes()
        head = data[:HEAD_BYTES]
        if b"data-sfz" in head:  # the "self-extracting ZIP" format: the page is compressed inside
            return {"text": "", "error": "singlefile_zip"}
        data = DATA_URI_RE.sub(b"data:,", NOISE_RE.sub(b"", data))
    except OSError as e:
        return {"text": "", "error": f"read_error:{type(e).__name__}"}
    except MemoryError:
        return {"text": "", "error": "too_large"}

    header = HEADER_RE.search(head)
    if header:
        url = header.group(1).decode("utf-8", "replace")
    else:
        tag = CANONICAL_RE.search(data[:HEAD_BYTES])
        href = HREF_RE.search(tag.group(0)) if tag else None
        url = html.unescape(href.group(1).decode("utf-8", "replace")) if href else None
    if b"sf-hidden" in data or b"shadowrootmode" in data or b"shadowmode" in data:
        data = _clean_dom(data)
    doc = extract.html_to_doc(data, url)
    return {**doc, "url": url, "singlefile": bool(header), "saved_at": _saved_at(header.group(2)) if header else None}


def _store_page(store: Store, path: Path, sha: str, res: dict) -> str:
    """Write the document unless the archive already has a better one; returns the outcome."""
    text, saved_url = res["text"], res["url"]
    r = route(saved_url) if saved_url else None
    if r is None or r.reason == "bad_url":  # no origin, or a file:// or browser-internal one
        url = key = None
        row = None
    else:
        row = store.find_url(r)
        url = row["url"] if row else r.url
        key = (row and row["doc_key"]) or r.doc_key or r.url
    if key is None:
        url, key = f"file:{sha[:16]}/{path.name}", f"file:{sha}"
    if row and row["status"] in DONE and (row["chars"] or 0) >= len(text):
        return "kept existing, it has more text"
    if row and row["status"] == "ok" and row["lane"] in COMPLETE_LANES:
        return f"kept existing {row['lane']} document"

    meta = {"url": saved_url or url, "source": "singlefile" if res["singlefile"] else "html_file",
            "title": res.get("title") or path.stem, "author": res.get("author"), "date": res.get("date"),
            "site": res.get("site")}
    if res["saved_at"]:
        meta["fetched_at"] = res["saved_at"]
    chars = store.write_doc(key, meta, text)
    ok = chars >= extract.MIN_CHARS
    store.record_import(url, key, r.dedup_key if r and r.reason != "bad_url" else key, "ok" if ok else "thin",
                        None if ok else "short", chars, saved_url)
    outcome = "replaced an existing document" if row and row["status"] in DONE else "saved"
    return outcome if ok else f"{outcome} (under {extract.MIN_CHARS} characters)"


async def run_import(store: Store, paths: list[Path], pool: extract.WorkerPool, workers: int) -> None:
    files = sorted({f for p in paths for f in ([p] if p.is_file() else p.rglob("*"))
                    if f.is_file() and f.suffix.lower() in SUFFIXES})
    log.info("%d HTML files found", len(files))
    imported = store.imported_files()
    slots = asyncio.Semaphore(workers + 1)
    outcomes: Counter = Counter()
    failures: list[tuple[Path, str]] = []

    async def one(path: Path) -> None:
        async with slots:
            try:
                sha = await asyncio.to_thread(file_sha1, path)
            except OSError as e:
                failures.append((path, f"read_error:{type(e).__name__}"))
                return
            if sha in imported:
                outcomes["already imported"] += 1
                return
            res = await pool.run(saved_page_to_doc, str(path))
        if not res["text"]:
            failures.append((path, res.get("error") or "no_text"))  # not recorded: a later run tries again
            return
        outcomes[_store_page(store, path, sha, res)] += 1
        store.mark_imported(sha, path.name, res["url"])
        done = sum(outcomes.values()) + len(failures)
        if done % 100 == 0:
            log.info("%d of %d files", done, len(files))

    try:
        await asyncio.gather(*(one(f) for f in files))
    finally:
        store.commit()
        for outcome, n in outcomes.most_common():
            print(f"{n:>7}  {outcome}")
        if failures:
            print(f"{len(failures):>7}  failed:")
            for path, reason in failures:
                print(f"         {reason:<24} {path}")
