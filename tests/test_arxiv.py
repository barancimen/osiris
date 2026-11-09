"""arXiv full text, and which originals a run keeps."""

import asyncio
from pathlib import Path

import pytest

from osiris import arxiv, extract
from osiris.generic import Fetched, GenericPass
from osiris.route import doc_id, route
from osiris.store import Store

KEY = "arxiv:2402.02637"
PAPER = "Body of the paper. " * 100


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


class Pool:
    async def run(self, worker, *args):
        return worker(*args)


@pytest.fixture(autouse=True)
def fake_extractors(monkeypatch):
    monkeypatch.setattr(extract, "html_to_doc", lambda body, url: {"text": "# Title\n\n" + body.decode()})
    monkeypatch.setattr(extract, "pdf_to_doc", lambda path: {"text": Path(path).read_text()})


def abstract_doc(store: Store) -> str:
    store.add([route("https://arxiv.org/pdf/2402.02637.pdf")])
    meta, body = arxiv.paper_markdown("2402.02637", {"title": "T", "abstract": "The abstract.", "authors": ["A"],
                                                     "published": "2024-02-04", "category": "cs.LG"})
    chars = store.write_doc(KEY, meta, body)
    store.record(KEY, "ok", chars=chars)
    store.commit()
    return doc_id(KEY)


def html(body=PAPER + "ltx_page"):
    return Fetched(status=200, final_url="https://arxiv.org/html/x", content_type="text/html", kind="html", body=body.encode())


def missing(status=404):
    return Fetched(status=status, error=f"http_{status}")


def pdf(tmp, text=PAPER):
    path = Path(tmp) / "download.pdf"
    path.write_text(text)
    return Fetched(status=200, final_url="https://arxiv.org/pdf/x", content_type="application/pdf", kind="pdf", pdf_path=str(path))


def full_text(store, answers, keep_raw=False):
    ft = arxiv.FullText(store, Pool(), keep_raw)
    asked = []

    async def get(session, url):
        asked.append(url)
        answer = answers[url.split("/")[3]]
        if answer.pdf_path:
            await ft.reader.pdf_slots.acquire()
        return answer

    ft.get = get
    return ft, asked


def run(ft, key=KEY):
    return asyncio.run(ft.one(None, key))


@pytest.mark.parametrize("url", ["https://arxiv.org/abs/2402.02637", "https://arxiv.org/pdf/2402.02637v2.pdf",
                                 "https://arxiv.org/html/2402.02637v1"])
def test_every_kind_of_link_is_the_same_paper(url):
    assert route(url).lane == "arxiv" and route(url).doc_key == KEY


def test_html_comes_first_and_the_abstract_stays(store):
    did = abstract_doc(store)
    ft, asked = full_text(store, {"html": html()})
    assert run(ft)
    assert asked == ["https://arxiv.org/html/2402.02637"]
    meta, body = store.read_doc(did)
    assert meta["source"] == "html:arxiv" and "raw" not in meta
    assert "## Abstract" in body and "The abstract." in body and "## Full text" in body and "Body of the paper." in body
    assert "# Title" not in body.partition("## Full text")[2]
    assert store.done_without("arxiv", arxiv.FULLTEXT) == []


def test_pdf_when_there_is_no_html(store, tmp_path):
    did = abstract_doc(store)
    ft, asked = full_text(store, {"html": missing(), "pdf": pdf(tmp_path)})
    assert run(ft)
    assert asked[1] == "https://arxiv.org/pdf/2402.02637"
    assert store.read_doc(did)[0]["source"] == "pdf:arxiv"
    assert not (tmp_path / "download.pdf").exists() and ft.reader.pdf_slots._value == arxiv.READING_AT_ONCE


def test_a_page_that_is_not_a_conversion_counts_as_no_html(store, tmp_path):
    abstract_doc(store)
    ft, asked = full_text(store, {"html": html(PAPER), "pdf": pdf(tmp_path)})
    assert run(ft) and len(asked) == 2


def test_a_timeout_leaves_the_paper_for_the_next_run_without_reading_it_twice(store, monkeypatch, tmp_path):
    did = abstract_doc(store)
    reads = []
    monkeypatch.setattr(extract, "html_to_doc", lambda body, url: reads.append(url) or {"text": "", "error": "extract_timeout"})
    ft, asked = full_text(store, {"html": html(), "pdf": pdf(tmp_path)})
    assert run(ft) and len(reads) == 1 and len(asked) == 1  # no second read, and it doesn't settle for the PDF
    assert "## Full text" not in store.read_doc(did)[1] and len(store.done_without("arxiv", arxiv.FULLTEXT)) == 1

    assert run(ft) and len(asked) == 3  # a second busy run moves on, so a page that can't be read still gets text
    assert store.read_doc(did)[0]["source"] == "pdf:arxiv" and store.done_without("arxiv", arxiv.FULLTEXT) == []


