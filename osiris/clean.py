"""Repair what converting a document to text broke, and nothing its author wrote.

Every change here has exactly one right answer, or is decided by evidence, and anything unsure is
left as it was printed:

- Every source: separated accents are joined to their letter (NFC: the text looks and means the
  same), a ligature inside a word is spelled out (ﬁnding -> finding), and an invisible character
  inside a word is removed (soft hyphen, zero-width space, word joiner, byte-order mark), but only
  between letters of scripts that put spaces between words: in Thai, Khmer or Japanese a zero-width
  space marks where a word ends. Code in markdown (fenced, indented or inline) is never touched.
- PDFs and OCR only: words broken at the end of a line are rejoined, or keep their hyphen (see
  `_decide`). Plain-text files are left alone, since books like Project Gutenberg's wrap lines at
  spaces and every hyphen at a line end in them is real.

Nothing else: no spelling or OCR correction, no case or accent folding, no change to spaces of any
kind, full-width letters or mojibake, and the damage markers the garbage check looks for (U+FFFD,
(cid:N), control characters) stay. Cleaning twice gives the same text as cleaning once.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

LEXICON_FILE = "lexicon.sqlite"
LEXICON_ENV = "OSIRIS_LEXICON"  # extraction workers find the archive's lexicon here

LETTER = r"[^\W\d_]"
# scripts that put spaces between words: an invisible character between two of their letters only
# breaks a word apart
SPACED = ("A-Za-zªµºÀ-ÖØ-öø-ʯͰ-ͳͶͷͻ-ͽ"
          "ΆΈ-ϿЀ-ҁҊ-ԯḀ-῿")
INVISIBLE = "­​⁠﻿"  # soft hyphen, zero-width space, word joiner, BOM / zero-width no-break space
INSIDE_WORD = re.compile(f"(?<=[{SPACED}])[{INVISIBLE}]+(?=[{SPACED}])")
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
# a ligature next to a letter is part of a word; one standing alone is being talked about
LIGATURE = re.compile(f"(?<={LETTER})[ﬀ-ﬆ]|[ﬀ-ﬆ](?={LETTER})")
# markdown code: a fenced block (to its closing fence or the end), an indented line, an inline span
CODE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n.*?(?:^\1[ \t]*$|\Z)|^(?: {4}|\t)[^\n]*$|(`+)(?!`)[^\n]+?(?<!`)\2(?!`)",
                  re.M | re.S)

# where pdfium and tesseract break a word at a line end. pdfium writes a hyphen it printed at a line
# end as U+FFFE (\x02 in older builds) and drops the line break; any other hyphen stays, followed
# by the line break. Two line breaks are a page boundary, never crossed
HYPHENS = "-‐‑"
BREAK = re.compile(rf"(?<!\w)({LETTER}+)(?:([￾\x02])(?:\n[ \t]*)?|([{HYPHENS}­])\n[ \t]*)({LETTER}+)(?!\w)")
WORD = re.compile(f"{LETTER}+")
PAIR = re.compile(rf"(?<!\w)({LETTER}+)[{HYPHENS}](?=({LETTER}+)(?!\w))")
TRIGGER = re.compile(f"[{INVISIBLE}ﬀ-ﬆ￾\x02]|[{HYPHENS}]\n")
CLEAR = 3  # one form must be this many times as common as the other to win
EXAMPLES = 5  # report rows kept per rule and document, for the rules that fire often


@dataclass
class Result:
    text: str
    counts: Counter = field(default_factory=Counter)
    # (rule, before, after, context): every line-end decision, and a few examples of the rest
    examples: list[tuple[str, str, str, str]] = field(default_factory=list)

    def note(self, rule: str, before: str, after: str, context: str, always: bool = False) -> None:
        self.counts[rule] += 1
        if always or self.counts[rule] <= EXAMPLES:
            self.examples.append((rule, before, after, context))


def is_pdf_source(source: str | None) -> bool:
    """Text from a PDF's text layer or from OCR of one: pdf, pdf:wayback, file:pdf, pdf:ocr, ..."""
    return bool(source) and (source.startswith("pdf") or source.startswith("file:pdf"))


