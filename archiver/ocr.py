"""`archiver ocr`: read PDFs that have no text layer, usually scans.

Separate from `archiver run` because OCR takes seconds per page: a long scan must not hold up
anything else. It works through PDFs recorded with no usable text: a local file is read where it
is and never changed, and a web PDF is downloaded again, since only its text was kept, to a
temporary file that is then deleted. Each PDF is tried once, unless --retry is given.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import uuid
from io import BytesIO
from pathlib import Path

import aiohttp

from . import extract
from .generic import PDF_CAP
from .localfile import candidate_paths
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_LANG = "eng"
DPI = 200
PAGE_TIMEOUT = 180  # seconds for one page
DOC_TIMEOUT = 3 * 3600  # a whole book; what's read by then is kept
TRIED = "ocr"


class _DocTimeout(Exception):
    pass


def _page_text(image: bytes, lang: str) -> str:
    # one thread per tesseract: the workers are the unit of parallelism
    result = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang], input=image, capture_output=True,
                            timeout=PAGE_TIMEOUT, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-200:])
    return result.stdout.decode("utf-8", "replace").replace("\f", "").strip()


def pdf_to_doc(path: str, lang: str) -> dict:
    """Runs in an OCR worker. The PDF is only opened for reading."""
    import pypdfium2 as pdfium

    def out_of_time(*_):
        raise _DocTimeout()

    def stop(*_):  # raising lets subprocess.run kill the tesseract it's waiting on
        raise SystemExit(1)

    signal.signal(signal.SIGALRM, out_of_time)
    signal.signal(signal.SIGTERM, stop)
    signal.alarm(DOC_TIMEOUT)
    pages: list[str] = []
    error = None
    pdf = None
    try:
        pdf = pdfium.PdfDocument(path)
        meta = pdf.get_metadata_dict(skip_empty=True)
        for i in range(min(len(pdf), extract.MAX_PDF_PAGES)):
            page = pdf[i]
            buffer = BytesIO()
            page.render(scale=DPI / 72, grayscale=True).to_pil().save(buffer, "PNG")
            page.close()
            try:
                pages.append(_page_text(buffer.getvalue(), lang))
            except subprocess.TimeoutExpired:
                continue  # one bad page shouldn't cost the rest of the book
    except _DocTimeout:
        error = "ocr_timeout"
        meta = {}
    except Exception as e:
        return {"text": "", "error": f"ocr_error:{type(e).__name__}:{str(e)[:80]}"}
    finally:
        signal.alarm(0)
        if pdf is not None:
            pdf.close()
    text = "\n\n".join(p for p in pages if p)
    title = meta.get("Title") or next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    return {"text": text, "title": title, "author": meta.get("Author"), "error": error}


def candidates(store: Store, retry: bool = False) -> list[tuple]:
    """(doc_key, url, final_url, lane, chars so far) for PDFs recorded with no usable text."""
    tried = "" if retry else f" AND instr(tried, ',{TRIED}') = 0"
    rows = store.db.execute(
        "SELECT doc_key, MIN(url), MAX(final_url), lane, COALESCE(MAX(chars), 0) FROM urls"
        " WHERE lane IN ('generic', 'localfile') AND ((status = 'failed' AND reason = 'no_text') OR status = 'thin')"
        f" AND (content_type = 'application/pdf' OR lower(url) LIKE '%.pdf%'){tried}"
        " GROUP BY doc_key ORDER BY MIN(rowid)"
    ).fetchall()
    return rows


async def _download(session: aiohttp.ClientSession, url: str, path: Path) -> str | None:
    """None when the PDF is on disk, else why it isn't."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"http_{resp.status}"
            size, first = 0, True
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(2**16):
                    if first and not chunk.startswith(b"%PDF"):
                        return "not_a_pdf"
                    first = False
                    size += len(chunk)
                    if size > PDF_CAP:
                        return "too_large"
                    f.write(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        return f"network:{type(e).__name__}"
    return None


async def run_ocr(store: Store, lang: str, workers: int, mem_mb: int, retry: bool, limit: int | None) -> None:
    if not shutil.which("tesseract"):
        raise SystemExit("tesseract isn't installed. On Debian/Ubuntu: apt install tesseract-ocr "
                         "(and tesseract-ocr-tur etc. for other languages); on macOS: brew install tesseract")
    todo = candidates(store, retry)[:limit]
    log.info("ocr: %d PDFs with no usable text, language %s, %d at a time", len(todo), lang, workers)
    pool = extract.WorkerPool(workers, mem_mb)
    slots = asyncio.Semaphore(workers)
    outcome: dict[str, int] = {}
    finished = 0
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}

    async def one(session: aiohttp.ClientSession, doc_key: str, url: str, final_url: str | None, lane: str, chars: int) -> None:
        nonlocal finished
        async with slots:
            temp = None
            if lane == "localfile":
                path = next((p for p in candidate_paths(url) if p.is_file()), None)
                if path is None:
                    outcome["file_missing"] = outcome.get("file_missing", 0) + 1
                    return  # not tried: it may just be on another machine
            else:
                temp = path = store.tmp_dir / f"{uuid.uuid4().hex}.pdf"
                problem = await _download(session, final_url or url, temp)
                if problem:
                    temp.unlink(missing_ok=True)
                    outcome[problem] = outcome.get(problem, 0) + 1
                    if not problem.startswith("network"):
                        store.mark_tried(doc_key, TRIED)
                    return
            try:
                res = await pool.run(pdf_to_doc, str(path), lang)
            finally:
                if temp is not None:  # only ever a download of ours, never one of your files
                    temp.unlink(missing_ok=True)
        store.mark_tried(doc_key, TRIED)
        text = (res.get("text") or "").strip()
        finished += 1
        if len(text) <= chars:
            key = res.get("error") or "no_more_text"
            outcome[key] = outcome.get(key, 0) + 1
            log.info("ocr: %d of %d, nothing new from %s", finished, len(todo), url[:90])
            store.commit()
            return
        meta = {"url": url, "final_url": final_url if final_url != url else None,
                "source": "file:pdf:ocr" if lane == "localfile" else "pdf:ocr",
                "title": res.get("title"), "author": res.get("author")}
        written = store.write_doc(doc_key, meta, text)
        ok = written >= extract.MIN_CHARS
        store.record(doc_key, "ok" if ok else "thin", None if ok else "short", chars=written)
        store.commit()
        outcome["read"] = outcome.get("read", 0) + 1
        log.info("ocr: %d of %d, %d characters from %s", finished, len(todo), written, url[:90])

    timeout = aiohttp.ClientTimeout(total=300, sock_connect=20)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            await asyncio.gather(*(one(session, *row) for row in todo))
    except asyncio.CancelledError:
        pool.kill()  # the PDF in progress is tried again next run; pool.shutdown() would wait it out
        raise
    else:
        pool.shutdown()
    finally:
        store.commit()
        for key, n in sorted(outcome.items(), key=lambda kv: -kv[1]):
            print(f"{n:>7}  {key}")
