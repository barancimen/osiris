"""arXiv lane: titles/authors/abstracts via the export API, then each paper's full text.

arxiv.org's robots.txt sets Crawl-delay: 15, while the API ToU allows one request every
3 seconds on a single connection with many IDs per request. The full text comes from arxiv.org
at the slower pace: its HTML version, whose maths survives as LaTeX, or else its PDF.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
import logging
import re
import xml.etree.ElementTree as ET

import aiohttp

from . import extract
from .generic import Fetched, Reader
from .route import doc_id
from .scry import ScryClient, ScryError, load_key
from .store import Store

log = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"
BATCH = 100
DELAY = 3.1
MAX_TRIES = 3
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ENTRY_ID_RE = re.compile(r"arxiv\.org/abs/(.+?)(?:v\d+)?$")
UA = "osiris/0.1 (one-off abstract lookup for saved browser tabs)"

FULLTEXT = "fulltext"  # in `tried`: this paper's full text has been looked for
FULLTEXT_DELAY = 15.0
FULLTEXT_SEP = "\n\n## Full text\n\n"
MIN_FULLTEXT_CHARS = 1000  # less than this is a failed conversion, not a paper
MAX_UNREACHABLE = 5  # papers in a row: arXiv is down or refusing us, so stop until the next run
BUSY = {"extract_timeout", "extract_crashed"}  # this machine's trouble, which says nothing about the paper
BUSY_ONCE = "html_busy"  # in `tried`: a run was too busy to read this paper's HTML, so the next one tries before its PDF
HTML_CHECKED = "html_checked"  # in `tried`: we know what this paper's HTML gives, so there's no need to look again
READING_AT_ONCE = 8  # papers between download and saved; requests still go out one at a time
MIN_HTML_SHARE = 0.5  # HTML replaces PDF text unless it's less than this share of it, which means a partial conversion
SCRY_SOURCE = "fulltext:scry"
SCRY_LOOKUP_BATCH = 500  # paper ids looked up in Scry's catalog at once
SCRY_TEXT_BATCH = 100
SCRY_BATCH_CHARS = 3_000_000  # text in one response, so each query stays well inside what ScryClient lets it spend
TRANSIENT = {"connect", "http_429", "http_500", "http_502", "http_503", "http_504"}


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


def tidy_html_text(text: str) -> str:
    """What's left of arXiv's HTML layout: its own title line and list markers on their own lines."""
    text = re.sub(r"\A#[^\n]*\n+", "", text.strip())
    text = re.sub(r"^- •[ \t]*\n+", "- ", text, flags=re.M)
    return re.sub(r"^- (\\\[\d+\\\])[ \t]*\n+", r"- \1 ", text, flags=re.M)


def with_full_text(body: str, text: str, html_over_pdf: bool = False) -> str | None:
    """The document with this full text after its abstract, or None if what it has is as long.
    PDF text runs long on page headers and the like, so arXiv's HTML can replace it when shorter."""
    head, _, old = body.partition(FULLTEXT_SEP)
    better = len(text) > len(old) or (html_over_pdf and len(text) >= len(old) * MIN_HTML_SHARE)
    return f"{head}{FULLTEXT_SEP}{text}" if better else None


