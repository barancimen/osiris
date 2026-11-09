"""The web page's settings stick, turn into the right `osiris run` flags, and failures are ready to show."""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from osiris import __main__ as main
from osiris import generic, tuning, twitter, ui, youtube
from osiris.route import doc_id, route
from osiris.store import Store

OPTIONS = ui.DEFAULT_CONFIG["options"]


def options(**changes) -> dict:
    return {**json.loads(json.dumps(OPTIONS)), **changes}


def with_page(out: Path, check):
    async def main():
        app = ui.make_app(out)
        async with TestClient(TestServer(app), headers={"Cookie": f"osiris_ui={app['token']}"}) as client:
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


def test_older_settings_are_carried_over(tmp_path: Path):
    (tmp_path / "ui.json").write_text(json.dumps({"options": {"youtube_delay": "10", "user_agent": "typed by hand"}}))
    loaded = ui.load_config(tmp_path)["options"]
    assert (loaded["youtube_delay_min"], loaded["youtube_delay_max"]) == (7.0, 13.0)
    assert loaded["user_agent_edit"] is True  # so choosing a cookies file doesn't overwrite it


def test_defaults_add_no_tuning_flags():
    args = ui._run_args(options(), Path("urls"))
    assert not any(knob.flag in args for knob in tuning.KNOBS) and "--concurrency" not in args
    assert args[args.index("--youtube-delay") + 1] == "10-16"


def test_changed_numbers_become_flags():
    args = ui._run_args(options(tries=5, page_timeout="120", concurrency=8, workers=2, youtube_delay_min=9, youtube_delay_max=2), Path("urls"))
    joined = " ".join(args)
    assert "--tries 5" in joined and "--page-timeout 120" in joined and "--concurrency 8" in joined and "--workers 2" in joined
    assert "--youtube-delay 2-9" in joined  # the wrong way round still means a range


def test_an_emptied_box_means_the_default():
    assert "--tries" not in ui._run_args(options(tries=""), Path("urls"))


@pytest.mark.parametrize("change", [{"tries": 0}, {"tries": "lots"}, {"youtube_delay_min": -1}, {"page_timeout": "inf"}])
def test_bad_numbers_are_refused_before_a_run(change):
    with pytest.raises(web.HTTPBadRequest):
        ui._run_args(options(**change), Path("urls"))


def test_switches_become_flags():
    args = ui._run_args(options(scry_twitter=False, wayback=False, doi=False, impersonate=False, refetch_comments=True,
                                contact="me@example.org", chrome_profile="/copy"), Path("urls"))
    for flag in ("--no-scry-tweets", "--no-wayback", "--no-doi", "--no-impersonate", "--refetch-comments", "--contact"):
        assert flag in args
    assert "--chrome-profile" not in args  # only means anything with the headless browser
    assert "--chrome-profile" in ui._run_args(options(render=True, chrome_profile="/copy"), Path("urls"))


def test_comment_updates_can_skip_recently_fetched_threads():
    args = ui._run_args(options(refetch_comments=True, refetch_comments_days=14), Path("urls"))
    assert args[args.index("--refetch-comments-days") + 1] == "14"
    assert "--refetch-comments-days" not in ui._run_args(options(), Path("urls"))
    assert "--refetch-arxiv-pdfs" in ui._run_args(options(refetch_arxiv_pdfs=True), Path("urls"))


def test_scry_for_arxiv_is_opt_in_and_capped():
    assert "--scry-arxiv" not in ui._run_args(options(), Path("urls"))
    args = ui._run_args(options(scry_arxiv=True, scry_arxiv_dollars=2.5), Path("urls"))
    assert "--scry-arxiv" in args and args[args.index("--scry-arxiv-max-dollars") + 1] == "2.5"


