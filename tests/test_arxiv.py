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
    assert not (tmp_path / "download.pdf").exists() and ft.reader.pdf_slots._value == 1


def test_a_page_that_is_not_a_conversion_counts_as_no_html(store, tmp_path):
    abstract_doc(store)
    ft, asked = full_text(store, {"html": html(PAPER), "pdf": pdf(tmp_path)})
    assert run(ft) and len(asked) == 2


def test_a_timeout_here_does_not_send_the_paper_to_its_pdf(store, monkeypatch):
    did = abstract_doc(store)
    answers = iter([{"text": "", "error": "extract_timeout"}, {"text": PAPER}])
    monkeypatch.setattr(extract, "html_to_doc", lambda body, url: next(answers))
    ft, asked = full_text(store, {"html": html()})
    assert run(ft) and len(asked) == 1 and store.read_doc(did)[0]["source"] == "html:arxiv"


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
    assert store.read_doc(did)[0]["raw"] == f"raw/{did[:2]}/{did}.pdf" and ft.reader.pdf_slots._value == 1


def test_shorter_full_text_does_not_replace_a_longer_one():
    body = "abs" + arxiv.FULLTEXT_SEP + "a long full text"
    assert arxiv.with_full_text(body, "short") is None
    assert arxiv.with_full_text(body, "a much longer full text").endswith("a much longer full text")


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
