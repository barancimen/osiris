"""Reading text files that aren't UTF-8: right, or no guess at all, never wrong."""

import re

import pytest

from osiris.encoding import CANDIDATES, guess
from osiris.extract import decode_text

ENGLISH = "It’s a “test” – really, don’t worry. We’re sure that’s fine, isn’t it? You’ll see… they’ve said so."
SAMPLES = [
    ENGLISH,
    "The café’s naïve résumé was “perfect”—or so they’d claimed. Isn’t it? We’d agree.",
    "Le café était très agréable, à côté de la fenêtre. Où êtes-vous? Noël approche, l’été est loin.",
    "Größe und Übermaß: „Grüße“ aus München – schön wär’s. Die Bücher über Äpfel sind öffentlich.",
    "Türkçe metin: ağaç, şehir, ılık rüzgâr, İstanbul'da güzel bir gün. Öğrenciler sınıfta çalışıyor.",
    "Zażółć gęślą jaźń. Łódź jest piękna, a Kraków też. Mężczyzna śpiewa piosenkę.",
    "Привет, как дела? Это проверка кодировки текста. Москва и Санкт-Петербург — большие города.",
    "Καλημέρα, τι κάνεις; Η Αθήνα είναι όμορφη πόλη. Ο ήλιος λάμπει σήμερα.",
    "Gödel, Erdős and Dvořák met in a café.",
]


class Vocabulary:
    def __init__(self, texts):
        self.words = {w.lower() for t in texts for w in re.findall(r"[^\W\d_]+", t)}

    def knows(self, word):
        return word in self.words


def test_no_guess_keeps_replacement_characters():
    data = "Gödel, Erdős and Dvořák.".encode("cp1250")
    assert "�" in decode_text(data)


@pytest.mark.parametrize("text", SAMPLES)
def test_never_wrong(text):
    """In every encoding that can hold it, with no vocabulary or one from the other samples."""
    others = Vocabulary([t for t in SAMPLES if t != text])
    for name in CANDIDATES:
        try:
            data = (text * 3).encode(name)
            data.decode("utf-8")
            continue  # UTF-8 by chance: never guessed
        except UnicodeEncodeError:
            continue
        except UnicodeDecodeError:
            pass
        for lexicon in (None, others):
            assert guess(data, lexicon) in (text * 3, None), name
