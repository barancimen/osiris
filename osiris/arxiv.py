"""arXiv lane: titles/authors/abstracts via the export API.

arxiv.org's robots.txt sets Crawl-delay: 15, while the API ToU allows one request every
3 seconds on a single connection with many IDs per request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import aiohttp

from .store import Store

log = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"
BATCH = 100
DELAY = 3.1
MAX_TRIES = 3
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ENTRY_ID_RE = re.compile(r"arxiv\.org/abs/(.+?)(?:v\d+)?$")
UA = "osiris/0.1 (one-off abstract lookup for saved browser tabs)"


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_feed(xml: bytes) -> tuple[dict[str, dict], bool]:
    """Map arXiv id -> paper fields. The bool is True if the API returned an error entry."""
    root = ET.fromstring(xml)
    papers, error = {}, False
    for entry in root.findall("atom:entry", NS):
        title = _clean(entry.findtext("atom:title", namespaces=NS))
        entry_id = entry.findtext("atom:id", default="", namespaces=NS)
        m = ENTRY_ID_RE.search(entry_id)
        if title == "Error" or not m:
            error = True
            continue
        category = entry.find("arxiv:primary_category", NS)
        papers[m.group(1)] = {
            "title": title,
            "abstract": _clean(entry.findtext("atom:summary", namespaces=NS)),
            "authors": [_clean(a.findtext("atom:name", namespaces=NS)) for a in entry.findall("atom:author", NS)],
            "published": entry.findtext("atom:published", namespaces=NS),
            "category": category.get("term") if category is not None else None,
        }
    return papers, error


def paper_markdown(arxiv_id: str, p: dict) -> tuple[dict, str]:
    authors = ", ".join(p["authors"])
    lines = [f"# {p['title']}", "", f"**Authors:** {authors}"]
    if p["category"]:
        lines.append(f"**Category:** {p['category']}")
    lines += ["", "## Abstract", "", p["abstract"]]
    meta = {
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "source": "arxiv_abstract",
        "title": p["title"],
        "author": authors,
        "date": (p["published"] or "")[:10] or None,
        "site": "arxiv.org",
    }
    return meta, "\n".join(lines)


class ArxivClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.next_call = 0.0

    async def query(self, ids: list[str]) -> tuple[dict[str, dict], bool]:
        loop = asyncio.get_running_loop()
        for attempt in range(1, MAX_TRIES + 1):
            await asyncio.sleep(max(0.0, self.next_call - loop.time()))
            try:
                async with self.session.get(
                    API_URL, params={"id_list": ",".join(ids), "max_results": str(len(ids))}
                ) as resp:
                    body = await resp.read()
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                status, body = 0, b""
                log.warning("arxiv: request error (try %d): %s", attempt, e)
            finally:
                self.next_call = loop.time() + DELAY
            if status == 200:
                papers, error = parse_feed(body)
                # the API intermittently returns an empty feed; retry before believing it
                if papers or error or attempt == MAX_TRIES:
                    return papers, error
            elif status:
                log.warning("arxiv: HTTP %d (try %d)", status, attempt)
            self.next_call += DELAY * attempt
        raise RuntimeError("arXiv API unavailable")

    async def lookup(self, ids: list[str]) -> dict[str, dict]:
        """Like query(), but a malformed ID only costs itself: errored batches are bisected."""
        papers, error = await self.query(ids)
        if error and len(ids) > 1:
            mid = len(ids) // 2
            papers = {**await self.lookup(ids[:mid]), **await self.lookup(ids[mid:])}
        return papers


async def run_arxiv(store: Store) -> None:
    pending = store.pending("arxiv")
    if not pending:
        return
    ids = [key.split(":", 1)[1] for key, _ in pending]
    log.info("arxiv: %d papers in %d calls", len(ids), -(-len(ids) // BATCH))

    connector = aiohttp.TCPConnector(limit=1)  # ToU: a single connection at a time
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers={"User-Agent": UA}) as session:
        client = ArxivClient(session)
        for i in range(0, len(ids), BATCH):
            batch = ids[i : i + BATCH]
            try:
                papers = await client.lookup(batch)
            except RuntimeError as e:
                log.error("arxiv: batch %d left pending: %s", i // BATCH, e)
                continue
            for arxiv_id in batch:
                paper = papers.get(arxiv_id)
                if paper is None:
                    store.record(f"arxiv:{arxiv_id}", "failed", "not_found")
                    continue
                meta, body = paper_markdown(arxiv_id, paper)
                chars = store.write_doc(f"arxiv:{arxiv_id}", meta, body)
                store.record(f"arxiv:{arxiv_id}", "ok", chars=chars, final_url=meta["url"], http_status=200)
            store.commit()
