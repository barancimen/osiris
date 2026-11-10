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
        # Google answers a headless browser with its block page about four times in five
        browser = browser and not (urlsplit(doc_key).hostname or "").startswith("scholar.google.")
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


CRAWL = "crawl"  # in `tried`: Scry's crawl has been asked for this page
CRAWL_BATCH = 50  # pages per query


async def crawl_copies(client: ScryClient, urls: list[str]) -> dict[str, tuple[str | None, str]]:
    """{url: (title, text)} for the pages Scry's crawler has read, from its newest reading of any spelling."""
    found: dict[str, tuple[str | None, str]] = {}
    for i in range(0, len(urls), CRAWL_BATCH):
        spelled, hosts = {}, set()
        for url in urls[i : i + CRAWL_BATCH]:
            these_hosts, spellings = _spellings(url)
            hosts.update(these_hosts)
            for spelling in spellings:
                spelled.setdefault(spelling, url)
        rows = await client.query(
            f"SELECT url, title, text FROM crawl.current WHERE host IN ({sql_list(sorted(hosts))})"
            f" AND url IN ({sql_list(spelled)}) ORDER BY observed_at DESC LIMIT 1 BY url LIMIT {len(spelled)}",
            max_seconds=120)
        for row in rows:  # newest first, so a page's newest reading wins over its other spellings
            url, text = spelled.get(row["url"]), clean.clean_text(row["text"] or "", pdf=False).strip()
            if url and text and url not in found:
                found[url] = (row["title"] or None, text)
    return found


def _save_crawl(store: Store, url: str, title: str | None, text: str) -> None:
    chars = store.write_doc(url, {"url": url, "source": CRAWL_SOURCE, "title": title}, text)
    ok = chars >= extract.MIN_CHARS
    store.record(url, "ok" if ok else "thin", None if ok else "short", chars=chars)


async def run_crawl_pass(store: Store, max_dollars: float) -> None:
    """The last fallback: Scry's copy of web pages still failed or thin, kept only if it's longer."""
    from .generic import NOT_FOR_WAYBACK

    candidates = [(url, prev) for url, prev in store.retry_candidates("generic", CRAWL, lambda r: not r.startswith(NOT_FOR_WAYBACK))
                  if url.startswith(("http://", "https://"))]
    if not candidates:
        return
    key = load_key()
    if not key:
        log.warning("generic: no Scry key, so Scry's copies of %d failed/thin pages can't be looked up", len(candidates))
        return
    log.info("generic: crawl pass: %d failed/thin pages from Scry's crawl", len(candidates))
    stop_at, saved = ScryClient.spent_nanodollars + int(max_dollars * 1e9), 0
    async with aiohttp.ClientSession() as session:
        client = ScryClient(session, key)
        for i in range(0, len(candidates), CRAWL_BATCH):
            batch = candidates[i : i + CRAWL_BATCH]
            if ScryClient.spent_nanodollars >= stop_at:
                log.warning("generic: reached the $%.2f limit for Scry's page copies; %d pages wait for the next run",
                            max_dollars, len(candidates) - i)
                break
            try:
                found = await crawl_copies(client, [url for url, _ in batch])
            except ScryError as e:
                log.warning("generic: Scry's crawl failed (%s); %d pages wait for the next run", e, len(candidates) - i)
                break
            for url, prev in batch:
                if url in found and len(found[url][1]) > max(prev, 0):
                    _save_crawl(store, url, *found[url])
                    saved += 1
                store.mark_tried(url, CRAWL)
            store.commit()
    log.info("generic: crawl pass: %d of %d pages saved from Scry's crawl", saved, len(candidates))


async def from_crawl(store: Store, keys: list[str]) -> None:
    """Each page as Scry's crawler last read it, for a fix that asks for it, whatever its length."""
    key = load_key()
    if not key:
        log.warning("fixes: there's no Scry key, so Scry's copies of %d pages can't be looked up", len(keys))
        return
    async with aiohttp.ClientSession() as session:
        try:
            found = await crawl_copies(ScryClient(session, key), keys)
        except ScryError as e:
            log.warning("fixes: Scry's crawl failed: %s", e)
            return
    for url, (title, text) in found.items():
        _save_crawl(store, url, title, text)
    store.commit()
    log.info("fixes: Scry had copies of %d of %d pages", len(found), len(keys))
