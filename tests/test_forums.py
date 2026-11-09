"""The Hacker News and LessWrong lanes: no comments lost, and documents moved safely."""

import asyncio
from pathlib import Path

import pytest

from osiris import forums, review
from osiris.route import doc_id, route
from osiris.store import Store

LW_URL = "https://www.lesswrong.com/posts/CEGnJBHmkcwPTysb7/lonely-dissent"


def hn_comment(cid, parent, author, text, children=()):
    return {"id": cid, "parent_id": parent, "author": author, "text": text, "children": list(children), "type": "comment"}


def test_hn_thread_keeps_replies_of_a_deleted_comment():
    item = {"id": 1, "type": "story", "title": "A story", "author": "pg", "created_at": "2024-10-16T01:00:00Z", "points": 5,
            "url": "https://example.com", "children": [
                hn_comment(2, 1, "ann", "top", [hn_comment(3, 2, "bob", "reply")]),
                hn_comment(4, 1, None, None, [hn_comment(5, 4, "cy", "under a deleted one")]),
            ]}
    meta, body = forums.hn_thread(item)
    assert meta["url"] == "https://news.ycombinator.com/item?id=1" and meta["date"] == "2024-10-16"
    assert "Link: https://example.com" in body and "## Comments (3)" in body
    assert body.index("**ann**:") < body.index("**bob** (reply to ann):") < body.index("under a deleted one")


def preview(store: Store, url: str, text: str, status: str = "ok", decision: str | None = None) -> str:
    """A page as the generic lane stored it."""
    store.db.execute("INSERT INTO urls (url, lane, doc_key, doc_id, status, chars, dedup_key) VALUES (?, 'generic', ?, ?, ?, ?, ?)",
                     (url, url, doc_id(url), status, len(text), url))
    store.commit()
    store.write_doc(url, {"url": url, "source": "html", "title": "Old"}, text)
    return doc_id(url)


def rows(store: Store, url: str) -> tuple:
    return store.db.execute("SELECT lane, doc_key, doc_id, status, chars FROM urls WHERE url = ?", (url,)).fetchone()


def run(store: Store, fetch, refetch=True, lane="lesswrong") -> None:
    asyncio.run(forums._run(store, lane, fetch, 2, refetch))


def answer(text):
    async def fetch(session, key):
        return {"url": "https://example.com/new", "source": "lesswrong", "title": "New"}, text
    return fetch


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


def test_moves_a_preview_and_removes_its_file(store):
    old = preview(store, LW_URL, "preview")
    variant = LW_URL + "?commentId=abc"
    old2 = preview(store, variant, "preview too")
    db = review.connect(store.out)
    db.execute("INSERT INTO decisions (doc_id, decision, hash) VALUES (?, 'bad', 'x')", (old,))
    db.commit()
    review.write_exclude(db, store)
    db.close()

    run(store, answer("the full post " * 20))

    new = doc_id("lw:CEGnJBHmkcwPTysb7")
    assert rows(store, LW_URL)[:4] == ("lesswrong", "lw:CEGnJBHmkcwPTysb7", new, "ok")
    assert rows(store, variant)[2] == new
    assert not store.md_path(old).exists() and not store.md_path(old2).exists()
    assert store.read_doc(new)[1].startswith("the full post")
    db = review.connect(store.out)
    assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    db.close()
    assert str(old) not in (store.out / review.EXCLUDE_FILE).read_text()


@pytest.mark.parametrize("fetch_result", ["none", "error", "shorter"])
def test_keeps_the_archived_document(store, fetch_result):
    old = preview(store, LW_URL, "a longer archived text")
    before = store.md_path(old).read_bytes()

    async def fetch(session, key):
        if fetch_result == "error":
            raise forums.ForumError("boom")
        return None if fetch_result == "none" else ({"url": "u", "title": "T"}, "short")

    run(store, fetch)
    assert rows(store, LW_URL)[:4] == ("generic", LW_URL, old, "ok")
    assert store.md_path(old).read_bytes() == before
    assert not store.md_path(doc_id("lw:CEGnJBHmkcwPTysb7")).exists()


def test_saved_pages_are_left_alone(store):
    preview(store, LW_URL, "my saved copy")
    store.db.execute("UPDATE urls SET lane = 'singlefile'")
    store.commit()
    assert store.archived_in("lesswrong", route) == {}