def from_chrome(producer: str | None) -> bool:
    """A page Chrome printed to PDF. Chrome hyphenates with U+2010, so in its PDFs a hyphen
    pdfium marks as U+FFFE is one the page itself had."""
    return bool(producer) and producer.startswith("Skia/PDF")


def _context(text: str, start: int, end: int) -> str:
    return text[max(start - 40, 0):end + 40].replace("\n", "⏎")


# -- every source -------------------------------------------------------------------------------

def _universal(text: str, result: Result) -> str:
    if not unicodedata.is_normalized("NFC", text):
        text = unicodedata.normalize("NFC", text)
        result.counts["accents joined (NFC)"] += 1

    def ligature(m: re.Match) -> str:
        result.note("ligature spelled out", m.group(), LIGATURES[m.group()], _context(m.string, m.start(), m.end()))
        return LIGATURES[m.group()]

    def invisible(m: re.Match) -> str:
        names = "+".join(f"U+{ord(c):04X}" for c in m.group())
        result.note("invisible character removed", names, "", _context(m.string, m.start(), m.end()))
        return ""

    return INSIDE_WORD.sub(invisible, LIGATURE.sub(ligature, text))


def _outside_code(text: str, fn) -> str:
    out, pos = [], 0
    for m in CODE.finditer(text):
        out += [fn(text[pos:m.start()]), m.group()]
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


# -- PDFs and OCR: words broken at line ends ----------------------------------------------------

@dataclass
class _Evidence:
    words: Counter
    pairs: Counter

    @classmethod
    def of(cls, text: str) -> _Evidence:
        """How the document spells words outside its line-end breaks."""
        rest = BREAK.sub(" ", text)
        return cls(Counter(w.lower() for w in WORD.findall(rest)),
                   Counter((a.lower(), b.lower()) for a, b in PAIR.findall(rest)))


def _verdict(joined: int, hyphenated: int, least: int) -> str | None:
    if joined >= least and joined >= CLEAR * hyphenated:
        return "join"
    if hyphenated >= least and hyphenated >= CLEAR * joined:
        return "keep"
    return None


def _decide(left: str, right: str, marker: str, producer: str | None, evidence: _Evidence,
            lexicon: Lexicon | None) -> tuple[str, str]:
    """("join" or "keep", why), the first rule that knows, in order of certainty."""
    if marker == "­":
        return "join", "join: soft hyphen"
    if from_chrome(producer):
        if marker in "￾\x02":
            return "keep", "keep: hyphen the page had (Chrome PDF)"
        if marker == "‐":
            return "join", "join: Chrome's hyphenation"
    lower = (left + right).lower(), (left.lower(), right.lower())
    if verdict := _verdict(evidence.words[lower[0]], evidence.pairs[lower[1]], 1):
        return verdict, f"{verdict}: same document"
    if right[0].isupper() and not left.isupper():
        return "keep", "keep: capital after the break"
    if lexicon is not None and (verdict := _verdict(*lexicon.counts(*lower[1]), 2)):
        return verdict, f"{verdict}: rest of the archive"
    return "keep", "keep: unsure"


def _dehyphenate(text: str, producer: str | None, lexicon: Lexicon | None, result: Result) -> str:
    # a word broken twice (one-\ntwo-\nthree) overlaps itself, so go again until no break is left;
    # a decided break never matches again, so this ends
    while matches := list(BREAK.finditer(text)):
        evidence = _Evidence.of(text)
        out, pos, newline_at = [], 0, None

        def stretch(start: int, end: int) -> str:
            piece = text[start:end]
            if newline_at is not None:
                piece = piece[:newline_at - start] + "\n" + piece[newline_at - start + 1:]
            return piece

        for i, m in enumerate(matches):
            out.append(stretch(pos, m.start()))
            left, marker, right = m.group(1), m.group(2) or m.group(3), m.group(4)
            verdict, why = _decide(left, right, marker, producer, evidence, lexicon)
            new = left + right if verdict == "join" else left + ("-" if marker in "￾\x02" else marker) + right
            result.note(why, m.group().replace("\n", "⏎"), new, _context(text, m.start(), m.end()), always=True)
            out.append(new)
            pos, newline_at = m.end(), None
            if "\n" in m.group():
                # the line break goes to the first space after the word, so lines don't run together;
                # never to just after a hyphen, where it would make a new break
                end = len(text) if i + 1 == len(matches) else matches[i + 1].start()
                line_end = text.find("\n", pos, end)
                limit = end if line_end < 0 else line_end
                at = text.find(" ", pos, limit)
                while at > 0 and text[at - 1] in HYPHENS + "￾\x02­":
                    at = text.find(" ", at + 1, limit)
                newline_at = at if at > 0 else None
        out.append(stretch(pos, len(text)))
        text = "".join(out)
    if "￾" in text:  # a printed hyphen that isn't between two letters, as in 1990-1995
        result.counts["hyphen restored"] += text.count("￾")
        text = text.replace("￾", "-")
    return text


