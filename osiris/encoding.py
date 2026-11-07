"""Which legacy encoding a text file that isn't UTF-8 was saved in, only when that's beyond doubt.

General-purpose detectors guess from letter statistics, and with single-byte encodings they are
often confidently wrong: Turkish cp1254 read as Baltic cp1257 comes out as "Türkēe ašaē" with no
error anywhere. A wrong guess is worse than no guess, since U+FFFD at least shows the garbage check
that something is missing. So every common encoding is tried, and its reading judged:

- Hard checks rule a reading out for good: control characters, unassigned characters, a word that
  mixes scripts, a capital after a small letter inside a word (a wrong Cyrillic code page swaps
  cases), a symbol inside a word, or more symbols than a person types.
- The soft check is whether the accented Latin letters all belong to one language's alphabet and
  the other letters to one script. Real text can fail it ("Gödel, Erdős and Dvořák"), so it
  never rules a reading out, and passing it proves nothing on its own either.

Encodings that agree on the text count as one reading. If exactly one reading survives the hard
checks and passes the soft one, that's the answer. Otherwise the archive's own vocabulary decides,
only by a clear margin, and failing that there's no guess.
"""

from __future__ import annotations

import re
import unicodedata

CANDIDATES = (
    "cp1252", "iso8859_15", "cp1254", "iso8859_9", "cp1250", "iso8859_2", "cp1257", "iso8859_13", "iso8859_16",
    "mac_roman", "cp437", "cp850", "latin_1", "cp1251", "koi8_r", "koi8_u", "mac_cyrillic", "iso8859_5", "cp1253",
    "iso8859_7", "cp1255", "cp1256", "shift_jis", "euc_jp", "gbk", "big5", "euc_kr",
)
# the accented letters of each language written in Latin script; a text's must all be in one of these
ALPHABETS = {
    "Turkish": "çğıöşüâîû", "Azerbaijani": "çəğıöşü", "German": "äöüß", "French": "àâæçéèêëîïôœùûüÿ",
    "Spanish": "áéíñóúü", "Portuguese": "áâãàçéêíóôõú", "Italian": "àèéìíîòóùú", "Catalan": "àçèéíïòóúü",
    "Dutch": "áéíóúàèëïöü", "Nordic": "æøåäöé", "Icelandic": "áðéíóúýþæö", "Finnish": "äöåšž",
    "Polish": "ąćęłńóśźż", "Czech": "áčďéěíňóřšťúůýž", "Slovak": "áäčďéíĺľňóôŕšťúýž", "Hungarian": "áéíóöőúüű",
    "Romanian": "ăâîșțşţ", "Croatian": "čćđšž", "Slovenian": "čšž", "Latvian": "āčēģīķļņšūž",
    "Lithuanian": "ąčęėįšųūž", "Estonian": "äöõüšž", "Albanian": "çë",
}
_ALPHABETS = [set(letters) | {c.upper() for c in letters if len(c.upper()) == 1} | ({"İ"} if name == "Turkish" else set())
              for name, letters in ALPHABETS.items()]
WORD = re.compile(r"[^\W\d_]+")
INSIDE_WORD = re.compile(r"(?<=[^\W\d_])[^\w\s'’ʼ‘·\-‐](?=[^\W\d_])")
CLEAR = 3  # the next best reading must have this many times as many words the vocabulary doesn't know
ENOUGH = 5  # and the vocabulary must know at least this many of the best reading's
SAMPLE = 200_000  # characters from each end of a long text for the slower checks
CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")
SYMBOL_RUN = re.compile(r"[^\x00-\x7f\w\s\u2010-\u2027\u00a0-\u00bf]{3,}")  # three symbols in a row