def test_site_limits():
    rows = [*OPTIONS["host_limits"], {"pattern": "Example.com", "at_once": 2, "seconds": 1.5}, {"pattern": "", "at_once": 1, "seconds": 0}]
    args = ui._run_args(options(host_limits=rows), Path("urls"))
    assert [args[i + 1] for i, a in enumerate(args) if a == "--host-limit"] == ["scholar.google.*=1:8", "example.com=2:1.5"]
    with pytest.raises(web.HTTPBadRequest):
        ui._run_args(options(host_limits=[{"pattern": "a b", "at_once": 1, "seconds": 0}]), Path("urls"))


def test_a_cookies_file_that_has_gone_stops_the_run_with_a_reason(tmp_path: Path):
    with pytest.raises(web.HTTPBadRequest) as refused:
        ui._run_args(options(cookies=str(tmp_path / "gone.txt")), Path("urls"))
    assert "gone.txt" in refused.value.text


def test_the_picker_lists_possible_cookie_files_only_when_choosing_one(tmp_path: Path):
    for name in ("cookies.txt", "site_cookies", "export.json", "photo.jpg", ".hidden.txt"):
        (tmp_path / name).write_text("x")

    async def look(client, _):
        chosen = await (await client.get("/api/browse", params={"path": str(tmp_path), "files": "1"})).json()
        plain = await (await client.get("/api/browse", params={"path": str(tmp_path)})).json()
        typed = await (await client.get("/api/browse", params={"path": str(tmp_path / "photo.jpg"), "files": "1"})).json()
        return chosen, plain, typed

    (tmp_path / "out").mkdir()
    chosen, plain, typed = with_page(tmp_path / "out", look)
    assert chosen["file_names"] == ["cookies.txt", "export.json", "site_cookies"]
    assert plain["file_names"] == []
    assert typed == {"file": str(tmp_path / "photo.jpg"), "folder": str(tmp_path)}


def test_checking_a_cookies_file_reports_counts(tmp_path: Path):
    good = tmp_path / "c.txt"
    good.write_text(".example.com\tTRUE\t/\tTRUE\t0\tsid\tSECRET\nother.org\tFALSE\t/\tFALSE\t0\ta\tb\n")
    result = ui._scan_cookies(good)
    assert result == {"ok": True, "summary": "2 cookies for 2 sites"}
    (tmp_path / "bad.json").write_text("[1, 2]")
    assert ui._scan_cookies(tmp_path / "bad.json")["ok"] is False
    assert ui._scan_cookies(tmp_path / "none")["summary"] == "File not found"


def archive(tmp_path: Path, urls: list[str]) -> tuple[Store, list]:
    store = Store(tmp_path)
    routes = [route(u) for u in urls]
    store.add(routes)
    return store, routes


def test_failures_are_grouped_by_reason_with_the_latest_run_marked(tmp_path: Path):
    store, r = archive(tmp_path, ["https://a.example/1", "https://a.example/2", "https://b.example/3", "https://c.example/4"])
    for item, reason in zip(r[:3], ("http_403", "http_403", "dns")):
        store.record(item.doc_key, "failed", reason)
    store.record(r[3].doc_key, "ok", chars=500)
    store.db.execute("UPDATE urls SET updated_at = '2020-01-01T00:00:00Z' WHERE url = ?", (r[0].url,))
    store.commit()

    failures = ui.Failures(tmp_path)
    failures.note_run(1_700_000_000, None)
    failures.read()
    summary = failures.summary
    assert (summary["total"], summary["finished"]) == (3, 4)
    assert [(x["reason"], x["count"], x["run"]) for x in summary["reasons"]] == [("http_403", 2, 1), ("dns", 1, 1)]
    assert summary["reasons"][0]["hosts"] == [("a.example", 2)]
    assert (summary["run"]["failed"], summary["run"]["done"]) == (2, 3)

    page = failures.page(["http_403"], False, "", 0, 50)
    assert page["total"] == 2 and page["run"] == 1 and {it["id"] for it in page["items"]} == {r[0].doc_id, r[1].doc_id}
    assert [it["id"] for it in failures.page(["http_403"], True, "", 0, 50)["items"]] == [r[1].doc_id]
    assert failures.page([], False, "b.example", 0, 50)["total"] == 1
    assert failures.page(["dns", "http_403"], False, "", 0, 50)["items"][0]["reason"] == "dns"  # in the order asked for

    version = failures.version
    failures.read()
    assert failures.version == version  # nothing changed, so the page isn't sent anything new
    store.record(r[2].doc_key, "ok", chars=500)  # a later fallback got it
    store.commit()
    failures.read()
    assert failures.version != version and [x["reason"] for x in failures.summary["reasons"]] == ["http_403"]
    store.close()


