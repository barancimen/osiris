"""Every tweet of the accounts in a list, from twitterapi.io and from Scry, each paid for once."""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from osiris import accounts, scry as scry_api, twitter, ui
from osiris.route import doc_id, route
from osiris.scry import ScryClient, ScryError
from osiris.store import Store

ADA, BOB = "4001", "4002"  # account ids


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    for key in ("SCRY_API_KEY", twitter.KEY_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ScryClient, "spent_nanodollars", 0)
    monkeypatch.setattr(accounts, "PAGE_DELAY", 0)
    monkeypatch.setattr(twitter, "PAGE_DELAY", 0)
    for service in ("BASE_URL", "API_URL", "TOOL_URL"):  # nothing listens there
        monkeypatch.setattr(twitter if service == "BASE_URL" else scry_api, service, "http://127.0.0.1:9")


def doc(store: Store, tid) -> tuple[dict, str] | None:
    return store.read_doc(doc_id(f"tweet:{tid}"))


def saved(store: Store) -> set[int]:
    return {int(key.split(":")[1]) for (key,) in store.db.execute("SELECT doc_key FROM urls WHERE status = 'ok'")}


def state(store: Store, handle: str) -> dict:
    return accounts._state(store, handle)


# -- the lists -----------------------------------------------------------------------------------

def test_a_list_is_usernames_one_per_line(tmp_path: Path):
    assert accounts.parse_list("# mine\n\nada\n@Bob  # a friend\nhttps://x.com/cy\ntwitter.com/Dee/\nhttps://twitter.com/eve?s=20\n") \
        == ["ada", "Bob", "cy", "Dee", "eve"]
    for line in ("ada lovelace", "https://example.com/ada", "a_name_longer_than_fifteen", "https://x.com/home", "ada/status/12345"):
        with pytest.raises(accounts.NotAList):
            accounts.parse_list(f"{line}\n")


def test_only_accounts_txt_at_the_top_of_a_folder_is_read(tmp_path: Path, caplog):
    (tmp_path / "sub").mkdir()
    (tmp_path / "accounts.txt").write_text("ada\nBob\nbob\n")
    (tmp_path / "people.txt").write_text("cy\n")
    (tmp_path / "sub" / "accounts.txt").write_text("dee\n")
    assert accounts.listed([tmp_path]) == ["ada", "Bob"]  # each once, whatever its capitals
    assert accounts.listed([tmp_path / "people.txt"]) == ["cy"]  # a list named outright can have any name
    (tmp_path / "accounts.txt").write_text("dee\neve adams\nfay\n")
    with caplog.at_level(logging.INFO):
        assert accounts.listed([tmp_path]) == []
    said = {r.levelname: r.getMessage() for r in caplog.records}
    assert "accounts.txt" in said["WARNING"] and "line 2" in said["WARNING"]


# -- twitterapi.io, stood in for by a local server -------------------------------------------------

def api_tweet(tid: int, user: str = "ada", text: str | None = None, **more) -> dict:
    return {"type": "tweet", "id": str(tid), "url": f"https://x.com/{user}/status/{tid}", "text": text or f"tweet {tid}",
            "createdAt": "Tue Dec 10 07:00:30 +0000 2024", "author": {"userName": user, "name": user.title()}, **more}


