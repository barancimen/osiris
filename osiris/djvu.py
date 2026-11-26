"""DjVu files, read with DjVuLibre's command-line tools.

Most DjVu files are scanned books, and most of those carry a text layer from the OCR that made
them. djvused gives the page count and the metadata, djvutxt the text layer, and ddjvu draws a
page for OCR when there's no text layer. The tools come with DjVuLibre: `apt install
djvulibre-bin` on Debian and Ubuntu, `brew install djvulibre` on macOS. Without them a DjVu file
fails with `djvulibre_missing`, and retrying failed items reads it once they're installed.

Runs in the extraction and OCR workers. The tools only read the file, and their output comes
back on stdout, so nothing is written anywhere.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

SUFFIXES = {".djvu", ".djv"}
TOOLS = ("djvused", "djvutxt", "ddjvu")
MISSING = "djvulibre_missing"
INSTALL = ("DjVu files need DjVuLibre's tools. On Debian/Ubuntu: apt install djvulibre-bin; "
           "on macOS: brew install djvulibre")
INFO_TIMEOUT = 60  # seconds for the page count and metadata
TEXT_TIMEOUT = 300  # seconds for a whole book's text layer, which DjVuLibre reads in a few
# the ends of a column, a region and a paragraph in a text layer; a line already ends with \n
ZONE_ENDS = re.compile(r"[\x0b\x1d\x1f]")
BLANK_LINES = re.compile(r"\n{3,}")
META_LINE = re.compile(r'^(\w+)\t"(.*)"$')
UTF8 = {**os.environ, "LC_ALL": "C.UTF-8"}  # under the C locale djvused can't open a file whose name isn't ASCII


class DjvuError(Exception):
    pass


def is_djvu(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUFFIXES


def missing() -> bool:
    return not all(shutil.which(tool) for tool in TOOLS)


def _run(args: list[str], timeout: float) -> bytes:
    done = subprocess.run(args, capture_output=True, timeout=timeout, env=UTF8, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise DjvuError(done.stderr.decode("utf-8", "replace").strip()[-200:])
    return done.stdout


def _unquote(value: str) -> str:
    """A value as djvused prints it with -u: UTF-8, with C escapes for quotes, backslashes and controls."""
    return re.sub(r'\\([0-7]{1,3}|.)', lambda m: chr(int(m[1], 8)) if m[1].isdigit()
                  else {"n": "\n", "t": "\t", "r": "\r"}.get(m[1], m[1]), value)


def info(path: str) -> dict:
    """The page count, and the title and author if the file names them. Raises DjvuError if the
    file isn't DjVu or can't be opened, which djvutxt and ddjvu don't always report."""
    out = _run(["djvused", "-u", "-e", "n; print-meta", path], INFO_TIMEOUT).decode("utf-8", "replace")
    first, _, rest = out.partition("\n")
    if not first.strip().isdigit():
        raise DjvuError(f"no page count: {first[:80]!r}")
    meta = {}
    for line in rest.splitlines():
        m = META_LINE.match(line)
        if m:
            meta[m[1].lower()] = _unquote(m[2]).strip()
    return {"pages": int(first), "title": meta.get("title") or None, "author": meta.get("author") or None}


def page_text(raw: str) -> str:
    """One page of djvutxt's output as plain lines, with a blank line where a paragraph ends."""
    text = ZONE_ENDS.sub("\n", raw)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return BLANK_LINES.sub("\n\n", text).strip()


def djvu_to_doc(path: str) -> dict:
    """Every page's text layer, separated by blank lines. No text and no error means there's no
    text layer, so the file is a scan for OCR to read."""
    if missing():
        return {"text": "", "error": MISSING}
    try:
        about = info(path)
        raw = _run(["djvutxt", path], TEXT_TIMEOUT).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return {"text": "", "error": "extract_timeout"}
    except (DjvuError, OSError):
        return {"text": "", "error": "djvu_error"}
    text = "\n\n".join(p for p in map(page_text, raw.split("\f")) if p)
    if not text:
        return {"text": "", "title": about["title"], "author": about["author"]}
    title = about["title"] or next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    return {"text": text, "title": title, "author": about["author"]}


djvu_to_doc.pdf_text = True  # an OCR text layer breaks words at line ends as a PDF's does: clean.py rejoins them


def render_page(path: str, index: int, dpi: int, timeout: float) -> bytes:
    """Page `index`, counting from 0, drawn in grey at `dpi` as a PGM image."""
    image = _run(["ddjvu", "-format=pgm", f"-page={index + 1}", f"-scale={dpi}", path, "-"], timeout)
    if not image.startswith(b"P5"):
        raise DjvuError(f"no image for page {index + 1}")
    return image
