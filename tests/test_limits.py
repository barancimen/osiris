"""Long documents are read to the end, and the status page counts Google Scholar without adding to the totals."""

import asyncio
from pathlib import Path

from osiris import extract, ocr, ui
from osiris.route import doc_id, route
from osiris.store import Store

PAGES = 450


def long_pdf(path: Path, pages: int = PAGES) -> None:
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for n in range(pages):
        page, content = 4 + 2 * n, 5 + 2 * n
        kids.append(f"{page} 0 R")
        stream = f"BT /F1 6 Tf 2 20 Td (Page{n}) Tj ET".encode()
        objects[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 40 40] /Contents {content} 0 R"
                         " /Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objects[content] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for number in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    path.write_bytes(out)


def test_every_page_of_a_long_pdf_is_read(tmp_path: Path):
    long_pdf(tmp_path / "long.pdf")
    text = extract.pdf_to_doc(str(tmp_path / "long.pdf"))["text"]
    assert f"Page{PAGES - 1}" in text and "Page0" in text


def scanned_book(tmp_path: Path, pages: int) -> tuple[Store, str]:
    pdf = tmp_path / "scan.pdf"
    long_pdf(pdf, pages)
    store = Store(tmp_path / "data")
    r = route(pdf.as_uri())
    store.add([r])
    store.record(r.doc_key, "failed", "no_text")
    store.commit()
    return store, r.doc_key


class InProcessPool:
    """The real work, without separate processes, counting how much runs at once."""
    running = most = 0

    def __init__(self, workers, mem_mb):
        pass

    async def run(self, fn, *args):
        cls = InProcessPool
        cls.running += 1
        cls.most = max(cls.most, cls.running)
        await asyncio.sleep(0.001)
        cls.running -= 1
        return extract._cleaned(fn, *args)

    def shutdown(self):
        pass

    kill = shutdown


def test_ocr_reads_every_page_in_order_using_every_core_on_a_single_book(tmp_path: Path, monkeypatch):
    store, key = scanned_book(tmp_path, 40)
    numbers = iter(range(40))
    monkeypatch.setattr(ocr, "_page_text", lambda image, lang: f"Words of a scanned page number {next(numbers)}, long enough to keep. " * 2)
    monkeypatch.setattr(extract, "WorkerPool", InProcessPool)
    InProcessPool.most = 0
    asyncio.run(ocr.run_ocr(store, "eng", 4, 0))
    meta, body = store.read_doc(doc_id(key))
    store.close()
    assert InProcessPool.most == 4  # one PDF, and still four cores busy
    assert body.count("Words of a scanned page") == 80 and body.index("number 0,") < body.index("number 39,")
    assert meta["source"] == "file:pdf:ocr"


def test_one_bad_ocr_page_does_not_cost_the_book(tmp_path: Path, monkeypatch):
    store, key = scanned_book(tmp_path, 6)

    def page_text(image, lang, seen=[]):
        seen.append(1)
        if len(seen) == 3:
            raise RuntimeError("tesseract fell over")
        return "A perfectly ordinary scanned page of words that came out fine. " * 2

    monkeypatch.setattr(ocr, "_page_text", page_text)
    monkeypatch.setattr(extract, "WorkerPool", InProcessPool)
    asyncio.run(ocr.run_ocr(store, "eng", 2, 0))
    body = store.read_doc(doc_id(key))[1]
    store.close()
    assert body.count("perfectly ordinary") == 10


def test_ocr_stops_a_document_once_its_time_is_used_up_and_keeps_what_it_read(tmp_path: Path, monkeypatch):
    store, key = scanned_book(tmp_path, 8)
    monkeypatch.setattr(ocr, "_page_text", lambda image, lang: "A perfectly ordinary scanned page of words that came out fine. " * 2)
    monkeypatch.setattr(extract, "WorkerPool", InProcessPool)
    monkeypatch.setattr(ocr, "DOC_TIMEOUT", -1)
    monkeypatch.setattr(ocr, "DOC_SECONDS_PER_PAGE", 0)
    asyncio.run(ocr.run_ocr(store, "eng", 1, 0))
    body = store.read_doc(doc_id(key))[1]
    store.close()
    assert 1 <= body.count("perfectly ordinary") / 2 < 8


def test_web_pdfs_at_once_follows_the_cores():
    assert "--pdf-at-once" in ui._run_args({"lanes": list(ui.LANES), "pdf_at_once": 9}, Path("urls"))
    assert "--pdf-at-once" not in ui._run_args({"lanes": list(ui.LANES), "pdf_at_once": ""}, Path("urls"))


def test_running_out_of_memory_fails_only_that_file():
    def too_big(*_):
        raise MemoryError
    assert extract._cleaned(too_big) == {"text": "", "error": "too_large"}


def test_scholar_row_is_separate_from_the_lane_counts(tmp_path: Path):
    store = Store(tmp_path)
    store.add([route("https://scholar.google.com/scholar?q=a"), route("https://scholar.google.de/citations?user=b"),
               route("https://example.com/page")])
    store.commit()
    table = ui._lane_table(tmp_path)
    store.close()
    assert table["scholar"] == {"pending": 2}
    assert sum(sum(c.values()) for c in table["lanes"].values()) == 3