class FullText:
    def __init__(self, store: Store, pool: extract.WorkerPool, keep_raw: bool):
        self.store = store
        self.pool = pool
        self.keep_raw = keep_raw
        self.reader = Reader(store.tmp_dir, asyncio.Semaphore(READING_AT_ONCE))
        self.next_call = 0.0
        self.gate = asyncio.Lock()

    async def get(self, session: aiohttp.ClientSession, url: str) -> Fetched:
        """One request at a time, FULLTEXT_DELAY apart, however many papers are being read."""
        loop = asyncio.get_running_loop()
        async with self.gate:
            for attempt in range(1, MAX_TRIES + 1):
                await asyncio.sleep(max(0.0, self.next_call - loop.time()))
                try:
                    async with session.get(url) as resp:
                        f = await self.reader.read(resp.status, str(resp.url), resp.headers, resp.content.iter_chunked(65536))
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.warning("arxiv: %s: %s", url, e or type(e).__name__)
                    f = Fetched(error="connect")
                finally:
                    self.next_call = loop.time() + FULLTEXT_DELAY
                if f.error not in TRANSIENT:
                    break
                self.next_call += f.retry_after or FULLTEXT_DELAY * attempt
        return f

    async def _from_html(self, key: str, f: Fetched) -> str | None:
        """The paper's text; "" if its HTML is missing or unusable; None if it's left for the next run,
        because this machine was too busy to read it. A second run that's too busy moves on to the PDF."""
        if f.kind != "html" or b"ltx_" not in f.body:
            log.info("arxiv: no HTML version of %s (%s)", key, f.error or f.content_type)
            self.store.mark_tried(key, HTML_CHECKED)
            return ""
        res = await self.pool.run(extract.html_to_doc, f.body, f.final_url)
        if res.get("error") == "extract_crashed":
            res = await self.pool.run(extract.html_to_doc, f.body, f.final_url)
        busy = res.get("error") in BUSY
        if busy and not self.store.has_tried(key, BUSY_ONCE):
            log.warning("arxiv: too busy to read the HTML of %s (%s); the next run tries it again", key, res.get("error"))
            self.store.mark_tried(key, BUSY_ONCE)
            return None
        if not busy:
            self.store.mark_tried(key, HTML_CHECKED)
        text = tidy_html_text(res.get("text") or "")
        if len(text) < MIN_FULLTEXT_CHARS:
            log.warning("arxiv: couldn't read the HTML of %s (%s, %d characters)", key, res.get("error"), len(text))
            return ""
        return text

    async def _from_pdf(self, f: Fetched) -> str:
        res = await self.pool.run(extract.pdf_to_doc, f.pdf_path)
        return (res.get("text") or "").strip()

    def _write(self, key: str, text: str, source: str, f: Fetched) -> None:
        found = self.store.read_doc(doc_id(key))
        if found is None:
            log.warning("arxiv: no document on disk for %s", key)
            return
        meta, body = found
        body = with_full_text(body, text, html_over_pdf=source == "html:arxiv" and meta.get("source") == "pdf:arxiv")
        if body is None:
            return
        raw_path = None
        if self.keep_raw:
            raw_path = (self.store.save_raw(key, "pdf", move_from=f.pdf_path) if f.kind == "pdf"
                        else self.store.save_raw(key, "html.zst", data=f.body))
            if f.kind == "pdf":
                f.pdf_path = None
                self.reader.pdf_slots.release()
        meta = {k: v for k, v in meta.items() if k not in ("id", "fetched_at")}
        chars = self.store.write_doc(key, {**meta, "source": source, "raw": raw_path}, body)
        self.store.record(key, "ok", chars=chars, http_status=f.status, final_url=f.final_url,
                          content_type=f.content_type, **({"raw_path": raw_path} if raw_path else {}))

    async def one(self, session: aiohttp.ClientSession, key: str) -> bool:
        """Add this paper's full text. False if arXiv couldn't be reached, so it's tried again next run."""
        arxiv_id = key.split(":", 1)[1]
        f = await self.get(session, f"https://arxiv.org/html/{arxiv_id}")
        if f.error in TRANSIENT:
            return False
        text = await self._from_html(key, f)
        if text is None:
            self.store.commit()
            return True
        if text:
            self._write(key, text, "html:arxiv", f)
        else:
            f = await self.get(session, f"https://arxiv.org/pdf/{arxiv_id}")
            if f.error in TRANSIENT:
                return False
            try:
                if f.kind == "pdf" and (text := await self._from_pdf(f)):
                    self._write(key, text, "pdf:arxiv", f)
            finally:
                self.reader.release_pdf(f)
        self.store.mark_tried(key, FULLTEXT)
        self.store.commit()
        return True


