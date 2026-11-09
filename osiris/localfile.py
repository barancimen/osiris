"""Local files lane: file:// links from your bookmarks and tabs, and lists you make yourself.

The originals are only ever opened for reading. Unlike a downloaded PDF, which lives in the
output directory's tmp/ and is deleted after extraction, a local file is yours: this module
never moves, renames, rewrites or deletes one, and never copies one under raw/.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import documents, extract
from .singlefile import saved_page_to_doc
from .store import REFRESH, Store

log = logging.getLogger(__name__)


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
        data = f.read()
    return {"text": extract.decode_text(data)}


# extension -> (name used in `source`, reader); route.LOCAL_READABLE lists the same extensions
READERS = {
    "pdf": ("pdf", extract.pdf_to_doc),
    **dict.fromkeys(("html", "htm", "xhtml"), ("html", saved_page_to_doc)),
    **dict.fromkeys(("txt", "md", "markdown", "rst", "tex", "org"), ("text", text_to_doc)),
    "docx": ("docx", documents.docx_to_doc),
    "epub": ("epub", documents.epub_to_doc),
    **dict.fromkeys(("mobi", "azw", "azw3"), ("kindle", documents.kindle_to_doc)),
}


async def run_localfile(store: Store, pool: extract.WorkerPool, workers: int,
                        refresh: list[tuple[str, str, int]] = ()) -> None:
    pending = store.pending("localfile")
    # (doc_key, url, chars to beat): a refreshed document keeps its text unless the new read has more
    todo = [(key, url, None) for key, url in pending]
    if refresh:
        todo += refresh
        log.info("localfile: refreshing %d PDFs that have text; each keeps its text unless the new read has more",
                 len(refresh))
    if todo:
        log.info("localfile: %d local files", len(todo))
    slots = asyncio.Semaphore(workers + 1)

    async def one(doc_key: str, url: str, keep: int | None) -> None:
        await read_one(doc_key, url, keep)
        if keep is not None:
            store.mark_tried(doc_key, REFRESH)

    async def read_one(doc_key: str, url: str, keep: int | None) -> None:
        def fail(reason: str) -> None:
            if keep is None:
                store.record(doc_key, "failed", reason)

        path = next((p for p in candidate_paths(url) if p.is_file()), None)
        if path is None:
            fail("file_missing")  # --retry-failed checks again
            return
        if not os.access(path, os.R_OK):
            fail("file_unreadable")
            return
        ext = path.suffix.lower().lstrip(".")
        kind, worker = READERS[ext]
        async with slots:
            try:
                res = await pool.run(worker, str(path))
            except OSError as e:
                fail(f"read_error:{type(e).__name__}")
                return
        text = (res.get("text") or "").strip()
        if not text:
            fail(res.get("error") or "no_text")
            return
        if keep is not None and len(text) <= keep:
            return
        chars = store.write_doc(doc_key, {
            "url": url, "final_url": res.get("url"), "source": f"file:{kind}", "title": res.get("title") or path.name,
            "author": res.get("author"), "date": res.get("date"), "site": res.get("site")}, text)
        ok = chars >= extract.MIN_CHARS
        store.record(doc_key, "ok" if ok else "thin", None if ok else "short", chars=chars)

    await asyncio.gather(*(one(*item) for item in todo))
    store.commit()