def test_a_crashed_read_is_tried_again_straight_away(store, monkeypatch):
    did = abstract_doc(store)
    answers = iter([{"text": "", "error": "extract_crashed"}, {"text": PAPER}])
    monkeypatch.setattr(extract, "html_to_doc", lambda body, url: next(answers))
    ft, asked = full_text(store, {"html": html()})
    assert run(ft) and len(asked) == 1 and store.read_doc(did)[0]["source"] == "html:arxiv"


def test_html_replaces_pdf_text_that_is_longer(store, tmp_path):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(), "pdf": pdf(tmp_path, PAPER + "Page header. " * 60)})
    assert run(ft) and store.read_doc(did)[0]["source"] == "pdf:arxiv"
    store.db.execute("UPDATE urls SET tried = '' WHERE doc_key = ?", (KEY,))
    ft, _ = full_text(store, {"html": html()})
    assert run(ft)
    meta, body = store.read_doc(did)
    assert meta["source"] == "html:arxiv" and "Page header" not in body


def test_a_partial_conversion_does_not_replace_a_pdf(store):
    body = "abs" + arxiv.FULLTEXT_SEP + "x" * 1000
    assert arxiv.with_full_text(body, "y" * 400, html_over_pdf=True) is None
    assert arxiv.with_full_text(body, "y" * 600, html_over_pdf=True).endswith("y" * 600)


def test_papers_that_used_their_pdf_are_requeued_once_their_html_is_unknown(store, tmp_path):
    abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(), "pdf": pdf(tmp_path)})
    assert run(ft) and store.done_without("arxiv", arxiv.FULLTEXT) == []
    assert arxiv.requeue_pdf_papers(store) == 0  # it has no web version: looking again wouldn't help

    store.db.execute("UPDATE urls SET tried = ',fulltext' WHERE doc_key = ?", (KEY,))  # fell back before we kept track
    assert arxiv.requeue_pdf_papers(store) == 1 and len(store.done_without("arxiv", arxiv.FULLTEXT)) == 1


def test_requests_stay_one_at_a_time_and_spaced_out_while_papers_overlap(store, monkeypatch):
    monkeypatch.setattr(arxiv, "FULLTEXT_DELAY", 0.03)
    ft = arxiv.FullText(store, Pool(), False)
    starts, inside, most = [], 0, 0

    class Response:
        status, url, headers = 404, "https://arxiv.org/x", {}
        content = None

        async def __aenter__(self):
            nonlocal inside, most
            inside += 1
            most = max(most, inside)
            starts.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.005)
            return self

        async def __aexit__(self, *_):
            nonlocal inside
            inside -= 1

    class Session:
        def get(self, url):
            return Response()

    async def read(status, url, headers, chunks):
        return Fetched(status=404, error="http_404")

    monkeypatch.setattr(ft.reader, "read", read)

    class Content:
        def iter_chunked(self, _):
            return None

    Response.content = Content()

    async def go():
        await asyncio.gather(*(ft.get(Session(), "https://arxiv.org/x") for _ in range(3)))

    asyncio.run(go())
    assert most == 1 and all(b - a >= 0.03 for a, b in zip(starts, starts[1:]))


def test_a_slow_read_does_not_hold_up_the_next_paper(store, monkeypatch):
    keys = [f"arxiv:2402.0263{i}" for i in range(3)]
    store.add([route(f"https://arxiv.org/abs/{k.split(':')[1]}") for k in keys])
    for k in keys:
        store.record(k, "ok", chars=10)
    store.commit()
    started, release = [], asyncio.Event()

    async def one(self, session, key):
        started.append(key)
        if len(started) == 3:
            release.set()
        await asyncio.wait_for(release.wait(), 2)  # only passes if all three are in progress together
        return True

    monkeypatch.setattr(arxiv.FullText, "one", one)
    asyncio.run(arxiv.add_full_text(store, Pool(), False))
    assert sorted(started) == sorted(keys)


def test_full_text_stops_when_arxiv_cannot_be_reached(store, monkeypatch):
    keys = [f"arxiv:2402.{10000 + i}" for i in range(40)]
    store.add([route(f"https://arxiv.org/abs/{k.split(':')[1]}") for k in keys])
    for k in keys:
        store.record(k, "ok", chars=10)
    store.commit()
    asked = []

    async def one(self, session, key):
        asked.append(key)
        return False

    monkeypatch.setattr(arxiv.FullText, "one", one)
    asyncio.run(arxiv.add_full_text(store, Pool(), False))
    assert len(asked) == arxiv.MAX_UNREACHABLE


def test_nothing_found_keeps_the_abstract_and_is_not_asked_again(store):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(), "pdf": missing()})
    before = store.read_doc(did)[1]
    assert run(ft)
    assert store.read_doc(did)[1] == before and store.done_without("arxiv", arxiv.FULLTEXT) == []


