"""Local files lane: file:// links from your bookmarks and tabs, mostly PDFs.

The originals are only ever opened for reading. Unlike a downloaded PDF, which lives in the
output directory's tmp/ and is deleted after extraction, a local file is yours: this module
never moves, renames, rewrites or deletes one, and --keep-raw stores a copy.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import extract
from .singlefile import saved_page_to_doc
from .store import Store

log = logging.getLogger(__name__)

PDF_EXTS = {"pdf"}
HTML_EXTS = {"html", "htm", "xhtml"}
TEXT_EXTS = {"txt", "md", "markdown", "rst", "tex", "org"}
READABLE_EXTS = PDF_EXTS | HTML_EXTS | TEXT_EXTS
MAX_TEXT_BYTES = 64 * 2**20


def candidate_paths(url: str) -> list[Path]:
    """Where a file:// URL may live on this machine. file:///C:/x is /mnt/c/x under WSL."""
    parts = urlsplit(url)
    path = unquote(parts.path)
    if parts.netloc and parts.netloc != "localhost":
        return [Path(f"//{parts.netloc}{path}")]
    if len(path) > 2 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
        return [Path(path[1:]), Path(f"/mnt/{path[1].lower()}{path[3:]}")]
    return [Path(path)]


def text_to_doc(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        return {"text": "", "error": "too_large"}
    return {"text": data.decode("utf-8", errors="replace")}


async def run_localfile(store: Store, pool: extract.WorkerPool, workers: int, keep_raw: bool = False) -> None:
    pending = store.pending("localfile")
    if not pending:
        return
    log.info("localfile: %d local files", len(pending))
    slots = asyncio.Semaphore(workers + 1)

    async def one(doc_key: str, url: str) -> None:
        path = next((p for p in candidate_paths(url) if p.is_file()), None)
        if path is None:
            store.record(doc_key, "failed", "file_missing")  # --retry-failed checks again
            return
        if not os.access(path, os.R_OK):
            store.record(doc_key, "failed", "file_unreadable")
            return
        ext = path.suffix.lower().lstrip(".")
        kind = "pdf" if ext in PDF_EXTS else "html" if ext in HTML_EXTS else "text"
        worker = {"pdf": extract.pdf_to_doc, "html": saved_page_to_doc, "text": text_to_doc}[kind]
        async with slots:
            try:
                res = await pool.run(worker, str(path))
            except OSError as e:
                store.record(doc_key, "failed", f"read_error:{type(e).__name__}")
                return
        text = (res.get("text") or "").strip()
        if not text:
            store.record(doc_key, "failed", res.get("error") or "no_text")
            return
        raw_path = None
        if keep_raw:
            try:  # on a thread: a large copy shouldn't hold up the other lanes
                raw_path = await asyncio.to_thread(store.save_raw, doc_key, ext or "bin", copy_from=str(path))
            except OSError as e:
                log.warning("localfile: couldn't keep a raw copy of %s: %s", path, e)
        chars = store.write_doc(doc_key, {
            "url": url, "final_url": res.get("url"), "source": f"file:{kind}", "title": res.get("title") or path.name,
            "author": res.get("author"), "date": res.get("date"), "site": res.get("site")}, text)
        ok = chars >= extract.MIN_CHARS
        store.record(doc_key, "ok" if ok else "thin", None if ok else "short", chars=chars, raw_path=raw_path)

    await asyncio.gather(*(one(key, url) for key, url in pending))
    store.commit()
