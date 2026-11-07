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
