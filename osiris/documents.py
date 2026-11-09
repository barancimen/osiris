"""Text of ebooks and Word documents: EPUB, DOCX, MOBI, AZW and AZW3.

Same recipe as Microsoft's MarkItDown: mammoth turns a DOCX into HTML, an EPUB is walked in
reading order from its spine, and markdownify turns the HTML into markdown. Main-content
extraction is deliberately not used, because a book has no boilerplate to cut and that
heuristic could drop whole chapters. Kindle files are unpacked with KindleUnpack (the `mobi`
package) into a temporary directory, and then read as the EPUB or HTML they contain.

Runs in the extraction workers. Input files are only ever opened for reading. Books under DRM
are reported as `drm_protected`, not worked around.
"""

from __future__ import annotations

import contextlib
import glob
import io
import os
import posixpath
import re
import signal
import tempfile
import zipfile
from urllib.parse import unquote

from . import extract

TIMEOUT = 300  # seconds per book; well above TASK_TIMEOUT because these are whole books
DOCX_META = {"title": "title", "creator": "author", "modified": "date"}
XML_PARSER_OPTIONS = {"recover": True, "resolve_entities": False, "no_network": True, "huge_tree": True}


def html_to_markdown(html: bytes | str) -> str:
    """All the text in the page, with headings, lists and tables. Links keep only their text."""
    import warnings

    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    from markdownify import MarkdownConverter

    with warnings.catch_warnings():  # EPUB chapters are XHTML; the lenient HTML parser is what we want
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for el in soup(["head", "script", "style", "svg", "img"]):
        el.decompose()
    text = MarkdownConverter(heading_style="ATX", bullets="-", strip=["a"]).convert_soup(soup)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _xml(data: bytes):
    from lxml import etree

    return etree.fromstring(data, parser=etree.XMLParser(**XML_PARSER_OPTIONS))


def _local(el) -> str:
    return el.tag.rpartition("}")[2] if isinstance(el.tag, str) else ""


def _opf_metadata(root) -> dict:
    found: dict = {}
    for el in root.iter():
        name, text = _local(el), (el.text or "").strip()
        if not text:
            continue
        if name == "title":
            found.setdefault("title", text)
        elif name == "creator":
            found["author"] = f"{found['author']}, {text}" if "author" in found else text
        elif name == "date":
            found.setdefault("date", text[:10])
    return found


def _resolve(z: zipfile.ZipFile, base: str, href: str) -> str | None:
    """Manifest hrefs are percent-encoded relative to the OPF; zip entry names aren't."""
    names = set(z.namelist())
    for candidate in (unquote(href), href):
        full = posixpath.normpath(posixpath.join(base, candidate))
        if full in names:
            return full
    return None


def _read_epub(path: str) -> dict:
    with zipfile.ZipFile(path) as z:
        opf_path = next(el.get("full-path") for el in _xml(z.read("META-INF/container.xml")).iter()
                        if _local(el) == "rootfile")
        opf = _xml(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        items = {el.get("id"): el for el in opf.iter() if _local(el) == "item"}
        spine = []
        for el in opf.iter():
            item = items.get(el.get("idref")) if _local(el) == "itemref" else None
            if item is not None and "html" in (item.get("media-type") or "html"):
                if name := _resolve(z, base, item.get("href") or ""):
                    spine.append(name)
        if not spine:  # a malformed OPF: file order is better than nothing
            spine = sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
        if "META-INF/encryption.xml" in z.namelist():  # font obfuscation lives here too, so look at what's listed
            locked = {unquote(el.get("URI") or "") for el in _xml(z.read("META-INF/encryption.xml")).iter()
                      if _local(el) == "CipherReference"}
            if locked.intersection(spine):
                return {"text": "", "error": "drm_protected"}
        chapters = [html_to_markdown(z.read(name)) for name in spine]
    return {"text": "\n\n".join(c for c in chapters if c), **_opf_metadata(opf)}


@contextlib.contextmanager
def _limits():
    signal.alarm(TIMEOUT)
    try:
        yield
    finally:
        signal.alarm(0)
        extract._release_memory()


def epub_to_doc(path: str) -> dict:
    with _limits():
        try:
            return _read_epub(path)
        except extract.ExtractionTimeout:
            return {"text": "", "error": "extract_timeout"}
        except Exception as e:  # truncated zip, missing container.xml, broken XML, ...
            return {"text": "", "error": f"epub_error:{type(e).__name__}"}


def docx_to_doc(path: str) -> dict:
    import mammoth

    # images would otherwise be embedded in the HTML as data URIs
    no_images = mammoth.images.img_element(lambda image: {"src": ""})
    with _limits():
        try:
            with open(path, "rb") as f:
                html = mammoth.convert_to_html(f, convert_image=no_images).value
            meta: dict = {}
            with zipfile.ZipFile(path) as z, contextlib.suppress(KeyError):
                for el in _xml(z.read("docProps/core.xml")).iter():
                    key = DOCX_META.get(_local(el))
                    if key and (el.text or "").strip():
                        meta[key] = el.text.strip()[:10] if key == "date" else el.text.strip()
            return {"text": html_to_markdown(html), **meta}
        except extract.ExtractionTimeout:
            return {"text": "", "error": "extract_timeout"}
        except Exception as e:  # not really a DOCX, password-protected, truncated, ...
            return {"text": "", "error": f"docx_error:{type(e).__name__}"}


def kindle_to_doc(path: str) -> dict:
    """MOBI, AZW and AZW3. Newer files unpack to an EPUB, older ones to one HTML file."""
    from mobi.kindleunpack import unpackBook

    with _limits():
        try:
            with tempfile.TemporaryDirectory(prefix="osiris-kindle-") as tmp:
                with contextlib.redirect_stdout(io.StringIO()):  # it narrates every step
                    unpackBook(path, tmp, epubver="A")
                epubs = glob.glob(os.path.join(glob.escape(tmp), "mobi8", "*.epub"))
                if epubs:
                    return _read_epub(epubs[0])
                book = os.path.join(tmp, "mobi7", "book.html")
                if os.path.exists(book):
                    with open(book, "rb") as f:
                        text = html_to_markdown(f.read())
                    meta: dict = {}
                    for opf in glob.glob(os.path.join(glob.escape(tmp), "mobi7", "*.opf")):
                        with open(opf, "rb") as f:
                            meta = _opf_metadata(_xml(f.read()))
                    return {"text": text, **meta}
                pdfs = glob.glob(os.path.join(glob.escape(tmp), "*.pdf"))  # Print Replica
                return extract.pdf_to_doc(pdfs[0]) if pdfs else {"text": "", "error": "kindle_no_content"}
        except extract.ExtractionTimeout:
            return {"text": "", "error": "extract_timeout"}
        except Exception as e:
            if "encrypted" in str(e).lower():
                return {"text": "", "error": "drm_protected"}
            return {"text": "", "error": f"kindle_error:{type(e).__name__}"}