def _script(c: str) -> str:
    o = ord(c)
    if o < 0x250 or 0x1E00 <= o < 0x1F00:
        return "latin"
    if 0x370 <= o < 0x400 or 0x1F00 <= o < 0x2000:
        return "greek"
    if 0x400 <= o < 0x530:
        return "cyrillic"
    if 0x590 <= o < 0x600:
        return "hebrew"
    if 0x600 <= o < 0x700 or 0x750 <= o < 0x780 or 0xFB50 <= o < 0xFE00 or 0xFE70 <= o < 0xFF00:
        return "arabic"
    if 0x3040 <= o < 0x3100 or 0x3130 <= o < 0x3190 or 0x3400 <= o < 0xA000 or 0xAC00 <= o < 0xD7B0 \
            or 0xF900 <= o < 0xFB00 or 0xFF00 <= o < 0xFFF0:
        return "cjk"
    return "other"


def _hard_ok(text: str) -> bool:
    """Nothing in this reading that a person writing text would have put there."""
    if CONTROL.search(text):
        return False
    if len(text) > 2 * SAMPLE:
        text = text[:SAMPLE] + "\n" + text[-SAMPLE:]
    beyond_ascii = [c for c in text if not c.isascii()]
    if any(unicodedata.category(c) in ("Co", "Cn") for c in beyond_ascii):
        return False
    # a misreading can also be all symbols, with no letters to check: ㎟㎮㏀, ╠═╣
    symbols = sum(unicodedata.category(c) in ("So", "Sm", "Sk", "No") for c in beyond_ascii)
    if symbols > 2 + len(beyond_ascii) // 10 or SYMBOL_RUN.search(text):
        return False
    inside = 0
    for m in INSIDE_WORD.finditer(text):  # Chinese and Japanese put punctuation between words without spaces
        if _script(text[m.start() - 1]) != "cjk" and _script(text[m.end()]) != "cjk":
            inside += 1
    words = [w for w in WORD.findall(text) if not w.isascii()]
    if inside > 2 + len(words) // 100:
        return False
    for word in words:
        found = {_script(c) for c in word if not c.isascii()} | ({"latin"} if any(c.isascii() for c in word) else set())
        if len(found) > 1 or "other" in found:
            return False
        if any(a.islower() and b.isupper() for a, b in zip(word, word[1:])):
            return False
    return True


def _alphabet_ok(text: str) -> bool:
    """Accented Latin letters from one language's alphabet, and other letters from one script."""
    letters = {c for w in WORD.findall(text) if not w.isascii() for c in w if not c.isascii()}
    accented = {c for c in letters if _script(c) == "latin"}
    others = {_script(c) for c in letters} - {"latin"}
    if len(others) > 1 or (others and accented):
        return False
    return not accented or any(accented <= alphabet for alphabet in _ALPHABETS)


def guess(data: bytes, lexicon=None) -> str | None:
    """The text, if one reading of these bytes is beyond doubt; otherwise None."""
    readings = set()
    for name in CANDIDATES:
        try:
            text = data.decode(name)
        except UnicodeDecodeError:
            continue
        if _hard_ok(text):
            readings.add(text)
    if len(readings) == 1 and _alphabet_ok(next(iter(readings))):
        return readings.pop()
    if readings and lexicon is not None:
        # a misreading makes words nobody writes (aðaç, þehir): the right reading has the fewest the
        # archive has never seen, and it must have far fewer than the next best
        ranked = []
        for text in readings:
            found = {w.lower() for w in WORD.findall(text) if not w.isascii()}
            known = sum(lexicon.knows(w) for w in found)
            ranked.append((len(found) - known, known, text))
        ranked.sort(key=lambda r: (r[0], -r[1]))
        misses, known, text = ranked[0]
        if len(ranked) == 1:  # only one reading left, but its letters mix alphabets: the vocabulary must vouch
            sure = known >= ENOUGH and known >= misses
        else:  # e.g. English with curly quotes: no accented words to know, and the misreading has many
            runner_up = ranked[1][0]
            sure = known >= misses and runner_up >= CLEAR * max(misses, 1) and runner_up - misses >= ENOUGH
        if sure:
            return text
    return None
