"""The Hacker News and LessWrong lanes: routing, rendering, and moving documents safely."""

import asyncio
import json
from pathlib import Path

import pytest

from osiris import forums, review
from osiris.route import doc_id, route
from osiris.store import Store

HN_URL = "https://news.ycombinator.com/item?id=123"
LW_URL = "https://www.lesswrong.com/posts/CEGnJBHmkcwPTysb7/lonely-dissent"


@pytest.mark.parametrize("url,lane,key", [
    (HN_URL, "hackernews", "hn:123"),
    ("https://news.ycombinator.com/item?p=2&id=123", "hackernews", "hn:123"),
    ("https://news.ycombinator.com/user?id=pg", "generic", None),
    ("https://news.ycombinator.com/", "generic", None),
    (LW_URL, "lesswrong", "lw:CEGnJBHmkcwPTysb7"),
    (LW_URL + "?commentId=abc", "lesswrong", "lw:CEGnJBHmkcwPTysb7"),
    ("https://www.greaterwrong.com/posts/CEGnJBHmkcwPTysb7/x", "lesswrong", "lw:CEGnJBHmkcwPTysb7"),
    ("https://www.alignmentforum.org/posts/CEGnJBHmkcwPTysb7/x", "lesswrong", "lw:CEGnJBHmkcwPTysb7"),
    ("https://www.lesswrong.com/s/abc/p/CEGnJBHmkcwPTysb7/x", "lesswrong", "lw:CEGnJBHmkcwPTysb7"),
    ("https://www.lesswrong.com/users/someone", "generic", None),
    ("https://www.lesswrong.com/tag/ai", "generic", None),
])
def test_route(url, lane, key):
    r = route(url)
    assert r.lane == lane
    if key:
        assert r.doc_key == key


def test_hn_text():
    text = ('First &amp; <i>second</i>.<p>See <a href="https:&#x2F;&#x2F;example.com&#x2F;a?b=1" rel="nofollow">'
            'https:&#x2F;&#x2F;example.com&#x2F;a...</a>'
            "<p><pre><code>  x &lt; y\n</code></pre>")
    assert forums.hn_text(text) == "First & *second*.\n\nSee https://example.com/a?b=1\n\n\n\n```\n  x < y\n```"


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


def test_hn_link_to_one_comment():
    item = hn_comment(9, 1, "ann", "the comment", [hn_comment(10, 9, "bob", "a reply")])
    item.update(story_id=1, created_at="2024-10-16T01:00:00Z")
    meta, body = forums.hn_thread(item, "The story")
    assert meta["title"] == "Comment by ann on: The story"
    assert "item?id=1" in body and "the comment" in body and "## Replies (1)" in body


def lw_comment(cid, parent, score, name, text, deleted=False):
    return {"_id": cid, "parentCommentId": parent, "baseScore": score, "deleted": deleted,
            "user": {"displayName": name} if name else None, "contents": {"markdown": text} if text else None}


def test_lw_thread_orders_by_karma():
    post = {"title": "T", "pageUrl": "https://www.lesswrong.com/posts/x/t", "postedAt": "2007-12-28T04:00:00Z",
            "baseScore": 202, "user": {"displayName": "E"}, "contents": {"markdown": "The essay."}}
    meta, body = forums.lw_thread(post, [
        lw_comment("a", None, 1, "low", "low comment"),
        lw_comment("b", None, 30, "high", "high comment"),
        lw_comment("c", "b", 2, "kid", "reply"),
        lw_comment("d", "b", 0, None, None, deleted=True),
    ])
    assert meta["author"] == "E" and "The essay." in body and "## Comments (3)" in body
    assert body.index("**high** (30 karma):") < body.index("**kid** (2 karma, reply to high):") < body.index("low comment")


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


def test_thin_page_also_moves(store):
    old = preview(store, LW_URL, "tiny", status="thin")
    assert store.reroute(route) == 0  # not before a new document exists
    run(store, answer("the full post " * 20))
    assert rows(store, LW_URL)[0] == "lesswrong" and not store.md_path(old).exists()


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


def test_without_the_flag_finished_pages_stay(store):
    preview(store, LW_URL, "preview")
    run(store, answer("the full post " * 20), refetch=False)
    assert rows(store, LW_URL)[0] == "generic"


def test_rerun_updates_in_place(store):
    store.add([route(LW_URL)])
    run(store, answer("first version of the thread " * 5), refetch=False)
    key = "lw:CEGnJBHmkcwPTysb7"
    assert rows(store, LW_URL)[:2] == ("lesswrong", key) and rows(store, LW_URL)[3] == "ok"
    run(store, answer("second version of the thread, with a new comment " * 5))
    assert "second" in store.read_doc(doc_id(key))[1]
    run(store, answer("short"))
    assert "second" in store.read_doc(doc_id(key))[1]


def test_new_url_that_fails_is_recorded(store):
    store.add([route(HN_URL)])

    async def missing(session, key):
        return None

    run(store, missing, refetch=False, lane="hackernews")
    assert rows(store, HN_URL)[3] == "failed"
    assert store.db.execute("SELECT reason FROM urls").fetchone()[0] == "not_found"


def test_saved_pages_are_left_alone(store):
    preview(store, LW_URL, "my saved copy")
    store.db.execute("UPDATE urls SET lane = 'singlefile'")
    store.commit()
    assert store.archived_in("lesswrong", route) == {}



def test_kept_original_follows_a_moved_document(store):
    old = preview(store, LW_URL, "preview")
    variant = LW_URL + "?commentId=abc"
    old2 = preview(store, variant, "preview too")
    big = store.save_raw(LW_URL, "html.zst", data=b"<html>" * 500)
    small = store.save_raw(variant, "html.zst", data=b"<html>")
    store.record(LW_URL, "ok", raw_path=big)
    store.record(variant, "ok", raw_path=small)
    store.commit()

    run(store, answer("the full post " * 20))

    new = doc_id("lw:CEGnJBHmkcwPTysb7")
    meta = store.read_doc(new)[0]
    assert meta["raw"] == f"raw/{new[:2]}/{new}.html.zst" and (store.out / meta["raw"]).stat().st_size > 20
    assert not (store.out / big).exists() and not (store.out / small).exists()
    assert rows(store, LW_URL)[2] == new and store.db.execute("SELECT DISTINCT raw_path FROM urls").fetchall() == [(meta["raw"],)]


def test_old_ui_config_gains_the_new_lanes(tmp_path):
    from osiris import ui

    path = ui.config_path(tmp_path)
    path.write_text(json.dumps({"options": {"lanes": ["generic", "reddit"]}}), encoding="utf-8")
    assert ui.load_config(tmp_path)["options"]["lanes"] == ["generic", "reddit", "hackernews", "lesswrong"]
    path.write_text(json.dumps({"options": {"lanes": ["generic"], "forum_lanes_offered": True}}), encoding="utf-8")
    assert ui.load_config(tmp_path)["options"]["lanes"] == ["generic"]
