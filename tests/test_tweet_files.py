"""Tweets from files: Twitter's export zip, bookmark exports, Community Archive uploads and fetch_tweets.py's databases."""

import html
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from osiris import tweetdb
from osiris.route import doc_id, route
from osiris.store import Store

ME = {"accountId": "42", "userName": "ada", "displayName": "Ada L"}
LONG = "A long thought that goes on " + "and on " * 60 + "to its end."


def own(tid: str, text: str, at: str = "Tue Aug 26 14:03:38 +0000 2025", **more) -> dict:
    return {"tweet": {"id_str": tid, "id": tid, "full_text": text, "created_at": at, **more}}


def export_zip(path: Path, tweets: list[dict], notes: list[dict] = (), folder: str = "", extra: dict | None = None) -> Path:
    """One of Twitter's exports, with the files a real one has beside the tweets."""
    files = {
        "data/tweets.js": ("YTD.tweets.part0", tweets),
        "data/note-tweet.js": ("YTD.note_tweet.part0", list(notes)),
        "data/community-tweet.js": ("YTD.community_tweet.part0", []),
        "data/deleted-tweets.js": ("YTD.deleted_tweets.part0", [own("90000", "a tweet I deleted")]),
        "data/like.js": ("YTD.like.part0", [{"like": {"tweetId": "90001", "fullText": "someone else's tweet"}}]),
        "data/direct-messages.js": ("YTD.direct_messages.part0", [{"dmConversation": {"conversationId": "90002"}}]),
    }
    types = {"tweets": "data/tweets.js", "noteTweet": "data/note-tweet.js", "communityTweet": "data/community-tweet.js",
             "deletedTweets": "data/deleted-tweets.js", "like": "data/like.js", "directMessages": "data/direct-messages.js"}
    manifest = {"userInfo": ME, "dataTypes": {name: {"files": [{"fileName": file, "globalName": files[file][0],
                                                               "count": str(len(files[file][1]))}]} for name, file in types.items()}}
    manifest["dataTypes"].update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(folder + "data/manifest.js", "window.__THAR_CONFIG = " + json.dumps(manifest, indent=2))
        zf.writestr(folder + "data/account.js", "window.YTD.account.part0 = " + json.dumps([{"account": {"username": "ada", "email": "ada@example.com"}}]))
        zf.writestr(folder + "Your archive.html", "<html><body>Open this to see your archive</body></html>")
        for file, (name, items) in files.items():
            zf.writestr(folder + file, f"window.{name} = " + json.dumps(items, indent=2))
    return path


def api_tweet(tid: str, user: str, name: str, text: str, note: str | None = None) -> dict:
    """A tweet as X's API nests it, which the bookmark exporter keeps under `metadata`."""
    tweet = {"__typename": "Tweet", "rest_id": tid,
             "core": {"user_results": {"result": {"core": {"screen_name": user, "name": name}}}},
             "legacy": {"full_text": text[:280], "created_at": "Thu Jan 24 20:42:52 +0000 2019"}}
    if note:
        tweet["note_tweet"] = {"note_tweet_results": {"result": {"text": note}}}
    return tweet


def bookmark(tid: str, user: str, name: str, text: str, quoted: dict | None = None, note: bool = False) -> dict:
    meta = api_tweet(tid, user, name, text, text if note else None)
    if quoted:
        meta["quoted_status_result"] = {"result": quoted}
    return {"id": tid, "created_at": "2019-01-24 23:42:52 +03:00", "full_text": text, "media": [], "screen_name": user, "name": name,
            "in_reply_to": None, "quoted_status": quoted and (quoted.get("rest_id") or quoted.get("tweet", {}).get("rest_id")),
            "favorite_count": 3, "bookmarked": True, "url": f"https://twitter.com/{user}/status/{tid}", "metadata": meta}


BOOKMARKS = [
    bookmark("20101", "ann", "Ann A", "First line & more\nsecond line <tag> https://t.co/abc"),
    bookmark("20102", "bob", "Bob B", LONG, note=True),
    bookmark("20103", "cy", "Cy C", "Look at this", quoted=api_tweet("50055", "dee", "Dee D", "the quoted words", "the quoted words, in full")),
    bookmark("20104", "eve", "Eve E", "Seen by some", quoted={"__typename": "TweetWithVisibilityResults", "tweet": api_tweet("50056", "fay", "Fay F", "inside a wrapper")}),
    bookmark("20105", "gus", "Gus G", "Quoting a tweet that's gone", quoted={"__typename": "TweetTombstone"}),
]
HEADINGS = {"id": "ID", "created_at": "Date", "full_text": "Content", "media": "Media", "screen_name": "Screen Name", "name": "Profile Name",
            "in_reply_to": "Replying To", "quoted_status": "Quote Source", "favorite_count": "Favorites", "bookmarked": "Bookmarked",
            "url": "URL", "metadata": "metadata"}


