"""One list of local folders: web pages in them are imported as saved pages, and every other file is listed as a local file."""

import asyncio
import io
import json
from pathlib import Path

from osiris import extract, singlefile, ui
from osiris.route import doc_id, route
from osiris.store import Store

SAVED = """<html><!--
 Page saved with SingleFile
 url: https://example.com/paywalled
 saved date: Wed Jul 08 2026 13:35:25 GMT+0300 (Turkey Standard Time)
--><head><title>A paywalled article</title></head><body><article><h1>A paywalled article</h1>
<p>{}</p></article></body></html>"""
WORDS = "Every word of the article that the site would only show to a subscriber. " * 12


def test_saved_page_folders_join_the_local_folders_and_a_removed_one_stays_removed(tmp_path: Path):
    ui.config_path(tmp_path).write_text(json.dumps({"local": ["/books", "/pages"], "saved": ["/pages", "/saved"]}))
    config = ui.load_config(tmp_path)
    assert config["local"] == ["/books", "/pages", "/saved"] and "saved" not in config

    async def remove(client):
        assert (await client.post("/api/config", json={"local": ["/books", "/pages"]})).status == 200

    async def main():
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(ui.make_app(tmp_path))) as client:
            await remove(client)

    asyncio.run(main())
    assert ui.load_config(tmp_path)["local"] == ["/books", "/pages"]  # as a restarted app reads it
    assert "saved" not in json.loads(ui.config_path(tmp_path).read_text())


def test_web_pages_are_left_out_of_the_list_of_local_files_and_counted(tmp_path: Path):
    for name in ("a.pdf", "b.djvu", "page.html", "other.HTM", "c.xhtml", ".hidden/d.html", "sub/e.html", "notes.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    out = io.StringIO()
    assert ui._write_local_urls([str(tmp_path)], out) == (4, 3)  # a.pdf b.djvu c.xhtml notes.txt; page, other, sub/e
    assert sorted(line.rsplit("/", 1)[1] for line in out.getvalue().split()) == ["a.pdf", "b.djvu", "c.xhtml", "notes.txt"]
    assert ui._scan_folder(tmp_path, "local")["summary"].startswith("7 readable (3 html, ")  # .htm counts as html


class InProcessPool:
    async def run(self, fn, *args):
        return extract._cleaned(fn, *args)


def test_the_import_skips_hidden_folders_and_pages_already_archived_as_local_files(tmp_path: Path):
    folder = tmp_path / "files"
    for name in ("saved.html", ".cache/junk.html", "old.html"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(SAVED.replace("paywalled", name.split("/")[-1].split(".")[0]).format(WORDS))
    store = Store(tmp_path / "data")
    old = route((folder / "old.html").resolve().as_uri())  # read from a folder of local files by an earlier version
    web = route("https://example.com/saved")
    store.add([old, web])
    store.record(old.doc_key, "ok", chars=900)
    store.record(web.doc_key, "failed", "http_403")
    store.commit()
    assert singlefile.html_files([folder]) == [folder / "old.html", folder / "saved.html"]
    asyncio.run(singlefile.run_import(store, [folder], InProcessPool(), 1))
    rows = dict(store.db.execute("SELECT url, status FROM urls"))
    meta, body = store.read_doc(doc_id(web.doc_key))  # the saved copy fills in the page the web refused
    store.close()
    assert rows == {old.url: "ok", web.url: "ok"} and meta["source"] == "singlefile" and "subscriber" in body
