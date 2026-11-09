"""clean.py: what it must never change, and that a second pass changes nothing more."""

import pytest

from osiris.clean import clean_text


def pdf(text, **kw):
    return clean_text(text, pdf=True, **kw)


def web(text):
    return clean_text(text, pdf=False)


@pytest.mark.parametrize("text", [
    "from 1990-\n1995 on",  # digits
    "the end infor-\n\nmation",  # a page boundary
    "a list:\n- one\n- two",  # not between letters
    "x2infor-\nmation",  # a digit glued to the word
])
def test_breaks_left_alone(text):
    assert pdf(text) == text


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
