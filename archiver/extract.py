"""CPU-bound text extraction. Runs inside ProcessPoolExecutor workers."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import resource
import signal
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

log = logging.getLogger(__name__)

MIN_CHARS = 200  # below this a page is "thin" (JS shell, paywall, bot wall, ...)
RECYCLE_AFTER = 500  # tasks per worker generation: freed address space never shrinks otherwise
MAX_PDF_PAGES = 400
TASK_TIMEOUT = 45  # seconds; a pathological document must not wedge the only worker

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:  # not glibc
    _libc = None


class ExtractionTimeout(Exception):
    pass


def _release_memory() -> None:
    """Hand freed heap back to the OS. Without this the worker keeps its high-water mark
    (~475 MB after a large PDF, ~240 MB after big HTML pages) for the rest of the run."""
    if _libc is not None:
        _libc.malloc_trim(0)


def _on_alarm(signum, frame):
    raise ExtractionTimeout()


def init_worker(mem_mb: int = 0) -> None:
    # the parent handles Ctrl-C and shuts the pool down
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mem_mb:
        # a pathological document then fails with MemoryError instead of swapping the machine
        cap = mem_mb * 2**20
        resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))
    signal.signal(signal.SIGALRM, _on_alarm)
    for name in ("trafilatura", "htmldate", "courlan", "justext", "charset_normalizer", "readability"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def _trafilatura(data: bytes, url: str, fast: bool):
    import trafilatura

    return trafilatura.bare_extraction(
        data,
        url=url,
        output_format="python",
        include_formatting=True,  # markdown headings, lists, tables, emphasis
        with_metadata=True,
        fast=fast,
        include_comments=False,
        include_tables=True,
        include_links=False,
        include_images=False,
    )


def html_to_doc(data: bytes, url: str) -> dict:
    """Main-content markdown + metadata. Falls back to slower extractors only for thin pages."""
    signal.alarm(TASK_TIMEOUT)
    try:
        doc = _trafilatura(data, url, fast=True)
        if doc is None or len(doc.text or "") < MIN_CHARS:
            slow = _trafilatura(data, url, fast=False)
            if slow is not None and len(slow.text or "") > len((doc and doc.text) or ""):
                doc = slow
    except ExtractionTimeout:
        return {"text": "", "error": "extract_timeout"}
    except Exception as e:  # malformed HTML can raise from deep inside lxml
        return {"text": "", "error": f"extract_error:{type(e).__name__}"}
    finally:
        signal.alarm(0)
        _release_memory()
    if doc is None:
        return {"text": ""}
    return {
        "text": doc.text or "",
        "title": doc.title,
        "author": doc.author,
        "date": doc.date,
        "site": doc.sitename,
    }


def pdf_to_doc(path: str) -> dict:
    """Plain text of every page, separated by blank lines."""
    import pypdfium2 as pdfium

    signal.alarm(TASK_TIMEOUT)
    pdf = None
    try:
        pdf = pdfium.PdfDocument(path)
        pages = []
        for i in range(min(len(pdf), MAX_PDF_PAGES)):
            page = pdf[i]
            textpage = page.get_textpage()
            pages.append(textpage.get_text_range().replace("\r\n", "\n").strip())
            textpage.close()
            page.close()
        meta = pdf.get_metadata_dict(skip_empty=True)
    except ExtractionTimeout:
        return {"text": "", "error": "extract_timeout"}
    except Exception as e:  # encrypted, truncated or corrupt PDFs
        return {"text": "", "error": f"pdf_error:{type(e).__name__}"}
    finally:
        signal.alarm(0)
        if pdf is not None:
            pdf.close()
        _release_memory()
    text = "\n\n".join(p for p in pages if p)
    title = meta.get("Title") or next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    return {"text": text, "title": title, "author": meta.get("Author")}


class WorkerPool:
    """Extraction processes that survive their workers. A worker that dies (OOM killer,
    memory cap, a crash inside lxml or pdfium) breaks the whole executor and every task in
    flight on it, so the pool is rebuilt and each of those tasks is retried alone in a
    one-off process: only the document that actually crashes is lost. Workers are also
    replaced every RECYCLE_AFTER tasks."""

    def __init__(self, workers: int, mem_mb: int = 0):
        self.workers = workers
        self.mem_mb = mem_mb
        self.executor = self._new(workers)
        self.submitted = 0

    def _new(self, workers: int) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=workers, initializer=init_worker, initargs=(self.mem_mb,))

    def _replace(self, old: ProcessPoolExecutor, cancel: bool) -> None:
        if old is self.executor:  # several failing tasks race to replace the same executor
            self.executor = self._new(self.workers)
            self.submitted = 0
            old.shutdown(wait=False, cancel_futures=cancel)

    async def run(self, fn, *args) -> dict:
        loop = asyncio.get_running_loop()
        if self.submitted >= RECYCLE_AFTER * self.workers:
            self._replace(self.executor, cancel=False)  # in-flight tasks still finish
        executor = self.executor
        self.submitted += 1
        try:
            return await loop.run_in_executor(executor, fn, *args)
        except BrokenProcessPool:
            if executor is self.executor:
                log.warning("an extraction worker died; restarting the pool")
            self._replace(executor, cancel=True)
        solo = self._new(1)
        try:
            return await loop.run_in_executor(solo, fn, *args)
        except BrokenProcessPool:
            log.warning("a document crashed its extraction worker twice; skipping it")
            return {"text": "", "error": "extract_crashed"}
        finally:
            solo.shutdown(wait=False)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    def kill(self) -> None:
        """Stop now, for work too long to wait for: shutdown() lets the running task finish."""
        for process in list((self.executor._processes or {}).values()):
            process.terminate()
        self.executor.shutdown(wait=False, cancel_futures=True)