@pytest.mark.parametrize("status", [429, 503])
def test_arxiv_unreachable_is_tried_next_run(store, status):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(status)})
    assert not run(ft)
    assert "## Full text" not in store.read_doc(did)[1]
    assert len(store.done_without("arxiv", arxiv.FULLTEXT)) == 1


@pytest.mark.parametrize("mode,kept", [(True, True), (False, False)])
def test_arxiv_originals(store, tmp_path, mode, kept):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": html()}, mode)
    assert run(ft)
    meta = store.read_doc(did)[0]
    assert ("raw" in meta) == kept
    if kept:
        assert meta["raw"] == f"raw/{did[:2]}/{did}.html.zst" and (store.out / meta["raw"]).is_file()


def test_kept_arxiv_pdf_is_moved_and_frees_its_slot(store, tmp_path):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(), "pdf": pdf(tmp_path)}, True)
    assert run(ft)
    assert store.read_doc(did)[0]["raw"] == f"raw/{did[:2]}/{did}.pdf" and ft.reader.pdf_slots._value == arxiv.READING_AT_ONCE


def test_shorter_full_text_does_not_replace_a_longer_one():
    body = "abs" + arxiv.FULLTEXT_SEP + "a long full text"
    assert arxiv.with_full_text(body, "short") is None
    assert arxiv.with_full_text(body, "a much longer full text").endswith("a much longer full text")


class FakeScry:
    """Answers the two statements fill_from_scry sends."""
    spent_nanodollars = 0
    catalog: dict = {}
    papers: dict = {}
    fail_on = None
    cost = 0
    statements: list = []

    def __init__(self, session, key):
        pass

    async def query(self, sql, max_seconds=60):
        FakeScry.statements.append(sql)
        FakeScry.spent_nanodollars += FakeScry.cost
        table = "catalog" if "academic.catalog" in sql else "papers"
        if FakeScry.fail_on == table:
            raise arxiv.ScryError("boom")
        if table == "catalog":
            return [{"arxiv_id": i, "paper_key": k, "fulltext_tokens": t} for i, (k, t) in FakeScry.catalog.items() if f"'{i}'" in sql]
        return [{"paper_key": k, "text": t} for k, t in FakeScry.papers.items() if f"'{k}'" in sql]


@pytest.fixture
def scry(monkeypatch):
    monkeypatch.setattr(arxiv, "ScryClient", FakeScry)
    monkeypatch.setattr(arxiv, "load_key", lambda: "a key")
    for name, value in (("spent_nanodollars", 0), ("catalog", {}), ("papers", {}), ("fail_on", None), ("cost", 0), ("statements", [])):
        monkeypatch.setattr(FakeScry, name, value)
    return FakeScry


def fill(store, todo, dollars=5.0):
    return asyncio.run(arxiv.fill_from_scry(store, todo, dollars))


def test_a_paper_scry_has_is_finished_without_asking_arxiv(store, scry):
    did = abstract_doc(store)
    scry.catalog, scry.papers = {"2402.02637": ("key1", 500)}, {"key1": PAPER}
    assert fill(store, [KEY]) == []
    meta, body = store.read_doc(did)
    assert meta["source"] == arxiv.SCRY_SOURCE and "The abstract." in body and "Body of the paper." in body
    assert store.done_without("arxiv", arxiv.FULLTEXT) == []


def test_a_paper_scry_lacks_still_goes_to_arxiv(store, scry):
    did = abstract_doc(store)
    assert fill(store, [KEY]) == [KEY] and "## Full text" not in store.read_doc(did)[1]


def test_a_paper_listed_but_with_no_usable_text_goes_to_arxiv(store, scry):
    abstract_doc(store)
    scry.catalog, scry.papers = {"2402.02637": ("key1", 500)}, {"key1": "too short"}
    assert fill(store, [KEY]) == [KEY]


def test_scry_does_not_replace_text_a_paper_already_has(store, scry, tmp_path):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(), "pdf": pdf(tmp_path)})
    assert run(ft)
    before = store.read_doc(did)[1]
    scry.catalog, scry.papers = {"2402.02637": ("key1", 500)}, {"key1": "A different text. " * 200}
    assert fill(store, [KEY]) == [KEY] and store.read_doc(did)[1] == before and not scry.statements


@pytest.mark.parametrize("broken", ["catalog", "papers"])
def test_when_scry_fails_papers_wait_rather_than_going_to_arxiv(store, scry, broken):
    did = abstract_doc(store)
    scry.catalog, scry.papers, scry.fail_on = {"2402.02637": ("key1", 500)}, {"key1": PAPER}, broken
    assert fill(store, [KEY]) == []
    assert "## Full text" not in store.read_doc(did)[1] and len(store.done_without("arxiv", arxiv.FULLTEXT)) == 1


