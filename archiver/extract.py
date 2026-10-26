"""CPU-bound text extraction. Runs inside ProcessPoolExecutor workers."""

from __future__ import annotations

import ctypes
import logging
import signal

MIN_CHARS = 200  # below this a page is "thin" (JS shell, paywall, bot wall, ...)
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


def init_worker() -> None:
    # the parent handles Ctrl-C and shuts the pool down
    signal.signal(signal.SIGINT, signal.SIG_IGN)
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
