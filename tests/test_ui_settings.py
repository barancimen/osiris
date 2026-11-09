"""The web page's settings stick, Scry stays opt-in and capped, and the YouTube lane backs off when blocked."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from osiris import ui, youtube
from osiris.route import route
from osiris.store import Store

OPTIONS = ui.DEFAULT_CONFIG["options"]


def options(**changes) -> dict:
    return {**json.loads(json.dumps(OPTIONS)), **changes}


def with_page(out: Path, check):
    async def main():
        app = ui.make_app(out)
        async with TestClient(TestServer(app)) as client:
            return await check(client, app)
    return asyncio.run(main())


def test_settings_changed_one_at_a_time_all_survive_a_restart(tmp_path: Path):
    async def change(client, _):
        for patch in ({"workers": 3}, {"retry_failed": True}, {"cookies": "/x/cookies.txt"}):
            assert (await client.post("/api/config", json={"options": patch})).status == 200
        assert (await client.post("/api/config", json={"local": ["/books"]})).status == 200

    with_page(tmp_path, change)
    config = ui.load_config(tmp_path)  # read from disk, as a restarted app does
    assert (config["options"]["workers"], config["options"]["retry_failed"]) == (3, True)
    assert config["options"]["cookies"] == "/x/cookies.txt" and config["local"] == ["/books"]


def test_scry_for_arxiv_is_opt_in_and_capped():
    assert "--scry-arxiv" not in ui._run_args(options(), Path("urls"))
    args = ui._run_args(options(scry_arxiv=True, scry_arxiv_dollars=2.5), Path("urls"))
    assert "--scry-arxiv" in args and args[args.index("--scry-arxiv-max-dollars") + 1] == "2.5"


def archive(tmp_path: Path, urls: list[str]) -> tuple[Store, list]:
    store = Store(tmp_path)
    routes = [route(u) for u in urls]
    store.add(routes)
    return store, routes


def test_youtube_waits_within_the_range_and_doubles_its_pause_after_blocks(tmp_path: Path, monkeypatch):
    store, r = archive(tmp_path, ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb"])
    calls = {"n": 0}

    def fetch(_key):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise youtube.Blocked("test")
        return {"status": "failed", "reason": "no_transcript"}

    waits, ranges = [], []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(youtube, "fetch", fetch)
    monkeypatch.setattr(youtube.asyncio, "sleep", no_wait)
    monkeypatch.setattr(youtube.random, "uniform", lambda a, b: ranges.append((a, b)) or a)
    asyncio.run(youtube.run_youtube(store, (2.0, 9.0)))
    assert waits == [1800.0, 3600.0, 4.0]  # at half speed now that it has been blocked
    assert ranges == [(2.0, 9.0)]
    store.close()


def lane(tmp_path: Path, monkeypatch, fetch, urls: int = 1) -> tuple[Store, list[float]]:
    store, _ = archive(tmp_path, [f"https://www.youtube.com/watch?v={chr(97 + n) * 11}" for n in range(urls)])
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(youtube, "fetch", fetch)
    monkeypatch.setattr(youtube.asyncio, "sleep", no_wait)
    monkeypatch.setattr(youtube.random, "uniform", lambda a, b: a)
    return store, waits


def fetched(_key):
    return {"status": "failed", "reason": "no_transcript"}


def test_a_new_run_waits_out_a_block_from_the_last_run_and_goes_slower(tmp_path: Path, monkeypatch):
    store, waits = lane(tmp_path, monkeypatch, fetched, urls=2)
    youtube._write_block(tmp_path, youtube.time.time() - 600, 1)  # blocked 10 minutes into a 30-minute pause
    asyncio.run(youtube.run_youtube(store, (2.0, 9.0)))
    assert waits[0] == pytest.approx(1200, abs=5) and waits[1:] == [4.0]
    store.close()


def test_one_video_refused_by_the_bot_check_does_not_stop_the_lane(tmp_path: Path, monkeypatch):
    seen = []

    def fetch(key):
        seen.append(key)
        if len(seen) == 1:
            raise youtube.OneRefused("RequestBlocked")
        return fetched(key)

    store, waits = lane(tmp_path, monkeypatch, fetch, urls=3)
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0)))
    assert len(set(seen)) == len(seen) == 3 and waits == [2.0, 2.0]  # no pause, and no second try
    assert store.db.execute("SELECT count(*) FROM urls WHERE reason = 'bot_check' AND status = 'failed'").fetchone()[0] == 1
    assert not (tmp_path / youtube.BLOCK_FILE).exists()
    store.close()


def test_two_videos_refused_in_a_row_are_a_block(tmp_path: Path, monkeypatch):
    seen = []

    def fetch(key):
        seen.append(key)
        if len(seen) <= 2:
            raise youtube.OneRefused("RequestBlocked")
        return fetched(key)

    store, waits = lane(tmp_path, monkeypatch, fetch, urls=2)
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0)))
    assert waits[:2] == [2.0, 1800.0] and (tmp_path / youtube.BLOCK_FILE).exists()
    assert store.db.execute("SELECT count(*) FROM urls WHERE reason = 'bot_check'").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM urls WHERE status = 'pending'").fetchone()[0] == 1  # the first still to do
    store.close()


class FakeScry:
    """Answers the queries the YouTube lane sends, from a table of video -> (auto, payload)."""

    def __init__(self, tracks, titles=None, census=None, cost=0, fail=False):
        self.tracks, self.titles, self.census, self.cost, self.fail = tracks, titles or {}, census or {}, cost, fail
        self.sql = []

    async def query(self, sql, max_seconds=60):
        self.sql.append(sql)
        if self.fail:
            raise youtube.ScryError("nope")
        youtube.ScryClient.spent_nanodollars += self.cost
        named = [v for v in re.findall(r"'([\w-]{11})'", sql)]
        if "youtube.videos_live" in sql:
            return [{"video_id": v, "title": self.titles[v][0], "channel": self.titles[v][1]} for v in named if v in self.titles]
        if "youtube.videos " in sql:
            return [{"video_id": v, "title": self.census[v][0], "channel": self.census[v][1]} for v in named if v in self.census]
        if "length(payload)" in sql.split("FROM")[0]:
            return [{"video_id": v, "chars": len(self.tracks[v][1])} for v in named if v in self.tracks]
        return [{"video_id": v, "auto": self.tracks[v][0], "payload": self.tracks[v][1]} for v in named if v in self.tracks]


def scry_lane(tmp_path: Path, monkeypatch, scry: FakeScry, fetch=None, urls: int = 2):
    store, waits = lane(tmp_path, monkeypatch, fetch or fetched, urls=urls)
    monkeypatch.setattr(youtube, "load_key", lambda: "a key")
    monkeypatch.setattr(youtube.ScryClient, "query", scry.query)
    return store, waits


LONG = "\n".join(f"line {n} of what was said in this video" for n in range(40))


def test_scry_is_only_asked_when_ticked(tmp_path: Path, monkeypatch):
    scry = FakeScry({})
    store, _ = scry_lane(tmp_path, monkeypatch, scry)
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0)))
    assert not scry.sql
    store.close()


def test_scry_failing_or_the_limit_leaves_videos_for_youtube(tmp_path: Path, monkeypatch):
    a = "a" * 11
    for scry in (FakeScry({a: (1, LONG)}, fail=True), FakeScry({a: (1, LONG)}, cost=int(6e9))):
        folder = tmp_path / str(id(scry))
        folder.mkdir()
        asked = []
        store, _ = scry_lane(folder, monkeypatch, scry, fetch=lambda key: asked.append(key) or fetched(key), urls=1)
        asyncio.run(youtube.run_youtube(store, (2.0, 2.0), 5.0))
        assert asked == [f"youtube:video:{a}"]
        store.close()


def test_damaged_human_captions_are_refused_and_preference_order_is_kept():
    sql = youtube._best_track("payload", ["a" * 11])
    assert "is_auto_caption = 0 AND position(payload, char(10)) = 0 AND length(event_starts_ms) > 1" in sql and "NOT (" in sql
    assert sql.index("startsWith(lang") < sql.index("is_auto_caption ASC") < sql.index("LIMIT 1 BY video_id")


def test_the_page_asks_for_scry_transcripts_only_when_ticked():
    assert "--scry-youtube" not in ui._run_args(options(), Path("urls"))
    args = ui._run_args(options(scry_youtube=True, scry_youtube_dollars=2), Path("urls"))
    assert "--scry-youtube" in args and args[args.index("--scry-youtube-max-dollars") + 1] == "2"