class Twitterapi:
    """twitterapi.io's user timeline: pages of `size` tweets, newest first, with the cursor at the top level.
    A cursor is the last tweet of the page before, as a real one marks a place in the timeline."""

    def __init__(self, timelines: dict[str, list[dict]], size: int = 3, pins: dict[str, dict] | None = None,
                 repeats_at_the_end: bool = False):
        self.timelines, self.size, self.pins = timelines, size, pins or {}
        self.repeats_at_the_end = repeats_at_the_end  # the last page comes back for ever, each time under a new cursor
        self.refused: set[tuple[str, str]] = set()
        self.asked: list[tuple[str, str]] = []

    async def last_tweets(self, request: web.Request) -> web.Response:
        assert request.headers["X-API-Key"] == "paid" and request.query["includeReplies"] == "true"
        assert request.query.get("cursor", "x")  # a first page is asked for without a cursor, not with an empty one
        handle, cursor = request.query["userName"].lower(), request.query.get("cursor", "")
        self.asked.append((handle, cursor))
        if (handle, cursor) in self.refused:
            return web.json_response({"error": 403, "message": "no"}, status=403)
        if handle not in self.timelines:
            return web.json_response({"status": "error", "msg": "user not found", "data": None})
        place, _, again = cursor.partition("r")
        if cursor and not place.isdigit():
            return web.json_response({"error": 400, "message": "bad cursor"}, status=400)
        timeline = sorted(self.timelines[handle], key=lambda tweet: -int(tweet["id"]))
        below = [tweet for tweet in timeline if not place or int(tweet["id"]) < int(place)]
        tweets, more, following = below[: self.size], len(below) > self.size, ""
        if more:
            following = tweets[-1]["id"]
        elif self.repeats_at_the_end:
            tweets = timeline[-self.size:] if again or not tweets else tweets
            more, following = True, f"{timeline[-1]['id']}r{int(again or 0) + 1}"
        return web.json_response({"status": "success", "code": 0, "msg": "success",
                                  "data": {"pin_tweet": self.pins.get(handle), "tweets": tweets},
                                  "has_next_page": more, "next_cursor": following})


def run(store: Store, handles: list[str], api: Twitterapi | None = None, monkeypatch=None, **options) -> None:
    async def main():
        server = None
        if api:
            app = web.Application()
            app.add_routes([web.get("/twitter/user/last_tweets", api.last_tweets)])
            server = TestServer(app)
            await server.start_server()
            monkeypatch.setattr(twitter, "BASE_URL", str(server.make_url("")).rstrip("/"))
            monkeypatch.setenv(twitter.KEY_ENV, "paid")
        try:
            await twitter.run_twitter(store, accounts=handles, **options)
        finally:
            if server:
                await server.close()
    asyncio.run(main())


ONE_PAGE = 3 * accounts.TWEET_NANODOLLARS / 1e9  # what a page of the stand-in's costs, in dollars


def test_twitterapi_walks_a_timeline_once_and_then_only_takes_what_is_new(store: Store, monkeypatch):
    timeline = [api_tweet(tid) for tid in range(10007, 10000, -1)]
    timeline[2] = api_tweet(10005, text="@bob a reply", isReply=True)
    timeline[4] = api_tweet(10003, text="RT @bob: cut sh…", retweeted_tweet=api_tweet(777, "bob", "all of what bob said"))
    api = Twitterapi({"ada": timeline}, pins={"ada": api_tweet(10001)})
    run(store, ["Ada"], api, monkeypatch)
    assert saved(store) == set(range(10001, 10008)) and api.asked == [("ada", ""), ("ada", "10005"), ("ada", "10002")]
    meta, body = doc(store, 10003)
    assert meta["source"] == "tweet:twitterapi" and body.endswith("Retweeted @bob:\n\n> all of what bob said")
    assert state(store, "ada")["api_newest"] == 10007 and state(store, "ada")["checked_at"] and not state(store, "ada")["api_cursor"]

    api.timelines["ada"] = [api_tweet(10009), api_tweet(10008), *timeline]
    api.asked.clear()
    run(store, ["Ada"], api, monkeypatch)
    assert saved(store) == set(range(10001, 10010))
    assert api.asked == [("ada", ""), ("ada", "10007")]  # it stops at a page it has all of, not at the account's first tweet
    assert state(store, "ada")["api_newest"] == 10009