def test_the_latest_run_is_remembered_across_a_restart(tmp_path: Path):
    Store(tmp_path).close()
    ui.Failures(tmp_path).note_run(1000.0, 2000.0)
    assert ui.Failures(tmp_path).run == (1000.0, 2000.0)


def test_two_links_to_one_document_are_one_failure_and_a_bad_address_is_harmless(tmp_path: Path):
    store, r = archive(tmp_path, ["https://a.example/1"])
    store.db.execute("INSERT INTO urls (url, lane, doc_key, doc_id, status, reason) VALUES (?, 'generic', ?, ?, 'pending', NULL)",
                     ("http://a.example/1?copy", r[0].doc_key, r[0].doc_id))
    store.db.execute("INSERT INTO urls (url, lane, doc_key, doc_id, status, reason) VALUES ('http://[bad/', 'generic', 'bad', 'badid', 'failed', NULL)")
    store.record(r[0].doc_key, "failed", "timeout")
    store.commit()
    failures = ui.Failures(tmp_path)
    failures.read()
    assert failures.summary["total"] == 2
    assert failures.by_reason["unknown"][0]["host"] == "unknown"
    store.close()


def test_the_page_only_gets_failures_when_they_change(tmp_path: Path):
    async def check(client, app):
        app["failures"].read()
        first = await (await client.get("/api/state")).json()
        again = await (await client.get("/api/state", params={"failures": first["failures"]["version"]})).json()
        return first["failures"], again["failures"]

    first, again = with_page(tmp_path, check)
    assert first["ready"] and first["total"] == 0 and again is None


def test_every_log_line_gets_one_timestamp(tmp_path: Path):
    job = ui.Job(tmp_path)
    for line in ("--- Archiving", "12:48:25 INFO already has one", ""):
        job.log(line)
    assert re.fullmatch(r"\d\d:\d\d:\d\d --- Archiving", job.lines[0])
    assert job.lines[1] == "12:48:25 INFO already has one" and job.lines[2] == "" and job.count == 3


def test_the_progress_line_counts_what_this_run_saved_by_source(tmp_path: Path, monkeypatch, caplog):
    store, r = archive(tmp_path, ["https://a.example/1", "https://a.example/2", "https://a.example/3", "https://x.com/a/status/1234567"])
    store.record(r[0].doc_key, "ok", chars=500)
    store.record(r[1].doc_key, "thin", "short", chars=5)
    store.record(r[2].doc_key, "failed", "dns")
    store.record(r[3].doc_key, "ok", chars=500)
    store.db.execute("UPDATE urls SET updated_at = '2020-01-01T00:00:00Z' WHERE url = ?", (r[3].url,))  # an earlier run's
    store.commit()
    assert main.downloaded_since(store, "2024-01-01T00:00:00Z") == {"generic": 2}

    monkeypatch.setattr(main, "REPORT_EVERY", 0.01)
    monkeypatch.setattr(main, "now_iso", lambda: "2024-01-01T00:00:00Z")

    async def report_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(main._report(store), 0.05)

    with caplog.at_level(logging.INFO, logger="osiris"):
        asyncio.run(report_briefly())
    assert "downloaded this run: generic 2 | " in caplog.text and "failed" not in caplog.text
    store.close()


