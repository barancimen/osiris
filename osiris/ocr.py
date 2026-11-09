"""Read PDFs that have no text layer, usually scans.

`osiris run` does this on its own once its generic and local-file lanes are done, and `osiris ocr`
does it alone. OCR takes seconds per page, so a long scan must not hold up anything else. It works
through PDFs recorded with no usable text: a local file is read where it is and never changed, and
a web PDF is downloaded again, since only its text was kept, to a temporary file that is then
deleted. Each PDF is tried once, unless --retry is given.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import uuid
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path

import aiohttp

from . import extract, review
from .localfile import candidate_paths
from .route import doc_id
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_LANG = "eng"
DPI = 200
PAGE_TIMEOUT = 180  # seconds for one page
DOC_TIMEOUT = 3 * 3600  # a whole book; what's read by then is kept
DOC_SECONDS_PER_PAGE = 30  # a book too long for DOC_TIMEOUT gets this long for each page
TRIED = "ocr"
LANES = ("generic", "localfile")  # the lanes whose PDFs can come back with no text


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
        signal.alarm(max(DOC_TIMEOUT, len(pdf) * DOC_SECONDS_PER_PAGE))
        for i in range(len(pdf)):
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


pdf_to_doc.pdf_text = True  # tesseract breaks words at line ends like a typesetter: clean.py rejoins them


def unavailable(lang: str) -> str | None:
    """Why OCR can't run in these languages, if so. Checked up front because a page that fails for
    this reason would otherwise count as the PDF's one try."""
    if not shutil.which("tesseract"):
        return ("tesseract isn't installed. On Debian/Ubuntu: apt install tesseract-ocr "
                "(and tesseract-ocr-tur etc. for other languages); on macOS: brew install tesseract")
    listed = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    have = set((listed.stdout + listed.stderr).split())
    missing = [code for code in lang.split("+") if code not in have]
    if missing:
        return (f"tesseract has no language pack for {', '.join(missing)}. On Debian/Ubuntu: "
                + " ".join(f"apt install tesseract-ocr-{code}" for code in missing))
    return None


def candidates(store: Store, retry: bool = False, lanes: Iterable[str] = LANES) -> list[tuple]:
    """(doc_key, url, final_url, lane, chars so far) for PDFs recorded with no usable text."""
    tried = "" if retry else f" AND instr(tried, ',{TRIED}') = 0"
    lanes = tuple(lanes)
    rows = store.db.execute(
        "SELECT doc_key, MIN(url), MAX(final_url), lane, COALESCE(MAX(chars), 0) FROM urls"
        f" WHERE lane IN ({', '.join('?' * len(lanes))})"
        " AND ((status = 'failed' AND reason = 'no_text') OR status = 'thin')"
        f" AND (content_type = 'application/pdf' OR lower(url) LIKE '%.pdf%'){tried}"
        " GROUP BY doc_key ORDER BY MIN(rowid)",
        lanes,
    ).fetchall()
    return rows


def forced_rows(store: Store, keys: list[str]) -> list[tuple]:
    """The same row as candidates(), for PDFs you've chosen to OCR whatever their status."""
    rows = []
    for key in keys:
        row = store.db.execute(
            "SELECT doc_key, MIN(url), MAX(final_url), lane, COALESCE(MAX(chars), 0) FROM urls WHERE doc_key = ?"
            " AND lane IN ('generic', 'localfile') GROUP BY doc_key", (key,)
        ).fetchone()
        if row:
            rows.append(row)
    return rows


async def _download(session: aiohttp.ClientSession, url: str, path: Path) -> str | None:
    """None when the PDF is on disk, else why it isn't."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"http_{resp.status}"
            first = True
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(2**16):
                    if first and not chunk.startswith(b"%PDF"):
                        return "not_a_pdf"
                    first = False
                    f.write(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        return f"network:{type(e).__name__}"
    return None


async def run_ocr(store: Store, lang: str, workers: int, mem_mb: int, retry: bool = False, limit: int | None = None,
                  lanes: Iterable[str] = LANES, forced: list[str] | None = None) -> None:
    """With `forced` (doc keys), only those are read, and the result replaces the text when it scores
    better rather than when it's longer: a garbage text layer can be longer than the truth."""
    todo = forced_rows(store, forced) if forced is not None else candidates(store, retry, lanes)[:limit]
    log.info("ocr: %d PDFs with no usable text, language %s, %d at a time", len(todo), lang, workers)
    if not todo:
        return
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
        if forced is not None:
            old = store.read_doc(doc_id(doc_key))
            improved = bool(text) and (old is None or review.score_text(text, lane) < review.score_text(old[1], lane))
        else:
            improved = len(text) > chars
        if not improved:
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
