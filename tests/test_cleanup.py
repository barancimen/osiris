"""`osiris clean` on a small archive: what changes, what mustn't, and that review decisions survive."""

import json
from pathlib import Path

import pytest

from osiris import cleanup, review
from osiris.route import doc_id, route
from osiris.store import Store

WINDOWS = ("It’s a “test” – really, don’t worry. We’re sure that’s fine, isn’t it? You’ll see… "
           "they’ve said so, and we’d agree.\n") * 4  # English typed on Windows: cp1252, not UTF-8


def old_doc(store: Store, url: str, meta: dict, body: str) -> str:
    """A document as an earlier version stored it, before any cleaning."""
    r = route(url)
    body = body.strip()
    store.add([r])
    did = doc_id(r.doc_key)
    path = store.md_path(did)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {json.dumps(did)}", f"url: {json.dumps(url)}"]
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()]
    lines += ['fetched_at: "2026-01-02T03:04:05Z"', "---"]
    path.write_text("\n".join(lines) + "\n\n" + body + "\n", encoding="utf-8")
    store.record(r.doc_key, "ok", None, chars=len(body))
    store.commit()
    return did


@pytest.fixture
def archive(tmp_path: Path):
    out = tmp_path / "data"
    store = Store(out)
    original = tmp_path / "notes.txt"
    original.write_bytes(WINDOWS.encode("cp1252"))
    docs = {
        "pdf": old_doc(store, "https://example.com/paper.pdf", {"source": "pdf", "title": "Eﬃcient retrieval"},
                       "Infor-\nmation retrieval. " * 5 + "We study information and its retrieval at length here."),
        "web": old_doc(store, "https://example.com/page", {"source": "html", "title": "A page"},
                       "Some infor­mation on a web page, written at length so it isn't thin. " * 4),
        "plain": old_doc(store, "https://example.com/plain", {"source": "html", "title": "Nothing to do"},
                         "Ordinary text with nothing to repair in it, long enough to count as a document. " * 4),
        "text": old_doc(store, original.as_uri(), {"source": "file:text", "title": "notes.txt"},
                        WINDOWS.encode("cp1252").decode("utf-8", errors="replace").strip()),
    }
    review.scan(store, out)
    db = review.connect(out)
    review.decide(db, [docs["pdf"]], "bad")
    review.decide(db, [docs["web"]], "fine")
    review.write_exclude(db, store)
    db.close()
    yield store, out, docs
    store.close()


def body(store: Store, did: str) -> str:
    return store.read_doc(did)[1]


def head(store: Store, did: str) -> list[str]:
    return [line for line in store.md_path(did).read_text(encoding="utf-8").split("\n---\n")[0].splitlines()
            if not line.startswith("title:")]


def decisions(out: Path) -> dict:
    db = review.connect(out)
    try:
        return dict(db.execute("SELECT doc_id, decision FROM decisions"))
    finally:
        db.close()


def test_clean(archive):
    store, out, docs = archive
    plain_mtime = store.md_path(docs["plain"]).stat().st_mtime_ns
    heads = {k: head(store, d) for k, d in docs.items()}
    exclude = (out / "review-exclude.txt").read_text()

    totals = cleanup.clean_archive(store)

    assert totals.changed == 3
    assert body(store, docs["pdf"]).startswith("Information\nretrieval. Information")
    assert store.read_doc(docs["pdf"])[0]["title"] == "Efficient retrieval"
    assert "information on a web page" in body(store, docs["web"])
    assert body(store, docs["text"]) == WINDOWS.strip()  # read again from the original, as cp1252
    assert store.md_path(docs["plain"]).stat().st_mtime_ns == plain_mtime  # untouched files aren't rewritten
    assert {k: head(store, d) for k, d in docs.items()} == heads  # fetched_at and the rest as they were
    chars = dict(store.db.execute("SELECT doc_id, chars FROM urls"))
    assert all(chars[d] == len(body(store, d)) for d in docs.values())

    # review decisions hold for the repaired text, through the next garbage check and into export
    assert decisions(out) == {docs["pdf"]: "bad", docs["web"]: "fine"}
    result = review.scan(store, out)
    assert result["text_changed"] == 0 and result["scored"] == 0
    assert decisions(out) == {docs["pdf"]: "bad", docs["web"]: "fine"}
    assert (out / "review-exclude.txt").read_text() == exclude
    assert review.excluded(out)[docs["pdf"]] == review.text_hash(body(store, docs["pdf"]))

    report = (out / cleanup.REPORT_FILE).read_text(encoding="utf-8")
    assert "join: same document" in report and "text re-read with its encoding detected" in report

    assert cleanup.clean_archive(store).changed == 0  # cleaning twice changes nothing


def test_dry_run_changes_nothing(archive):
    store, out, docs = archive
    files = {d: store.md_path(d).read_bytes() for d in docs.values()}
    totals = cleanup.clean_archive(store, dry_run=True)
    assert totals.changed == 3
    assert {d: store.md_path(d).read_bytes() for d in docs.values()} == files
    assert "join: same document" in (out / cleanup.REPORT_FILE).read_text(encoding="utf-8")


def test_undo(archive):
    store, out, docs = archive
    files = {d: store.md_path(d).read_bytes() for d in docs.values()}
    cleanup.clean_archive(store)
    restored, skipped = cleanup.undo(store)
    assert (restored, skipped) == (3, 0)
    assert {d: store.md_path(d).read_bytes() for d in docs.values()} == files
    assert decisions(out) == {docs["pdf"]: "bad", docs["web"]: "fine"}
    result = review.scan(store, out)
    assert result["text_changed"] == 0
    assert decisions(out) == {docs["pdf"]: "bad", docs["web"]: "fine"}


def test_undo_leaves_newer_text_alone(archive):
    store, out, docs = archive
    cleanup.clean_archive(store)
    path = store.md_path(docs["web"])
    path.write_text(path.read_text(encoding="utf-8") + "fetched again\n", encoding="utf-8")
    assert cleanup.undo(store) == (2, 1)
    assert path.read_text(encoding="utf-8").endswith("fetched again\n")


def test_interrupted_bookkeeping_is_finished(archive):
    store, out, docs = archive
    did = docs["pdf"]
    before = body(store, did)
    cleanup.clean_archive(store)
    # as if the run stopped right after replacing the file
    store.db.execute("UPDATE urls SET chars = ? WHERE doc_id = ?", (len(before), did))
    store.commit()
    db = review.connect(out)
    db.execute("UPDATE decisions SET hash = ? WHERE doc_id = ?", (review.text_hash(before), did))
    db.commit()
    db.close()
    totals = cleanup.clean_archive(store)
    assert totals.changed == 0 and totals.finished == 1
    assert dict(store.db.execute("SELECT doc_id, chars FROM urls"))[did] == len(body(store, did))
    assert review.scan(store, out)["text_changed"] == 0
    assert decisions(out)[did] == "bad"


def test_write_doc_cleans_new_documents(tmp_path: Path):
    store = Store(tmp_path)
    r = route("https://example.com/new")
    store.add([r])
    chars = store.write_doc(r.doc_key, {"url": r.url, "source": "html", "title": "ﬁrst"}, "infor­mation ")
    meta, text = store.read_doc(doc_id(r.doc_key))
    assert (text, meta["title"], chars) == ("information", "first", len("information"))
    store.close()


def test_lexicon(archive):
    store, out, _ = archive
    path = cleanup.build_lexicon(store)
    from osiris.clean import Lexicon

    assert Lexicon(path).counts("infor", "mation")[0] >= 2  # "information", outside the breaks
