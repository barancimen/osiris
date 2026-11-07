"""clean.py: each repair, and the ways each could change what an author wrote."""

import shutil
import subprocess
from pathlib import Path

import pytest

from osiris import extract
from osiris.clean import clean, clean_text, clean_title

CHROME = "Skia/PDF m156"


def pdf(text, **kw):
    return clean_text(text, pdf=True, **kw)


def web(text):
    return clean_text(text, pdf=False)


# -- what search engines tripped over ------------------------------------------------------------

@pytest.mark.parametrize("broken, fixed", [
    ("infor￾mation; information", "information; information"),  # pdfium's printed hyphen
    ("infor\x02mation; information", "information; information"),  # older pdfium
    ("infor-\nmation; information", "information;\ninformation"),  # tesseract and most extractors
    ("infor­mation", "information"),  # soft hyphen
    ("ﬁnding", "finding"),  # ligature
    ("co​operate", "cooperate"),  # zero-width space
    ("café", "café"),  # a separated accent
])
def test_repairs(broken, fixed):
    assert pdf(broken) == fixed


def test_document_spells_it_with_a_hyphen():
    assert pdf("a well￾known fact, and another well-known one") == "a well-known fact, and another well-known one"


def test_document_spells_it_joined():
    assert pdf("infor-\nmation retrieval needs information") == "information\nretrieval needs information"


def test_unsure_keeps_the_printed_hyphen():
    assert pdf("the state￾of the art") == "the state-of the art"
    assert pdf("some foo-\nbar here") == "some foo-bar\nhere"


def test_capital_after_the_break_keeps_its_hyphen():
    assert pdf("with Anglo-\nSaxon roots") == "with Anglo-Saxon\nroots"


def test_chrome_pdfs():
    # Chrome hyphenates with U+2010; a hyphen pdfium marks U+FFFE was in the page itself
    assert pdf("infor‐\nmation here", producer=CHROME) == "information\nhere"
    assert pdf("e￾mail and email", producer=CHROME) == "e-mail and email"


def test_soft_hyphen_at_a_line_end_joins():
    assert pdf("extra­\nordinary things") == "extraordinary\nthings"


@pytest.mark.parametrize("text", [
    "from 1990-\n1995 on",  # digits
    "the end infor-\n\nmation",  # a page boundary
    "a list:\n- one\n- two",  # not between letters
    "x2infor-\nmation",  # a digit glued to the word
])
def test_breaks_left_alone(text):
    assert pdf(text) == text


def test_printed_hyphen_between_digits():
    assert pdf("1990￾1995") == "1990-1995"


def test_lexicon_decides_when_the_document_cant():
    class Lexicon:
        def __init__(self, joined, pair):
            self.joined, self.pair = joined, pair

        def counts(self, left, right):
            return self.joined, self.pair

    assert pdf("infor-\nmation", lexicon=Lexicon(50, 0)) == "information"
    assert pdf("pre-\nexisting", lexicon=Lexicon(1, 40)) == "pre-existing"
    assert pdf("pre-\nexisting", lexicon=Lexicon(10, 8)) == "pre-existing"  # no clear winner: as printed


def test_line_break_moves_after_the_word():
    once = pdf("infor-\nmation retrieval is ne-\ncessary for information that's necessary")
    assert once == "information\nretrieval is necessary\nfor information that's necessary"


def test_word_broken_twice():
    assert pdf("line one-\n  two-\nthree four") == "line one-two-three\nfour"


def test_line_break_never_lands_after_a_hyphen():
    once = pdf("infor-\nmation x- y and information")
    assert "x-\n" not in once
    assert pdf(once) == once


# -- what must survive ----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "information¹ and x² and ½",  # NFKC would make these digits
    "ＡＢＣ full width and 10 km and « French »",  # spaces and full-width letters
    "bad � and (cid:12) and \x07 and ",  # what the garbage check looks for
    "the ﬁ ligature, standing alone",  # a ligature being talked about
    "ภาษา​ไทย and 日本​語",  # zero-width space between words in scripts without spaces
    "zero‍width joiner and non‌joiner",  # needed by Persian and emoji
    "Ã© mojibake someone is asking about",
    "ordinary text with nothing to repair.\n\nTwo paragraphs.",
])
def test_left_exactly_as_written(text):
    assert pdf(text) == text
    assert web(text) == text


def test_markdown_code_is_never_touched():
    text = "ﬁne `co​de ﬁx` text\n```\nco​de ﬁx\n```\n    indented ﬁx\n~~~\nopen fence ﬁx"
    assert web(text) == "fine `co​de ﬁx` text\n```\nco​de ﬁx\n```\n    indented ﬁx\n~~~\nopen fence ﬁx"


def test_no_line_end_repairs_outside_pdfs():
    text = "a hard-wrapped plain text book where a hyphen at the end of a line is real: well-\nknown"
    assert web(text) == text


def test_byte_order_mark_at_the_start():
    assert web("﻿hello") == "hello"


def test_title():
    assert clean_title("Eﬃcient state￾of­the-art") == "Efficient state-ofthe-art"


def test_report_lists_every_line_end_decision():
    result = clean("a-\nb c-\nd e-\nf g-\nh i-\nj k-\nl", pdf=True)
    assert len(result.examples) == 6


FIXTURES = [
    "infor-\nmation retrieval is ne-\ncessary and information necessary",
    "a well￾known fact; a well-known one",
    "ab-\ncd e- f-\ngh",
    "x-\ny-\nz w￾\nv",
    "co­​operate ﬁ ﬁx `ﬁ`",
    "line one-\n  two-\nthree four",
]


@pytest.mark.parametrize("text", FIXTURES)
@pytest.mark.parametrize("is_pdf", [True, False])
def test_idempotent(text, is_pdf):
    once = clean_text(text, pdf=is_pdf)
    assert clean_text(once, pdf=is_pdf) == once


# -- a real PDF -----------------------------------------------------------------------------------

BROWSERS = ["/root/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome", shutil.which("chromium"),
            shutil.which("google-chrome")]


def test_chrome_printed_pdf(tmp_path: Path):
    chrome = next((b for b in BROWSERS if b and Path(b).exists()), None)
    if chrome is None:
        pytest.skip("no Chrome to print a PDF with")
    page = tmp_path / "page.html"
    page.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><style>p{width:9em;font:14px serif;'
        'text-align:justify}.a{hyphens:auto}</style><p class="a">Comprehensive information retrieval '
        'necessitates understanding characteristically extraordinary circumstances.</p>'
        "<p>A well-known state-of-the-art self-evident long-term approach with Anglo-Saxon roots.</p>")
    out = tmp_path / "page.pdf"
    subprocess.run([chrome, "--headless", "--no-sandbox", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                    page.as_uri()], capture_output=True, timeout=60)
    res = extract._cleaned(extract.pdf_to_doc, str(out))
    words = res["text"].split()
    for word in ("Comprehensive", "information", "necessitates", "understanding", "characteristically",
                 "extraordinary", "circumstances.", "well-known", "state-of-the-art", "self-evident", "long-term",
                 "Anglo-Saxon"):
        assert word in words, (word, res["text"])
    assert "￾" not in res["text"] and "‐" not in res["text"]