def test_twitterapi_stops_at_its_limit_and_the_next_run_carries_on_from_there(store: Store, monkeypatch, caplog):
    api = Twitterapi({"ada": [api_tweet(tid) for tid in range(10010, 10000, -1)], "bob": [api_tweet(20001, "bob")]},
                     pins={"ada": api_tweet(10001)})
    with caplog.at_level(logging.WARNING):
        run(store, ["ada", "bob"], api, monkeypatch, api_dollars=2 * ONE_PAGE)
    assert api.asked == [("ada", ""), ("ada", "10008")]
    assert saved(store) == {*range(10005, 10011), 10001}  # what was paid for is kept, the pinned tweet with it
    assert state(store, "ada")["api_cursor"] == "10005" and not state(store, "ada")["checked_at"] and not state(store, "bob")["checked_at"]
    assert "limit for twitterapi.io" in caplog.text and "1 account left for the next run" in caplog.text

    api.timelines["ada"] += [api_tweet(10012), api_tweet(10011)]  # posted since that walk began
    api.asked.clear()
    run(store, ["ada", "bob"], api, monkeypatch)
    # the rest of the walk, without a page of it paid for twice, then what is new, down to where the walk began
    assert api.asked == [("ada", "10005"), ("ada", "10002"), ("ada", ""), ("ada", "10010"), ("bob", "")]
    assert saved(store) == {*range(10001, 10013), 20001}
    assert state(store, "ada")["api_newest"] == 10012 and state(store, "ada")["api_cursor"] is None and state(store, "bob")["checked_at"]


def test_each_page_is_saved_before_the_next_is_asked_for(store: Store, monkeypatch, caplog):
    api = Twitterapi({"ada": [api_tweet(tid) for tid in range(10010, 10000, -1)]})
    api.refused.add(("ada", "10005"))
    with caplog.at_level(logging.ERROR):
        run(store, ["ada"], api, monkeypatch)
    assert "@ada: twitterapi.io failed" in caplog.text and saved(store) == set(range(10005, 10011))
    assert state(store, "ada")["api_cursor"] == "10005" and state(store, "ada")["api_top"] == 10010
    api.refused.clear()
    api.asked.clear()
    run(store, ["ada"], api, monkeypatch)
    assert api.asked[:2] == [("ada", "10005"), ("ada", "10002")] and saved(store) == set(range(10001, 10011))


def test_a_timeline_that_ends_by_repeating_its_last_page_is_finished(store: Store, monkeypatch):
    api = Twitterapi({"ada": [api_tweet(tid) for tid in range(10005, 10000, -1)]}, repeats_at_the_end=True)
    run(store, ["ada"], api, monkeypatch, api_dollars=100 * ONE_PAGE)
    assert api.asked == [("ada", ""), ("ada", "10003"), ("ada", "10001r1")]  # not page after page until the money is gone
    assert saved(store) == set(range(10001, 10006))
    assert state(store, "ada")["api_newest"] == 10005 and state(store, "ada")["api_cursor"] is None and state(store, "ada")["checked_at"]
    api.asked.clear()
    run(store, ["ada"], api, monkeypatch)
    assert api.asked == [("ada", "")]


def test_an_account_twitterapi_does_not_have_holds_up_nothing(store: Store, monkeypatch, caplog):
    api = Twitterapi({"bob": [api_tweet(20001, "bob")]})
    with caplog.at_level(logging.WARNING):
        run(store, ["gone", "bob"], api, monkeypatch)
    assert saved(store) == {20001} and caplog.text.count("@gone: twitterapi.io says: user not found") == 1


def test_a_place_twitterapi_no_longer_takes_starts_the_walk_again(store: Store, monkeypatch):
    api = Twitterapi({"ada": [api_tweet(tid) for tid in range(10004, 10000, -1)]})
    accounts._remember(store, "ada", api_cursor="expired", api_top=10004)
    run(store, ["ada"], api, monkeypatch)
    assert api.asked == [("ada", "expired"), ("ada", ""), ("ada", "10002")] and saved(store) == {10001, 10002, 10003, 10004}


# -- Scry, stood in for by its own queries ---------------------------------------------------------

