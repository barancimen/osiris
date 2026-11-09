"""Long documents are read to the end, and the status page counts Google Scholar without adding to the totals."""

from pathlib import Path

from osiris import extract, ocr, ui
from osiris.route import route
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


def test_ocr_goes_through_every_page(tmp_path: Path, monkeypatch):
    long_pdf(tmp_path / "long.pdf")
    monkeypatch.setattr(ocr, "_page_text", lambda image, lang: "words")
    res = ocr.pdf_to_doc(str(tmp_path / "long.pdf"), "eng")
    assert res["text"].count("words") == PAGES


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