# -- the archive's own vocabulary -----------------------------------------------------------------

class Lexicon:
    """How often the whole archive spells a word joined, and as a hyphenated pair: built by
    `osiris clean` into data/lexicon.sqlite, and only read here."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, check_same_thread=False)

    def knows(self, word: str) -> bool:
        return self.db.execute("SELECT 1 FROM words WHERE word = ?", (word,)).fetchone() is not None

    def counts(self, left: str, right: str) -> tuple[int, int]:
        joined = self.db.execute("SELECT n FROM words WHERE word = ?", (left + right,)).fetchone()
        pair = self.db.execute("SELECT n FROM pairs WHERE l = ? AND r = ?", (left, right)).fetchone()
        return (joined[0] if joined else 0), (pair[0] if pair else 0)


_lexicons: dict[str, Lexicon | None] = {}


def lexicon_at(path: str | Path | None) -> Lexicon | None:
    """The lexicon at this path, opened once per process, or None if there isn't one yet."""
    if not path:
        return None
    key = str(path)
    if key not in _lexicons:
        try:
            _lexicons[key] = Lexicon(Path(path)) if Path(path).is_file() else None
        except sqlite3.Error:
            _lexicons[key] = None
    return _lexicons[key]


def lexicon_from_env() -> Lexicon | None:
    return lexicon_at(os.environ.get(LEXICON_ENV))


def vocabulary(text: str, pdf: bool) -> tuple[Counter, Counter]:
    """Words and hyphenated pairs, lowercased, for the lexicon: the text cleaned as stored, with
    line-end breaks left out, since they're what the lexicon is for deciding."""
    text = _universal(text, Result(text))
    if pdf:
        text = BREAK.sub(" ", text)
    words = Counter(w.lower() for w in WORD.findall(text))
    pairs = Counter((a.lower(), b.lower()) for a, b in PAIR.findall(text))
    return words, pairs


# -- entry points --------------------------------------------------------------------------------

def clean(text: str, *, pdf: bool, producer: str | None = None, lexicon: Lexicon | None = None) -> Result:
    """The text repaired, with what changed. `pdf` is for text from a PDF or OCR of one;
    `producer` is the PDF's Producer field."""
    result = Result(text)
    if not TRIGGER.search(text) and unicodedata.is_normalized("NFC", text):
        return result  # nothing to repair: the common case, at the cost of one scan
    if pdf:  # PDF text isn't markdown, so there's no code to keep out
        text = _dehyphenate(_universal(text, result), producer, lexicon, result)
    else:
        text = _outside_code(text, lambda piece: _universal(piece, result))
    if text.startswith("﻿"):  # a byte-order mark: how the file was saved, not part of the text
        text = text[1:]
        result.counts["byte-order mark removed"] += 1
    result.text = text
    return result


def clean_text(text: str, *, pdf: bool, producer: str | None = None, lexicon: Lexicon | None = None) -> str:
    return clean(text, pdf=pdf, producer=producer, lexicon=lexicon).text


def clean_title(title: str | None) -> str | None:
    """A title isn't markdown and has no line ends to rejoin: just the repairs every source gets."""
    if not title:
        return title
    title = clean_text(title, pdf=False)
    return title.replace("￾", "-")