class Scry:
    """Scry's Twitter relations, answering the queries osiris sends and charging for each."""

    def __init__(self, monkeypatch, price: int = 0, most: int = 1000, most_chars: int = 10**9):
        self.handles: dict[str, list[str]] = {}  # lowercase username: the accounts that have posted under it, latest first
        self.users: dict[str, tuple[str, str]] = {}
        self.tweets: dict[int, dict] = {}
        self.price, self.most = price, most  # nanodollars a query, and the most tweets it will send at once
        self.most_chars = most_chars  # the most text it will send in one answer
        self.asked: list[tuple[str, list[int]]] = []  # the tweets asked for, with the account they were asked of
        self.listed = 0
        self.collecting: dict[str, dict] = {}  # lowercase username: the account it was asked to collect, and how that stands
        self.collect_asked: list[list[str]] = []
        self.collects = True
        self.will_not_collect: set[str] = set()  # accounts it has tweets of and still says it doesn't know
        monkeypatch.setenv("SCRY_API_KEY", "key")
        monkeypatch.setattr(ScryClient, "query", lambda client, sql, max_seconds=60: self.query(sql))
        monkeypatch.setattr(ScryClient, "tool", lambda client, name, arguments: self.tool(name, arguments))

    async def tool(self, name: str, arguments: dict) -> dict:
        assert name == "twitter_watch"
        if not self.collects:
            raise ScryError("twitter_watch: not for this key")
        if arguments["action"] == "list":
            return {"rows": list(self.collecting.values())}
        assert arguments["action"] == "watch" and arguments["cadence_secs"] == 86400 and len(arguments["handles"]) <= 100
        self.collect_asked.append(arguments["handles"])
        rows = []
        for handle in arguments["handles"]:  # it takes only accounts it has met
            known = handle.lower() in self.handles and handle.lower() not in self.will_not_collect
            rows.append({"handle": handle.lower(), "status": "watching" if known else "unknown_handle", "history_walked_at": None})
            if known:
                self.collecting[handle.lower()] = rows[-1]
        return {"rows": rows}

    def account(self, aid: str, handle: str, name: str, tweets: dict[int, str | dict]) -> None:
        self.handles.setdefault(handle.lower(), []).insert(0, aid)
        self.users[aid] = (handle, name)
        for tid, text in tweets.items():
            self.tweets[tid] = {"author_id": aid, "author_handle": handle, **(text if isinstance(text, dict) else {"text": text})}

    async def query(self, sql: str) -> list[dict]:
        ScryClient.spent_nanodollars += self.price
        numbers = lambda text: [int(n) for n in re.findall(r"\d+", text)]
        if "FROM twitter.author_handles" in sql:
            wanted = re.findall(r"'(\w+)'", sql)
            return [{"handle": h, "author_id": aid} for h in wanted for aid in self.handles.get(h, ())]
        if "FROM twitter.users" in sql:
            ids = map(str, numbers(sql.split(" IN (")[1].split(")")[0]))
            return [{"author_id": aid, "handle": self.users[aid][0], "name": self.users[aid][1]} for aid in ids if aid in self.users]
        if m := re.search(r"FROM twitter.tweets_by_author WHERE author_id = (\d+) AND tweet_id (<=|>) (\d+)", sql):
            aid, bound = m.group(1), int(m.group(3))
            if m.group(2) == "<=":
                return [{"n": sum(t["author_id"] == aid and tid <= bound for tid, t in self.tweets.items())}]
            self.listed += 1
            limit = int(sql.rsplit("LIMIT ", 1)[1])
            return [{"id": str(tid)} for tid in sorted(self.tweets) if self.tweets[tid]["author_id"] == aid and tid > bound][:limit]
        m = re.search(r"FROM twitter.tweets WHERE (?:author_id = (\d+) AND )?tweet_id IN \(([\d,]+)\) GROUP BY tweet_id LIMIT (\d+)", sql)
        assert m, sql
        ids = numbers(m.group(2))
        assert int(m.group(3)) == len(ids)
        mine = [self.tweets[tid] for tid in ids if tid in self.tweets and (not m.group(1) or self.tweets[tid]["author_id"] == m.group(1))]
        if piece := re.search(r"substringUTF8\(argMax\((text|article_text), .*?\), (\d+), (\d+)\) AS piece", sql):
            start, length = int(piece.group(2)) - 1, int(piece.group(3))
            return [{"piece": t.get(piece.group(1), "")[start : start + length]} for t in mine]
        whole = " AS text" in sql  # with each tweet's text and article, not only the facts about it
        if len(ids) > self.most or (whole and sum(len(t["text"]) + len(t.get("article_text", "")) for t in mine) > self.most_chars):
            raise ScryError("query_resource_limit_exceeded: too much", "query_resource_limit_exceeded")
        if not whole:
            return [{"id": str(tid), "author_id": t["author_id"], "author_handle": t.get("author_handle", ""), "author_display_name": "",
                     "created": "2026-06-25 13:14:08.000000", "quoted_id": "0", "article_title": t.get("article_title", "")}
                    for tid in ids if (t := self.tweets.get(tid)) in mine and t]
        self.asked.append((m.group(1) or "", ids))
        return [{"id": str(tid), "author_id": t["author_id"], "author_handle": t.get("author_handle", ""), "author_display_name": "",
                 "created": "2026-06-25 13:14:08.000000", "text": t["text"], "quoted_id": str(t.get("quoted", 0)),
                 "article_title": t.get("article_title", ""), "article_text": t.get("article_text", "")}
                for tid in ids if (t := self.tweets.get(tid)) and (not m.group(1) or t["author_id"] == m.group(1))]