async def add_full_text(store: Store, pool: extract.WorkerPool, keep_raw: bool, scry_dollars: float | None = None) -> None:
    todo = [key for key, _ in store.done_without("arxiv", FULLTEXT)]
    if todo and scry_dollars is not None:
        todo = await fill_from_scry(store, todo, scry_dollars)
    if not todo:
        return
    log.info("arxiv: full text for %d papers, one every %.0f seconds (about %.1f hours); safe to stop, the next run "
             "carries on", len(todo), FULLTEXT_DELAY, len(todo) * FULLTEXT_DELAY / 3600)
    full_text = FullText(store, pool, keep_raw)
    queue = deque(todo)
    unreachable = 0

    async def work(session: aiohttp.ClientSession) -> None:
        nonlocal unreachable
        while queue and unreachable < MAX_UNREACHABLE:
            key = queue.popleft()
            try:
                reached = await full_text.one(session, key)
            except Exception:
                log.exception("arxiv: %s failed", key)
                continue
            unreachable = 0 if reached else unreachable + 1
            if unreachable == MAX_UNREACHABLE:
                log.warning("arxiv: couldn't reach arXiv for %d papers in a row, stopping until the next run", unreachable)

    connector = aiohttp.TCPConnector(limit=1)
    timeout = aiohttp.ClientTimeout(total=300, sock_read=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers={"User-Agent": UA}) as session:
        await asyncio.gather(*(work(session) for _ in range(READING_AT_ONCE)))


def _sql_list(values: Iterable[str]) -> str:
    return ",".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


def _text_batches(found: dict[str, tuple[str, int]]) -> list[list[str]]:
    """Ids grouped so that no response is too big. A paper too big for a batch of its own is left out."""
    batches: list[list[str]] = []
    chars = 0
    for arxiv_id, (_, tokens) in found.items():
        size = 4 * tokens  # about four characters to a token
        if size > SCRY_BATCH_CHARS:
            continue
        if not batches or len(batches[-1]) >= SCRY_TEXT_BATCH or chars + size > SCRY_BATCH_CHARS:
            batches.append([])
            chars = 0
        batches[-1].append(arxiv_id)
        chars += size
    return batches


def _save_scry_text(store: Store, key: str, text: str) -> None:
    meta, body = store.read_doc(doc_id(key))
    body = with_full_text(body, text)
    if body is not None:
        meta = {k: v for k, v in meta.items() if k not in ("id", "fetched_at")}
        store.record(key, "ok", chars=store.write_doc(key, {**meta, "source": SCRY_SOURCE}, body))
    store.mark_tried(key, FULLTEXT)


