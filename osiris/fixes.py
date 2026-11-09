"""Fixes queued on the Documents tab: the ways of fetching a document again that suit it, and those
that aren't simply a normal run of its source over that one document."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp

from . import clean, extract
from .doi import doi_of
from .scry import ScryClient, ScryError, load_key, sql_list
from .store import Store

log = logging.getLogger(__name__)

METHODS = ("again", "impersonate", "render", "wayback", "crawl", "doi", "scry", "html", "twitterapi")
FETCHED_LIKE_NEW = {"again", "impersonate", "render", "wayback", "twitterapi"}  # back to waiting first
CRAWL_SOURCE = "scry:crawl"


def methods_for(doc_key: str, lane: str, scry: bool, browser: bool) -> list[str]:
    """The ways this document can be fetched again, the usual way first."""
    if lane == "singlefile":  # nowhere to go back to
        return []
    found = ["again"]
    if lane == "generic" and doc_key.startswith(("http://", "https://")):
        found += ["impersonate", *(["render"] if browser else []), "wayback", *(["crawl"] if scry else [])]
        if doi_of(doc_key):
            found.append("doi")
    elif lane == "arxiv":
        found += ["html", *(["scry"] if scry else [])]
    elif lane == "youtube" and doc_key.startswith("youtube:video:") and scry:
        found.append("scry")
    elif lane == "lesswrong" and scry:
        found.append("scry")
    elif lane == "twitter":
        found.append("twitterapi")
    return found


def _spellings(url: str) -> tuple[list[str], list[str]]:
    """The hosts and addresses Scry's crawl may have this page under: http or https, with or without www."""
    parts = urlsplit(url)
    bare = parts.netloc.lower().removeprefix("www.")
    hosts = [bare, f"www.{bare}"]
    rest = url[len(f"{parts.scheme}://{parts.netloc}"):] or "/"
    return hosts, [f"{scheme}://{host}{rest}" for scheme in ("https", "http") for host in hosts]


async def from_crawl(store: Store, keys: list[str]) -> None:
    """Each page as Scry's crawler last read it, for pages that are gone, blocked or came out wrong."""
    key = load_key()
    if not key:
        log.warning("fixes: there's no Scry key, so Scry's copies of %d pages can't be looked up", len(keys))
        return
    found = 0
    async with aiohttp.ClientSession() as session:
        client = ScryClient(session, key)
        for url in keys:
            hosts, spellings = _spellings(url)
            try:
                rows = await client.query(
                    f"SELECT title, text FROM crawl.current WHERE host IN ({sql_list(hosts)})"
                    f" AND url IN ({sql_list(spellings)}) ORDER BY observed_at DESC LIMIT 1", max_seconds=60)
            except ScryError as e:
                log.warning("fixes: Scry's copy of %s couldn't be looked up: %s", url, e)
                continue
            text = clean.clean_text(rows[0]["text"] or "", pdf=False).strip() if rows else ""
            if not text:
                log.info("fixes: Scry has no copy of %s", url)
                continue
            chars = store.write_doc(url, {"url": url, "source": CRAWL_SOURCE, "title": rows[0]["title"] or None}, text)
            ok = chars >= extract.MIN_CHARS
            store.record(url, "ok" if ok else "thin", None if ok else "short", chars=chars)
            found += 1
    store.commit()
    log.info("fixes: Scry had copies of %d of %d pages", found, len(keys))
