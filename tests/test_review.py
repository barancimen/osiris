"""A fix never replaces a document without your choice, and your decisions hold."""

from pathlib import Path

from osiris import review
from osiris.route import doc_id, route
from osiris.store import Store


# -- fetching again only ever improves a document ------------------------------------------------

GARBLED = "Ã©tÃ© Ã  la cafÃ© â€™ " * 40
CLEAN = "The summer was pleasant, and the café by the window served good coffee all season long. " * 6


def refetch_setup(tmp_path: Path, text: str) -> tuple[Store, str, str]:
    store = Store(tmp_path)
    r = route("https://example.com/article")
    store.add([r])
    chars = store.write_doc(r.doc_key, {"url": r.url, "source": "html", "title": "An article"}, text)
    store.record(r.doc_key, "ok" if chars >= 200 else "thin", chars=chars)
    store.commit()
    review.scan(store, tmp_path)
    db = review.connect(tmp_path)
    review.decide(db, [doc_id(r.doc_key)], "refetch")
    review.apply_refetch(store, db)
    db.close()
    return store, r.doc_key, doc_id(r.doc_key)


def outcome(store: Store, tmp_path: Path, did: str) -> tuple:
    review.scan(store, tmp_path)
    db = review.connect(tmp_path)
    decision = db.execute("SELECT decision, note FROM decisions WHERE doc_id = ?", (did,)).fetchone()
    db.close()
    status = store.db.execute("SELECT status FROM urls WHERE doc_id = ?", (did,)).fetchone()[0]
    return status, store.read_doc(did)[1], decision


def test_failed_fetch_keeps_the_document(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    store.record(key, "failed", "http_404", http_status=404)  # what the run records when the page is gone
    store.commit()
    status, text, decision = outcome(store, tmp_path, did)
    assert (status, text) == ("ok", GARBLED.strip())
    assert decision == ("bad", "Fetching again failed, so the earlier text was put back")
    assert did in review.excluded(tmp_path)  # still Bad, for the text it's about
    store.close()


def fetched(store: Store, key: str, text: str, status: str = "ok") -> None:
    chars = store.write_doc(key, {"url": "https://example.com/article", "source": "html"}, text)
    store.record(key, status, None if status == "ok" else "short", chars=chars)
    store.commit()


def choose(store: Store, tmp_path: Path, did: str, keep_new: bool) -> None:
    db = review.connect(tmp_path)
    review.choose(store, db, did, keep_new)
    db.close()


def kept_copies(tmp_path: Path) -> int:
    db = review.connect(tmp_path)
    n = db.execute("SELECT COUNT(*) FROM refetch_kept").fetchone()[0]
    db.close()
    return n


def test_different_text_waits_for_your_choice(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, CLEAN)
    fetched(store, key, GARBLED)
    status, text, decision = outcome(store, tmp_path, did)
    assert (status, text, decision[0]) == ("ok", GARBLED.strip(), "choose")
    assert did in review.excluded(tmp_path)  # neither version is exported until you pick
    db = review.connect(tmp_path)
    earlier = review.earlier_version(db, did)
    db.close()
    assert earlier["body"] == CLEAN.strip() and earlier["length"] == len(CLEAN.strip())
    assert outcome(store, tmp_path, did)[2][0] == "choose"  # later checks leave it waiting
    store.close()


def test_keeping_the_earlier_text_restores_it(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    before = store.md_path(did).read_bytes()
    fetched(store, key, "We use cookies.", "thin")
    outcome(store, tmp_path, did)
    choose(store, tmp_path, did, False)
    assert store.md_path(did).read_bytes() == before
    assert outcome(store, tmp_path, did) == ("ok", GARBLED.strip(), None)  # its earlier status, too
    assert kept_copies(tmp_path) == 0
    assert review.scan(store, tmp_path)["scored"] == 0  # put back and scored: nothing new for the next check
    store.close()


def test_a_decision_cant_override_the_choice(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    fetched(store, key, CLEAN)
    outcome(store, tmp_path, did)
    db = review.connect(tmp_path)
    assert review.decide(db, [did], "fine") == 0
    db.close()
    assert outcome(store, tmp_path, did)[2][0] == "choose" and kept_copies(tmp_path) == 1
    store.close()


def test_a_fetch_that_never_ran_keeps_the_document(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)  # queued, then stopped before fetching
    status, text, decision = outcome(store, tmp_path, did)
    assert (status, text, decision[0]) == ("ok", GARBLED.strip(), "bad")
    store.close()