def test_a_knob_sets_its_constant(monkeypatch):
    monkeypatch.setattr(generic, "MAX_ATTEMPTS", generic.MAX_ATTEMPTS)
    monkeypatch.setattr(tuning, "_chosen", {})
    tuning.set_constants({"--tries": 7})
    assert generic.MAX_ATTEMPTS == 7 and tuning.chosen() == {"--tries": 7}


def test_every_knob_points_at_a_real_number():
    for knob in tuning.KNOBS:
        assert isinstance(knob.default, (int, float)), knob.flag


def test_youtube_delay_ranges():
    assert youtube.parse_delay("2-9") == (2.0, 9.0)
    assert youtube.parse_delay("10") == pytest.approx((7.0, 13.0))
    with pytest.raises(ValueError):
        youtube.parse_delay("9-2")


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


def test_a_block_from_days_ago_is_forgotten(tmp_path: Path, monkeypatch):
    store, waits = lane(tmp_path, monkeypatch, fetched, urls=2)
    youtube._write_block(tmp_path, youtube.time.time() - 3 * 86400, 3)
    asyncio.run(youtube.run_youtube(store, (2.0, 9.0)))
    assert waits == [2.0]
    store.close()


def test_a_block_keeps_counting_until_enough_videos_go_through(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def fetch(_key):  # one success between two blocks is no reason to think it's over
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise youtube.Blocked("test")
        return fetched(_key)

    store, waits = lane(tmp_path, monkeypatch, fetch, urls=2)
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0)))
    assert [w for w in waits if w > 60] == [1800.0, 3600.0]
    store.close()


def test_losing_the_connection_is_not_a_block(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def fetch(_key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise youtube.Offline("ConnectionError")
        return fetched(_key)

    store, waits = lane(tmp_path, monkeypatch, fetch, urls=2)
    asyncio.run(youtube.run_youtube(store, (2.0, 9.0)))
    assert waits == [300.0, 2.0]  # back to its normal pace
    assert not (tmp_path / youtube.BLOCK_FILE).exists()
    store.close()


def test_the_earlier_pace_is_replaced_once_but_a_chosen_one_stays(tmp_path: Path):
    (tmp_path / "ui.json").write_text(json.dumps({"options": {"youtube_delay_min": 3.5, "youtube_delay_max": 6.5, "youtube_block_wait": 5.0}}))
    loaded = ui.load_config(tmp_path)["options"]
    assert (loaded["youtube_delay_min"], loaded["youtube_delay_max"], loaded["youtube_block_wait"]) == (10.0, 16.0, 30.0)
    (tmp_path / "ui.json").write_text(json.dumps({"options": {"youtube_delay_min": 2, "youtube_delay_max": 3}}))
    loaded = ui.load_config(tmp_path)["options"]
    assert (loaded["youtube_delay_min"], loaded["youtube_delay_max"]) == (2, 3)
    (tmp_path / "ui.json").write_text(json.dumps({"options": {"youtube_pace_updated": True, "youtube_delay_min": 3.5, "youtube_delay_max": 6.5}}))
    assert ui.load_config(tmp_path)["options"]["youtube_delay_min"] == 3.5


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


@pytest.mark.parametrize("error, kind", [("IpBlocked", youtube.Blocked), ("RequestBlocked", youtube.OneRefused)])
def test_only_the_bot_check_is_about_one_video(monkeypatch, error, kind):
    from youtube_transcript_api import YouTubeTranscriptApi, _errors

    def refuse(_self, video_id):
        raise getattr(_errors, error)(video_id)

    monkeypatch.setattr(YouTubeTranscriptApi, "list", refuse)
    with pytest.raises(youtube.Blocked) as caught:
        youtube.fetch_video("aaaaaaaaaaa")
    assert type(caught.value) is kind


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


def test_a_transcript_from_scry_never_touches_youtube(tmp_path: Path, monkeypatch):
    a, b = "a" * 11, "b" * 11
    scry = FakeScry({a: (1, LONG)}, titles={a: ("A title", "A channel")})
    asked = []
    store, _ = scry_lane(tmp_path, monkeypatch, scry, fetch=lambda key: asked.append(key) or fetched(key))
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0), 5.0))
    assert asked == [f"youtube:video:{b}"]
    meta, body = store.read_doc(doc_id(f"youtube:video:{a}"))
    assert meta["source"] == "youtube:transcript:auto:scry" and meta["author"] == "A channel"
    assert body.startswith("# A title") and "line 39 of what was said" in body
    assert store.db.execute("SELECT status FROM urls WHERE doc_key = ?", (f"youtube:video:{a}",)).fetchone()[0] == "ok"
    store.close()