def bookmarks_html(path: Path, items: list[dict], headings: dict = HEADINGS) -> Path:
    """The same bookmarks as the exporter's HTML table: every value escaped, and the tweet's JSON behind a label to click."""
    def cell(key, value):
        if key == "full_text":
            return f'<p style="white-space: pre-wrap; max-width: 640px;">{html.escape(value)}</p>'
        if key == "metadata":
            return f"<details><summary>Expand</summary><pre>{html.escape(json.dumps(value, indent=2))}</pre></details>"
        if key == "url":
            return f'<a href="{value}" target="_blank">{value}</a>'
        return html.escape(json.dumps(value) if not isinstance(value, str) else value)
    head = "".join(f"<th>{h}</th>" for h in headings.values())
    rows = "".join("<tr>" + "".join(f"<td>{cell(k, item[k])}</td>" for k in headings) + "</tr>" for item in items)
    path.write_text(f'<html>\n<head><meta charset="utf-8"><title>Exported Data</title></head>\n<body>\n'
                    f'<table class="table table-striped"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></body></html>', encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """Where the tweet files are, apart from the archive."""
    (tmp_path / "tweets").mkdir()
    return tmp_path / "tweets"


def doc(store: Store, tid: str):
    return store.read_doc(doc_id(f"tweet:{tid}"))


def saved(store: Store) -> set[str]:
    return {key.split(":")[1] for (key,) in store.db.execute("SELECT doc_key FROM urls WHERE status = 'ok'")}


def test_an_export_gives_the_accounts_own_tweets_and_nothing_else(store: Store, folder: Path):
    path = export_zip(folder / "twitter-2025-08-27-abc.zip", [
        own("10001", "Just a tweet"),
        own("10002", "@bob I think so too", in_reply_to_status_id_str="77", in_reply_to_screen_name="bob"),
        own("10003", "RT @cy: something cy said that got cut sh…"),
        own("10004", "@bob " + LONG[:270] + "…", at="Sat Nov 15 21:06:36 +0000 2025"),
    ], notes=[{"noteTweet": {"noteTweetId": "9", "createdAt": "2025-11-15T21:06:36.000Z", "core": {"text": LONG}}}])
    assert tweetdb.count_tweets(path) == ("export", 4)
    tweetdb.import_files(store, [folder])
    assert saved(store) == {"10001", "10002", "10003", "10004"}  # not the deleted tweet, the like or the message
    meta, body = doc(store, "10002")
    assert meta["url"] == "https://x.com/ada/status/10002" and meta["author"] == "Ada L (@ada)" and meta["source"] == "tweet:import"
    assert meta["date"] == "2025-08-26 14:03:38" and body == "**Ada L (@ada)**\n\n@bob I think so too"
    assert doc(store, "10003")[1].endswith("RT @cy: something cy said that got cut sh…")
    assert doc(store, "10004")[1].endswith("@bob " + LONG)  # the whole of a long tweet, from note-tweet.js
    assert "example.com" not in "".join(p.read_text() for p in store.md_dir.rglob("*.md"))


def test_an_export_is_read_in_pieces_and_inside_a_folder_of_the_zip(store: Store, folder: Path, monkeypatch):
    monkeypatch.setattr(tweetdb, "CHUNK", 64)  # far smaller than a tweet, so values straddle what's read at once
    path = export_zip(folder / "again.zip", [own(str(10000 + n), f"Tweet number {n} " + "x" * (n * 7 % 90)) for n in range(1, 41)],
                      folder="twitter-2026-01-04-abc/")
    assert tweetdb.count_tweets(path) == ("export", 40)
    tweetdb.import_files(store, [path])
    assert saved(store) == {str(10000 + n) for n in range(1, 41)}
    assert doc(store, "10017")[1].endswith("Tweet number 17 " + "x" * (17 * 7 % 90))


def test_other_zips_and_pages_are_passed_over(store: Store, folder: Path):
    with zipfile.ZipFile(folder / "photos.zip", "w") as zf:
        zf.writestr("data/tweets.js", "window.YTD.tweets.part0 = " + json.dumps([own("10001", "no manifest beside it")]))
    (folder / "broken.zip").write_bytes(b"PK not really a zip")
    (folder / "page.html").write_text("<html><table><thead><tr><th>ID</th><th>Price</th></tr></thead>"
                                        "<tbody><tr><td>12345</td><td>9</td></tr></tbody></table></html>")
    (folder / "settings.json").write_text('[{"id": 5, "value": true}]')
    (folder / "accounts.txt").write_text("ada\n")
    assert [tweetdb.count_tweets(f) for f in tweetdb.find_files([folder])] == [None, None, None, None]  # the list of accounts isn't a tweet file at all
    tweetdb.import_files(store, [folder])
    assert saved(store) == set()


def test_bookmarks_come_out_the_same_from_json_and_html(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tweetdb, "CHUNK", 512)
    (tmp_path / "j").mkdir()
    (tmp_path / "h").mkdir()
    (tmp_path / "j" / "twitter-Bookmarks-1.json").write_text(json.dumps(BOOKMARKS, indent=2), encoding="utf-8")
    bookmarks_html(tmp_path / "h" / "twitter-Bookmarks-1.html", BOOKMARKS)
    docs = {}
    for kind in ("j", "h"):
        (found,) = tweetdb.find_files([tmp_path / kind])
        assert tweetdb.count_tweets(found) == ("bookmarks", 5)
        store = Store(tmp_path / f"data-{kind}")
        tweetdb.import_files(store, [tmp_path / kind])
        docs[kind] = {tid: doc(store, tid) for tid in ("20101", "20102", "20103", "20104", "20105")}
        store.close()
    strip = lambda found: {tid: ({k: v for k, v in meta.items() if k != "fetched_at"}, body) for tid, (meta, body) in found.items()}
    assert strip(docs["j"]) == strip(docs["h"])
    meta, body = docs["h"]["20101"]
    assert meta["url"] == "https://x.com/ann/status/20101" and meta["author"] == "Ann A (@ann)"
    assert meta["date"] == "2019-01-24 20:42:52"  # UTC, not the +03:00 the exporter wrote
    assert body == "**Ann A (@ann)**\n\nFirst line & more\nsecond line <tag> https://t.co/abc"
    assert docs["h"]["20102"][1].endswith(LONG)
    assert docs["h"]["20103"][1].endswith("Look at this\n\nQuoting @dee:\n\n> the quoted words, in full")
    assert docs["h"]["20104"][1].endswith("Quoting @fay:\n\n> inside a wrapper")
    assert "Quoting @" not in docs["h"]["20105"][1]  # the tweet it quoted is gone


def test_bookmarks_exported_without_the_metadata_column_still_import(store: Store, folder: Path):
    plain = {k: v for k, v in HEADINGS.items() if k != "metadata"}
    bookmarks_html(folder / "b.html", BOOKMARKS[:2], plain)
    (folder / "b.json").write_text(json.dumps([{k: v for k, v in b.items() if k != "metadata"} for b in BOOKMARKS[2:4]]))
    tweetdb.import_files(store, [folder])
    assert saved(store) == {"20101", "20102", "20103", "20104"}
    assert doc(store, "20101")[0]["date"] == "2019-01-24 20:42:52" and doc(store, "20103")[0]["author"] == "Cy C (@cy)"


def test_a_tweet_saved_or_deleted_is_left_alone_and_one_waiting_gets_the_text(store: Store, folder: Path):
    store.add([route(f"https://x.com/someone/status/{tid}") for tid in ("20101", "20102", "20103")])
    store.write_doc("tweet:20101", {"url": "https://x.com/ann/status/20101", "source": "tweet:scry", "title": "kept"}, "what was saved before")
    store.record("tweet:20101", "ok", chars=21)
    store.write_doc("tweet:20102", {"url": "https://x.com/bob/status/20102", "source": "tweet:scry"}, "to be deleted")
    store.record("tweet:20102", "ok", chars=13)
    store.delete(doc_id("tweet:20102"))
    store.record("tweet:20103", "failed", "not_in_scry")
    (folder / "b.json").write_text(json.dumps(BOOKMARKS))
    tweetdb.import_files(store, [folder])
    assert doc(store, "20101")[1] == "what was saved before" and doc(store, "20102") is None
    assert doc(store, "20103")[1].startswith("**Cy C (@cy)**") and saved(store) == {"20101", "20103", "20104", "20105"}
    assert store.db.execute("SELECT status, reason FROM urls WHERE doc_key = 'tweet:20103'").fetchone() == ("ok", None)
    tweetdb.import_files(store, [folder])  # again: nothing changes
    assert store.db.execute("SELECT COUNT(*) FROM urls").fetchone()[0] == 5


def test_databases_and_community_archive_uploads_still_import(store: Store, folder: Path):
    db = sqlite3.connect(folder / "tweets.db")
    db.execute("CREATE TABLE tweets (id TEXT, text TEXT, created_at TEXT, author_username TEXT, author_name TEXT, url TEXT, retweeted_text TEXT)")
    db.execute("INSERT INTO tweets VALUES ('30201', 'RT @cy: cut sh…', 'Wed Sep 16 22:18:18 +0000 2026', 'ada', 'Ada L', NULL, 'the whole of what cy said')")
    db.commit()
    db.close()
    (folder / "upload.json").write_text(json.dumps({
        "account": [{"account": {"username": "ada", "accountDisplayName": "Ada L"}}],
        "note-tweet": [{"noteTweet": {"createdAt": "2025-11-15T21:06:36.000Z", "core": {"text": LONG}}}],
        "tweets": [own("30301", LONG[:270] + "…", at="Sat Nov 15 21:06:36 +0000 2025"), own("30302", "short")],
        "community-tweet": [own("30303", "in a community")], "like": [{"like": {"tweetId": "30304"}}]}))
    assert [tweetdb.count_tweets(f) for f in tweetdb.find_files([folder])] == [("database", 1), ("archive", 3)]
    tweetdb.import_files(store, [folder])
    assert saved(store) == {"30201", "30301", "30302", "30303"}
    assert doc(store, "30201")[1] == "**Ada L (@ada)**\n\nRT @cy: cut sh…\n\nRetweeted @cy:\n\n> the whole of what cy said"
    assert doc(store, "30301")[1].endswith(LONG) and doc(store, "30201")[0]["date"] == "2026-09-16 22:18:18"
