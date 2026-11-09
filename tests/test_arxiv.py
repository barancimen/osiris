"""arXiv full text: better text is never overwritten, requests stay spaced out, and Scry stays opt-in and capped."""

import asyncio
from pathlib import Path

import pytest

from osiris import arxiv, extract
from osiris.generic import Fetched
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


def test_a_partial_conversion_does_not_replace_a_pdf(store):
    body = "abs" + arxiv.FULLTEXT_SEP + "x" * 1000
    assert arxiv.with_full_text(body, "y" * 400, html_over_pdf=True) is None
    assert arxiv.with_full_text(body, "y" * 600, html_over_pdf=True).endswith("y" * 600)


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


def test_scry_does_not_replace_text_a_paper_already_has(store, scry, tmp_path):
    did = abstract_doc(store)
    ft, _ = full_text(store, {"html": missing(), "pdf": pdf(tmp_path)})
    assert run(ft)
    before = store.read_doc(did)[1]
    scry.catalog, scry.papers = {"2402.02637": ("key1", 500)}, {"key1": "A different text. " * 200}
    assert fill(store, [KEY]) == [KEY] and store.read_doc(did)[1] == before and not scry.statements


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