async def fill_from_scry(store: Store, todo: list[str], max_dollars: float) -> list[str]:
    """Full text from Scry for papers that have only their abstract, without asking arXiv for them.
    Returns the papers arXiv should still be asked for: those Scry doesn't have, and those that already
    have text, because Scry's copy drops formulas and shouldn't replace better text. If Scry fails or the
    spending limit is reached, papers it wasn't asked about wait for the next run instead of going to arXiv."""
    key = load_key()
    if not key:
        log.warning("arxiv: no Scry key, so every paper comes from arXiv")
        return todo
    for_arxiv, ids = [], {}
    for doc_key in todo:
        found = store.read_doc(doc_id(doc_key))
        if found is not None and FULLTEXT_SEP not in found[1]:
            ids[doc_key.split(":", 1)[1]] = doc_key
        else:
            for_arxiv.append(doc_key)
    if not ids:
        return for_arxiv
    stop_at = ScryClient.spent_nanodollars + int(max_dollars * 1e9)
    saved, waiting = 0, 0
    wanted = list(ids)
    versions: dict[str, tuple[str, int]] = {}
    async with aiohttp.ClientSession() as session:
        client = ScryClient(session, key)
        try:
            for i in range(0, len(wanted), SCRY_LOOKUP_BATCH):
                batch = wanted[i : i + SCRY_LOOKUP_BATCH]
                for row in await client.query(
                    "SELECT arxiv_id, paper_key, fulltext_tokens FROM academic.catalog"
                    f" WHERE arxiv_id IN ({_sql_list(batch)}) AND has_fulltext = 1 LIMIT {2 * len(batch)}", max_seconds=60):
                    versions[row["arxiv_id"]] = (row["paper_key"], row["fulltext_tokens"])
        except ScryError as e:
            log.warning("arxiv: Scry's catalog failed (%s); those papers wait for the next run", e)
            return for_arxiv
        batches = _text_batches(versions)
        fetchable = {arxiv_id for batch in batches for arxiv_id in batch}
        for_arxiv += [doc_key for arxiv_id, doc_key in ids.items() if arxiv_id not in fetchable]
        for n, batch in enumerate(batches):
            if ScryClient.spent_nanodollars >= stop_at:
                waiting = sum(len(b) for b in batches[n:])
                log.warning("arxiv: reached the $%.2f limit for Scry; %d papers wait for the next run", max_dollars, waiting)
                break
            by_paper = {versions[arxiv_id][0]: arxiv_id for arxiv_id in batch}
            try:
                rows = await client.query(
                    f"SELECT paper_key, text FROM academic.papers WHERE paper_key IN ({_sql_list(by_paper)})"
                    " AND quality_label = 'good' ORDER BY paper_key, bucket = 'arxiv' DESC"
                    f" LIMIT 1 BY paper_key LIMIT {2 * len(batch)}", max_seconds=120)
            except ScryError as e:
                waiting = sum(len(b) for b in batches[n:])
                log.warning("arxiv: Scry failed (%s); %d papers wait for the next run", e, waiting)
                break
            got = {r["paper_key"]: r["text"].strip() for r in rows}
            for paper_key, arxiv_id in by_paper.items():
                if len(got.get(paper_key, "")) >= MIN_FULLTEXT_CHARS:
                    _save_scry_text(store, ids[arxiv_id], got[paper_key])
                    saved += 1
                else:
                    for_arxiv.append(ids[arxiv_id])
            store.commit()
            log.info("arxiv: %d papers read from Scry so far", saved)
    log.info("arxiv: %d of %d papers came from Scry, %d still go to arXiv, %d wait for the next run",
             saved, len(ids), len(for_arxiv), waiting)
    return for_arxiv


def requeue_pdf_papers(store: Store) -> int:
    """Give papers whose full text came from a PDF another look at their HTML, once each: until we've
    actually read a paper's HTML, or know there isn't any, it stays eligible. The document says where its
    text came from: a paper saved earlier as a PDF link can have a PDF content type and text from Scry."""
    candidates = store.db.execute(
        "SELECT DISTINCT doc_key FROM urls WHERE lane = 'arxiv' AND content_type = 'application/pdf'"
        " AND instr(tried, ?) > 0 AND instr(tried, ?) = 0",
        (f",{FULLTEXT}", f",{HTML_CHECKED}"),
    ).fetchall()
    keys = [key for (key,) in candidates if (doc := store.read_doc(doc_id(key))) and doc[0].get("source") == "pdf:arxiv"]
    store.db.executemany("UPDATE urls SET tried = replace(tried, ?, '') WHERE doc_key = ?",
                         ((f",{FULLTEXT}", key) for key in keys))
    store.commit()
    return len(keys)


async def run_arxiv(store: Store, pool: extract.WorkerPool, keep_raw: bool = False, full_text: bool = True,
                    refetch_pdfs: bool = False, scry_dollars: float | None = None) -> None:
    await _abstracts(store)
    if full_text:
        if refetch_pdfs:
            log.info("arxiv: %d papers that used their PDF get another look at their HTML", requeue_pdf_papers(store))
        await add_full_text(store, pool, keep_raw, scry_dollars)


async def _abstracts(store: Store) -> None:
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
                old = store.read_doc(doc_id(f"arxiv:{arxiv_id}"))
                if old and FULLTEXT_SEP in old[1]:  # fetching it again doesn't cost it its full text
                    body += FULLTEXT_SEP + old[1].partition(FULLTEXT_SEP)[2]
                    meta["source"] = old[0].get("source")
                chars = store.write_doc(f"arxiv:{arxiv_id}", meta, body)
                store.record(f"arxiv:{arxiv_id}", "ok", chars=chars, final_url=meta["url"], http_status=200)
            store.commit()