def test_scry_gives_what_it_has_of_an_account_and_is_asked_for_each_tweet_once(store: Store, monkeypatch):
    scry = Scry(monkeypatch)
    scry.account(BOB, "bob", "Bob B", {555: "what bob said"})
    scry.account(ADA, "Ada", "Ada L", {10001: "first", 10002: {"text": "seen without my name", "author_handle": ""},
                                      10003: {"text": "look", "quoted": 555}, 10004: {"text": "quoting one that's gone", "quoted": 556}})
    run(store, ["ada", "nobody"])
    assert saved(store) == {10001, 10002, 10003, 10004}  # bob's tweet is quoted, not saved as a document
    meta, body = doc(store, 10003)
    assert meta["source"] == "tweet:scry" and meta["url"] == "https://x.com/Ada/status/10003" and meta["date"] == "2026-06-25 13:14:08"
    assert body == "**Ada L (@Ada)**\n\nlook\n\nQuoting @bob:\n\n> what bob said"
    assert doc(store, 10002)[0]["author"] == "Ada L (@Ada)"
    assert scry.asked == [(ADA, [10001, 10002, 10003, 10004]), ("", [555, 556])]  # the account's own, read the cheap way
    assert state(store, "ada")["scry_after"] == 10004 and state(store, "ada")["scry_count"] == 4 and state(store, "nobody")["checked_at"]

    scry.asked.clear()
    run(store, ["ada"])
    assert scry.asked == []

    scry.tweets[10005] = {"author_id": ADA, "author_handle": "Ada", "text": "a new one"}
    run(store, ["ada"])
    assert scry.asked == [(ADA, [10005])] and saved(store) == {10001, 10002, 10003, 10004, 10005}

    scry.asked.clear()
    scry.tweets[9999] = {"author_id": ADA, "author_handle": "Ada", "text": "an old one Scry has only now found"}
    run(store, ["ada"])
    assert scry.asked == [(ADA, [9999])] and doc(store, 9999)[1].endswith("an old one Scry has only now found")
    assert state(store, "ada")["scry_count"] == 6


