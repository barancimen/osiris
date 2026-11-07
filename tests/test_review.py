"""Review keeps working, and keeps your decisions, in archives from before it was called review."""

from pathlib import Path

from osiris import review
from osiris.route import doc_id, route
from osiris.store import Store


def test_old_audit_files_become_review_files(tmp_path: Path):
    store = Store(tmp_path)
    r = route("https://example.com/page")
    store.add([r])
    store.write_doc(r.doc_key, {"url": r.url, "source": "html", "title": "A page"}, "Some text. " * 30)
    store.record(r.doc_key, "ok", chars=330)
    store.commit()
    review.scan(store, tmp_path)
    db = review.connect(tmp_path)
    review.decide(db, [doc_id(r.doc_key)], "bad")
    db.close()
    # as an archive from before the rename has them
    for suffix in ("", "-wal", "-shm"):
        if (tmp_path / f"review.sqlite{suffix}").exists():
            (tmp_path / f"review.sqlite{suffix}").rename(tmp_path / f"audit.sqlite{suffix}")
    (tmp_path / "review-exclude.txt").rename(tmp_path / "audit-exclude.txt")

    assert review.exists(tmp_path)
    assert review.excluded(tmp_path) == {doc_id(r.doc_key): review.text_hash(store.read_doc(doc_id(r.doc_key))[1])}
    assert (tmp_path / "review.sqlite").exists() and not (tmp_path / "audit.sqlite").exists()
    review.scan(store, tmp_path)
    assert (tmp_path / "review-exclude.txt").read_text().strip().endswith(".md")
    assert not (tmp_path / "audit-exclude.txt").exists()
    store.close()


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


def test_worse_text_is_not_kept(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, CLEAN)
    chars = store.write_doc(key, {"url": "https://example.com/article", "source": "html"}, GARBLED)
    store.record(key, "ok", chars=chars)
    store.commit()
    status, text, decision = outcome(store, tmp_path, did)
    assert (status, text) == ("ok", CLEAN.strip())
    assert decision[0] == "bad" and "didn't give better text" in decision[1]
    store.close()


def test_newly_thin_text_is_not_kept(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    chars = store.write_doc(key, {"url": "https://example.com/article", "source": "html"}, "We use cookies.")
    store.record(key, "thin", "short", chars=chars)
    store.commit()
    status, text, decision = outcome(store, tmp_path, did)
    assert (status, text) == ("ok", GARBLED.strip())
    assert decision[0] == "bad"
    store.close()


def test_better_text_is_kept(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    chars = store.write_doc(key, {"url": "https://example.com/article", "source": "html"}, CLEAN)
    store.record(key, "ok", chars=chars)
    store.commit()
    status, text, decision = outcome(store, tmp_path, did)
    assert (status, text, decision) == ("ok", CLEAN.strip(), None)  # a new text gets a new look
    store.close()


def test_same_text_is_noted(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    store.record(key, "ok", chars=len(GARBLED.strip()))
    store.commit()
    status, text, decision = outcome(store, tmp_path, did)
    assert decision == ("bad", "Fetching again changed nothing")
    store.close()


def test_restored_file_is_byte_for_byte(tmp_path: Path):
    store, key, did = refetch_setup(tmp_path, GARBLED)
    before = store.md_path(did).read_bytes()
    chars = store.write_doc(key, {"url": "https://example.com/article", "source": "html"}, "Access denied.")
    store.record(key, "thin", "short", chars=chars)
    store.commit()
    outcome(store, tmp_path, did)
    assert store.md_path(did).read_bytes() == before
    assert review.scan(store, tmp_path)["scored"] == 0  # put back and scored: nothing new for the next check
    store.close()