def test_scry_stops_at_the_spending_limit(store, scry):
    ids = [f"2402.{10000 + i}" for i in range(250)]
    store.add([route(f"https://arxiv.org/abs/{i}") for i in ids])
    for i in ids:
        chars = store.write_doc(f"arxiv:{i}", {"title": "T", "url": "u"}, "An abstract.")
        store.record(f"arxiv:{i}", "ok", chars=chars)
    store.commit()
    scry.catalog = {i: (f"k{i}", 10) for i in ids}
    scry.papers = {f"k{i}": PAPER for i in ids}
    scry.cost = 400_000_000  # $0.40 a statement
    assert fill(store, [f"arxiv:{i}" for i in ids], 0.5) == []  # the rest wait; arXiv isn't asked for them
    done = 250 - len(store.done_without("arxiv", arxiv.FULLTEXT))
    assert done == 100  # one catalog statement, then one batch of text, is past the limit


def test_without_a_key_everything_goes_to_arxiv(store, monkeypatch):
    abstract_doc(store)
    monkeypatch.setattr(arxiv, "load_key", lambda: None)
    assert fill(store, [KEY]) == [KEY]


def test_big_papers_are_kept_out_of_scry_and_batches_stay_small():
    found = {"a": ("ka", 400_000), "b": ("kb", 400_000), "c": ("kc", 900_000), "d": ("kd", 100)}
    assert arxiv._text_batches(found) == [["a"], ["b", "d"]]
    assert all(len(b) <= arxiv.SCRY_TEXT_BATCH for b in arxiv._text_batches({str(i): (str(i), 1) for i in range(250)}))


def test_quotes_in_ids_cannot_break_the_statement():
    assert arxiv._sql_list(["a'b", "c\\d"]) == "'a\\'b','c\\\\d'"


def test_the_full_text_step_uses_scry_only_when_asked(store, scry, monkeypatch):
    abstract_doc(store)
    scry.catalog, scry.papers = {"2402.02637": ("key1", 500)}, {"key1": PAPER}
    asked = []

    async def one(self, session, key):
        asked.append(key)
        return True

    monkeypatch.setattr(arxiv.FullText, "one", one)
    asyncio.run(arxiv.add_full_text(store, Pool(), False))
    assert asked == [KEY] and not scry.statements
    asked.clear()
    asyncio.run(arxiv.add_full_text(store, Pool(), False, scry_dollars=5.0))
    assert not asked and scry.statements


def test_tidy_html_text():
    text = "# Title\n\nIntro\n\n- \u2022 \nFirst\n- \\[12\\]\nA reference"
    assert arxiv.tidy_html_text(text) == "Intro\n\n- First\n- \\[12\\] A reference"


class Client:
    pass


def generic_pass(store, mode):
    return GenericPass(store, Client(), None, None, 1, mode)


@pytest.mark.parametrize("mode,kind,kept", [(True, "pdf", True), (True, "html", False), (True, "text", False),
                                            (False, "pdf", False)])
def test_which_web_originals_are_kept(store, mode, kind, kept):
    assert generic_pass(store, mode)._keeps_raw(Fetched(kind=kind)) == kept


def test_raw_line_only_when_the_file_exists(store):
    url = "https://example.com/a"
    store.add([route(url)])
    store.write_doc(url, {"url": url}, "text")
    assert "raw" not in store.read_doc(doc_id(url))[0]
    raw = store.save_raw(url, "html.zst", data=b"<html>")
    store.record(url, "ok", raw_path=raw)
    store.write_doc(url, {"url": url}, "rewritten without saying so")
    assert store.read_doc(doc_id(url))[0]["raw"] == raw  # any later rewrite keeps the link
    (store.out / raw).unlink()
    store.write_doc(url, {"url": url}, "and now its original is gone")
    assert "raw" not in store.read_doc(doc_id(url))[0]


def test_new_format_replaces_the_old_copy(store, tmp_path):
    url = "https://example.com/paper"
    old = store.save_raw(url, "html.zst", data=b"<html>")
    download = tmp_path / "d.pdf"
    download.write_bytes(b"%PDF")
    new = store.save_raw(url, "pdf", move_from=str(download))
    assert (store.out / new).is_file() and not (store.out / old).exists()


def test_local_files_are_not_copied():
    import inspect

    from osiris.localfile import run_localfile
    assert "keep_raw" not in inspect.signature(run_localfile).parameters


def test_old_ui_config_gets_full_text_on(tmp_path):
    import json

    from osiris import ui

    ui.config_path(tmp_path).write_text(json.dumps({"options": {"keep_raw": True}}), encoding="utf-8")
    options = ui.load_config(tmp_path)["options"]
    assert options["arxiv_fulltext"] is True and options["keep_raw_pdfs"] is True and "keep_raw" not in options