def test_with_scry_alone_it_is_asked_to_collect_each_account_once(store: Store, monkeypatch, caplog):
    scry = Scry(monkeypatch)
    scry.account(ADA, "ada", "Ada L", {10001: "the one tweet Scry happened to have"})
    scry.account(BOB, "bob", "Bob B", {20001: "from an account that has since gone"})
    scry.will_not_collect.add("bob")
    with caplog.at_level(logging.INFO):
        run(store, ["Ada", "newbie", "bob"])
    assert scry.collect_asked == [["Ada", "newbie", "bob"]] and set(scry.collecting) == {"ada"} and saved(store) == {10001, 20001}
    assert "asked Scry to collect 1 account" in caplog.text and "@Ada: Scry is still walking back" in caplog.text
    assert "@newbie: Scry has never seen this account" in caplog.text
    assert "@bob: Scry has some of this account's tweets but wouldn't take it to collect (unknown handle)" in caplog.text

    scry.collecting["ada"]["history_walked_at"] = "2026-11-25T03:00:00Z"  # its walk is done, and found an older tweet
    scry.tweets[10000] = {"author_id": ADA, "author_handle": "ada", "text": "found on the walk"}
    scry.collect_asked.clear()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        run(store, ["Ada", "newbie"])
    assert scry.collect_asked == [["newbie"]]  # ada is being collected already; newbie may have turned up since
    assert saved(store) == {10000, 10001, 20001} and "still walking" not in caplog.text and "asked Scry to collect" not in caplog.text


def test_a_key_that_cannot_ask_scry_to_collect_still_takes_what_scry_has(store: Store, monkeypatch, caplog):
    scry = Scry(monkeypatch)
    scry.collects = False
    scry.account(ADA, "ada", "Ada L", {10001: "one"})
    with caplog.at_level(logging.INFO):
        run(store, ["ada"])
    assert saved(store) == {10001} and "couldn't be asked to collect" in caplog.text and state(store, "ada")["checked_at"]


def test_scry_stops_at_its_limit_and_the_next_run_carries_on(store: Store, monkeypatch, caplog):
    monkeypatch.setattr(twitter, "SCRY_BATCH", 2)
    scry = Scry(monkeypatch, price=1_000_000)
    scry.account(ADA, "ada", "Ada L", {tid: f"tweet {tid}" for tid in range(10001, 10010)})
    with caplog.at_level(logging.WARNING):
        run(store, ["ada"], scry_dollars=0.0045)  # the two lookups of the account, a listing, and two batches of tweets
    assert saved(store) == {10001, 10002, 10003, 10004} and "limit for Scry" in caplog.text
    assert state(store, "ada")["scry_after"] == 10004 and state(store, "ada")["scry_count"] == 4 and not state(store, "ada")["checked_at"]
    run(store, ["ada"], scry_dollars=1)
    assert saved(store) == set(range(10001, 10010)) and state(store, "ada")["checked_at"]
    assert sorted(tid for _, ids in scry.asked for tid in ids) == list(range(10001, 10010))  # none asked for twice


def test_an_answer_scry_calls_too_long_is_asked_for_in_smaller_parts(store: Store, monkeypatch):
    scry = Scry(monkeypatch, most=2)
    scry.account(ADA, "ada", "Ada L", {tid: f"tweet {tid}" for tid in range(10001, 10012)})
    run(store, ["ada"])
    assert saved(store) == set(range(10001, 10012)) and all(len(ids) <= 2 for _, ids in scry.asked)


def test_a_tweet_too_long_for_one_answer_comes_in_pieces(store: Store, monkeypatch):
    monkeypatch.setattr(twitter, "SCRY_PIECE", 100)
    article = "".join(f"Paragraph {n} of a long article, with ünïcödé in it.\n" for n in range(40))
    scry = Scry(monkeypatch, most_chars=150)
    scry.account(ADA, "ada", "Ada L", {10001: "short", 10002: {"text": "My article", "article_title": "On length", "article_text": article},
                                      10003: "x" * 300, 10004: "short again"})
    run(store, ["ada"])
    assert saved(store) == {10001, 10002, 10003, 10004}
    assert doc(store, 10002)[1] == "**Ada L (@ada)**\n\nMy article\n\n## On length\n\n" + article.rstrip()
    assert doc(store, 10003)[1].endswith("\n\n" + "x" * 300)  # a length that is exactly a number of pieces


