"""DjVu files on this computer: their text layer, OCR when they have none, and what happens without DjVuLibre."""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from osiris import clean, djvu, extract, ocr
from osiris.localfile import run_localfile
from osiris.route import Route, doc_id, route
from osiris.store import Store

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("cjb2", "djvm", *djvu.TOOLS)),
                                reason="needs DjVuLibre's tools, which also make the test files")

PAGE_ONE = '''(page 0 0 600 300
 (column 0 0 600 300
  (region 0 0 600 300
   (para 0 150 600 300
    (line 10 250 590 290 (word 10 250 100 290 "A") (word 110 250 300 290 "long") (word 310 250 590 290 "docu-"))
    (line 10 200 590 240 (word 10 200 100 240 "ment") (word 110 200 590 240 "here.")))
   (para 0 0 600 150
    (line 10 100 590 140 (word 10 100 300 140 "Second") (word 310 100 590 140 "paragraph."))))))'''
PAGE_TWO = '(page 0 0 600 300 (line 10 250 590 290 (word 10 250 300 290 "Page") (word 310 250 590 290 "two.")))'


def make_djvu(path: Path, widths: list[int], texts: list[str] | None = None, meta: str | None = None) -> Path:
    """A bundled DjVu file of blank pages this wide, with these text layers and this metadata. It's
    made under a plain name, since djvused scripts split file names at spaces, and then renamed."""
    work = path.parent / f"parts-{len(list(path.parent.glob('parts-*')))}"
    work.mkdir()
    made = work / "made.djvu"
    pages = []
    for n, width in enumerate(widths):
        Image.new("1", (width, 300), 1).save(work / f"{n}.pbm")
        subprocess.run(["cjb2", "-dpi", "100", work / f"{n}.pbm", work / f"{n}.djvu"], check=True)
        pages.append(work / f"{n}.djvu")
    subprocess.run(["djvm", "-c", made, *pages], check=True)
    script = []
    for n, text in enumerate(texts or []):
        (work / f"{n}.txt").write_text(text, encoding="utf-8")
        script.append(f"select {n + 1}; set-txt {work / f'{n}.txt'}")
    if meta:
        (work / "meta.txt").write_text(meta, encoding="utf-8")
        script.append(f"select; set-meta {work / 'meta.txt'}")
    if script:
        subprocess.run(["djvused", made, "-e", "; ".join(script), "-s"], check=True)
    made.rename(path)
    return path


def test_the_text_layer_is_read_page_by_page_with_the_files_title_and_author(tmp_path: Path):
    path = make_djvu(tmp_path / "book.djvu", [600, 600], [PAGE_ONE, PAGE_TWO],
                     meta='Title\t"Türkçe \\"kitap\\""\nAuthor\t"Ada Lovelace"\n')
    res = extract._cleaned(djvu.djvu_to_doc, str(path))  # with the repairs the worker pool makes
    assert res["title"] == 'Türkçe "kitap"' and res["author"] == "Ada Lovelace"
    text = res["text"]
    assert not any(c in text for c in "\x0b\x0c\x1d\x1f")  # the ends of columns, regions, paragraphs and pages
    assert "docu-ment" in text  # the PDF repairs moved the line-end hyphen, as for a PDF's text layer
    assert text.index("docu-ment") < text.index("\n\nSecond paragraph.") < text.index("\n\nPage two.")
    assert clean.is_pdf_source("file:djvu") and clean.is_pdf_source("file:djvu:ocr")


def test_a_name_that_isnt_ascii_is_read_even_under_the_c_locale(tmp_path: Path):
    path = make_djvu(tmp_path / "Türkçe kitap #1 100%.djvu", [600], [PAGE_TWO])
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("LC_")}, "LC_ALL": "C", "LANG": "C"}
    done = subprocess.run([sys.executable, "-c", "import sys; from osiris import djvu; print(djvu.djvu_to_doc(sys.argv[1])['text'])",
                           str(path)], capture_output=True, text=True, env=env, timeout=60)
    assert done.stdout.strip() == "Page two.", done.stderr