def test_scry_is_only_asked_when_ticked(tmp_path: Path, monkeypatch):
    scry = FakeScry({})
    store, _ = scry_lane(tmp_path, monkeypatch, scry)
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0)))
    assert not scry.sql
    store.close()


def test_the_title_falls_back_to_the_older_census(tmp_path: Path, monkeypatch):
    a = "a" * 11
    scry = FakeScry({a: (0, LONG)}, census={a: ("Old title", "Old channel")})
    store, _ = scry_lane(tmp_path, monkeypatch, scry, urls=1)
    asyncio.run(youtube.run_youtube(store, (2.0, 2.0), 5.0))
    assert any("youtube.videos " in sql for sql in scry.sql)
    meta, _ = store.read_doc(doc_id(f"youtube:video:{a}"))
    assert meta["title"] == "Old title" and meta["source"] == "youtube:transcript:human:scry"
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


def test_sound_labels_go_from_captions_stored_on_one_line():
    assert youtube._scry_text("[Music] hello there [Applause] and goodbye") == "hello there and goodbye"
    assert youtube._scry_text("[Music]\nhello [sic] there\n[Applause]") == "hello [sic] there"


def test_a_big_batch_is_split_and_a_huge_transcript_left_out():
    limit = youtube.SCRY_BATCH_CHARS
    assert youtube._text_batches({"a": limit - 10, "b": 20, "c": limit + 1, "d": 5}) == [["a"], ["b", "d"]]


def test_the_page_asks_for_scry_transcripts_only_when_ticked():
    assert "--scry-youtube" not in ui._run_args(options(), Path("urls"))
    args = ui._run_args(options(scry_youtube=True, scry_youtube_dollars=2), Path("urls"))
    assert "--scry-youtube" in args and args[args.index("--scry-youtube-max-dollars") + 1] == "2"


def test_a_rate_limit_on_the_title_lookup_counts_as_a_block(monkeypatch):
    def refused(*_a, **_k):
        raise youtube.urllib.error.HTTPError("https://www.youtube.com/oembed", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(youtube.urllib.request, "urlopen", refused)
    with pytest.raises(youtube.Blocked):
        youtube._oembed("aaaaaaaaaaa")


def test_one_session_serves_every_video():
    youtube._session.cache_clear()
    assert youtube._session() is youtube._session()
    youtube._session.cache_clear()


def test_tweets_skip_scry_when_told_to(tmp_path: Path, monkeypatch):
    store, r = archive(tmp_path, ["https://x.com/a/status/1234567"])
    monkeypatch.delenv(twitter.KEY_ENV, raising=False)
    monkeypatch.setattr(twitter, "load_key", lambda: "a key")
    used = []

    async def from_scry(*a):
        used.append(a)
        return {}

    monkeypatch.setattr(twitter, "_from_scry", from_scry)
    asyncio.run(twitter.run_twitter(store, use_scry=False))
    assert not used
    assert store.db.execute("SELECT status FROM urls").fetchone()[0] == "pending"
    asyncio.run(twitter.run_twitter(store))
    assert used and store.db.execute("SELECT status, reason FROM urls").fetchone() == ("failed", "not_in_scry")
    store.close()