def test_with_both_turned_on_scry_adds_what_twitterapi_did_not_have(store: Store, monkeypatch):
    api = Twitterapi({"ada": [api_tweet(tid) for tid in (10005, 10004, 10003)]})
    scry = Scry(monkeypatch)
    scry.account(ADA, "ada", "Ada L", {10001: "deleted since", 10002: "also deleted", 10003: "still up", 10004: "still up too"})
    run(store, ["ada"], api, monkeypatch)
    assert saved(store) == {10001, 10002, 10003, 10004, 10005} and scry.asked == [(ADA, [10001, 10002])]
    assert scry.collect_asked == []  # twitterapi.io has the timeline, so Scry isn't asked to walk it too
    assert [doc(store, tid)[0]["source"] for tid in (10001, 10003, 10005)] == ["tweet:scry", "tweet:twitterapi", "tweet:twitterapi"]


def test_with_neither_turned_on_the_accounts_are_skipped(store: Store, monkeypatch, caplog):
    scry = Scry(monkeypatch)
    scry.account(ADA, "ada", "Ada L", {10001: "a tweet"})
    with caplog.at_level(logging.WARNING):
        run(store, ["ada"], use_scry=False)  # Scry has a key but is turned off, and twitterapi.io has no key
    assert saved(store) == set() and "1 account skipped" in caplog.text and scry.listed == 0 and scry.collect_asked == []


# -- tweets you saved a link to --------------------------------------------------------------------

def test_saved_links_are_looked_up_in_scry_until_its_money_runs_out(store: Store, monkeypatch, caplog):
    monkeypatch.setattr(twitter, "SCRY_BATCH", 2)
    scry = Scry(monkeypatch, price=1_000_000)
    scry.account(ADA, "ada", "Ada L", {10001: "one", 10002: "two", 10004: "four"})
    store.add([route(f"https://x.com/ada/status/{tid}") for tid in range(10001, 10006)])
    with caplog.at_level(logging.WARNING):
        run(store, [], scry_dollars=0.0015)  # enough for two of the three batches
    status = lambda: dict(store.db.execute("SELECT CAST(substr(doc_key, 7) AS INTEGER), status || ':' || COALESCE(reason, '') FROM urls"))
    assert status() == {10001: "ok:", 10002: "ok:", 10003: "failed:not_in_scry", 10004: "ok:", 10005: "pending:"}
    assert "limit for Scry; 1 tweets wait" in caplog.text and scry.asked[0] == ("", [10001, 10002])
    run(store, [])
    assert status()[10005] == "failed:not_in_scry"


# -- the page --------------------------------------------------------------------------------------

def test_the_page_says_what_a_tweet_folder_holds_and_counts_its_accounts(store: Store, tmp_path: Path):
    folder = tmp_path / "tweets"
    folder.mkdir()
    assert ui._scan_folder(folder, "tweets") == {"ok": False, "summary": "No tweet files or lists of accounts here"}
    (folder / "accounts.txt").write_text("ada\nbob\n")
    (folder / "b.json").write_text(json.dumps([{"id": "20101", "screen_name": "ann", "name": "Ann", "full_text": "hello"}]))
    assert ui._scan_folder(folder, "tweets") == {"ok": True, "summary": "1 bookmarks file, 1 tweets, 2 accounts listed"}

    options = json.loads(json.dumps(ui.DEFAULT_CONFIG["options"]))
    assert "--scry-tweets-max-dollars" not in ui._run_args(options, Path("urls"))
    args = ui._run_args({**options, "scry_twitter_dollars": 0.25}, Path("urls"))
    assert args[args.index("--scry-tweets-max-dollars") + 1] == "0.25"

    plan = ui._plan(["twitter", "generic"], options, ["Ada", "bob"])
    left = lambda: next(s["left"] for s in ui._progress(store.out, plan, 0)["stages"] if s["stage"] == "accounts")
    assert left() == 2
    accounts._remember(store, "ada", checked_at="9999")  # this run got to the end of ada's tweets
    accounts._remember(store, "bob", checked_at="2000-01-01T00:00:00Z")  # an earlier run did bob's
    store.commit()
    assert left() == 1
    assert all(s["stage"] != "accounts" for s in ui._progress(store.out, ui._plan(["generic"], options, ["ada"]), 0)["stages"])