def test_a_broken_file_and_a_missing_djvulibre_each_say_why(tmp_path: Path, monkeypatch):
    (tmp_path / "broken.djvu").write_bytes(b"AT&TFORM not really")
    assert djvu.djvu_to_doc(str(tmp_path / "broken.djvu")) == {"text": "", "error": "djvu_error"}
    monkeypatch.setattr(djvu, "missing", lambda: True)
    assert djvu.djvu_to_doc(str(tmp_path / "broken.djvu")) == {"text": "", "error": "djvulibre_missing"}


class InProcessPool:
    def __init__(self, *args):
        pass

    async def run(self, fn, *args):
        return extract._cleaned(fn, *args)

    def shutdown(self):
        pass

    kill = shutdown


def width_of(image: bytes, lang: str, orient: bool = False) -> str:
    """Stands in for tesseract: says how wide the page it was given is."""
    width = int(image.split(b"\n")[1].split()[0])
    return f"A page {width} pixels wide, with enough words on it to be kept."


def scan_in_store(tmp_path: Path, widths: list[int]) -> tuple[Store, str]:
    path = make_djvu(tmp_path / "scan.djvu", widths)
    store = Store(tmp_path / "data")
    r = route(path.as_uri())
    assert r.lane == "localfile"
    store.add([r])
    store.commit()
    return store, r.doc_key


def test_a_scan_without_a_text_layer_is_read_with_ocr_page_by_page_in_order(tmp_path: Path, monkeypatch):
    store, key = scan_in_store(tmp_path, [300, 500, 400])
    web = Route("https://example.com/scan.djvu", "generic", doc_key="https://example.com/scan.djvu")
    store.add([web])
    store.record(web.doc_key, "failed", "no_text")  # OCR only draws DjVu files that are on this computer
    asyncio.run(run_localfile(store, InProcessPool(), 1))
    assert store.db.execute("SELECT status, reason FROM urls WHERE doc_key = ?", (key,)).fetchone() == ("failed", "no_text")
    assert [row[0] for row in ocr.candidates(store)] == [key]
    monkeypatch.setattr(ocr, "_page_text", width_of)
    monkeypatch.setattr(extract, "WorkerPool", InProcessPool)
    asyncio.run(ocr.run_ocr(store, "eng", 2, 0))
    meta, body = store.read_doc(doc_id(key))
    store.close()
    assert meta["source"] == "file:djvu:ocr"
    widths = [int(line.split()[2]) for line in body.split("\n\n")]  # the pages are drawn at 200 dpi, from 100
    assert widths == [600, 1000, 800]


def test_without_djvulibre_ocr_leaves_a_scan_its_try(tmp_path: Path, monkeypatch):
    store, key = scan_in_store(tmp_path, [300])
    store.record(key, "failed", "no_text")
    store.commit()
    monkeypatch.setattr(djvu, "missing", lambda: True)
    monkeypatch.setattr(extract, "WorkerPool", InProcessPool)
    asyncio.run(ocr.run_ocr(store, "eng", 1, 0))
    assert [row[0] for row in ocr.candidates(store)] == [key]
    store.close()


def test_a_djvu_file_an_earlier_version_skipped_is_read_on_the_next_run(tmp_path: Path):
    path = make_djvu(tmp_path / "old.djvu", [600], [PAGE_TWO])
    store = Store(tmp_path / "data")
    r = route(path.as_uri())
    store.add([Route(r.url, "skip", reason="local_other", dedup_key=r.dedup_key)])  # as it was recorded before
    store.commit()
    assert store.reroute(route) == 1
    asyncio.run(run_localfile(store, InProcessPool(), 1))
    meta, body = store.read_doc(doc_id(r.doc_key))
    store.close()
    assert meta["source"] == "file:djvu" and body.endswith("Page two.")
