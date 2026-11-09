"""DOI fallback pass: title, authors and abstract for paper links the generic lane couldn't
read (dead mirrors like sci-hub, publisher bot walls, paywalls, oversized PDFs).

Looks each DOI up in Scry's OpenAlex snapshot first (a Scry key is needed, the lookup is
free), then in the OpenAlex API for whatever Scry lacks. The API shares a free daily budget
per IP without a key (~1,000 requests, 50 DOIs each); set OPENALEX_API_KEY for your own.
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import unquote

import aiohttp

from . import extract
from .scry import ScryClient, ScryError, load_key
from .store import Store

log = logging.getLogger(__name__)

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s?#&\"'<>]+)")
# publisher URL tails that follow the DOI itself
TAIL_RE = re.compile(r"(?:/(?:full|abstract|pdf|epdf|pdfdirect|html|meta|fulltext|summary|references|figures"
                     r"|suppl|supplementary|download|reader))+/?$|\.pdf$|[.,;)]+$", re.I)
# publishers whose article ids are DOI suffixes; also matched inside wrapper URLs (sci-hub.se/https://...)
ID_DOIS = (
    (re.compile(r"nature\.com/articles/([a-z0-9.\-]+?)(?:\.pdf)?/?(?:[?#]|$)", re.I), "10.1038/"),
    (re.compile(r"jstor\.org/stable/(?:pdf/)?(\d+)(?:\.pdf)?/?(?:[?#]|$)", re.I), "10.2307/"),
)
OPENALEX_URL = "https://api.openalex.org/works"
SCRY_BATCH = 200
OPENALEX_BATCH = 50


def doi_of(url: str) -> str | None:
    url = unquote(url)
    for pattern, prefix in ID_DOIS:
        if m := pattern.search(url):
            return f"{prefix}{m.group(1)}".lower()
    m = DOI_RE.search(url)
    if not m:
        return None
    doi = m.group(1)
    while (shorter := TAIL_RE.sub("", doi)) != doi:
        doi = shorter
    return doi.lower()


def _abstract(inverted: dict | str | None) -> str:
    if isinstance(inverted, str):
        inverted = json.loads(inverted) if inverted else None
    if not inverted:
        return ""
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return " ".join(word for _, word in words)


def paper_markdown(doi: str, p: dict) -> tuple[dict, str]:
    lines = [f"# {p['title']}", ""]
    if p["authors"]:
        lines.append(f"**Authors:** {p['authors']}")
    published = " · ".join(str(x) for x in (p.get("year"), p.get("venue")) if x)
    if published:
        lines.append(f"**Published:** {published}")
    lines.append(f"**DOI:** https://doi.org/{doi}")
    if p["abstract"]:
        lines += ["", "## Abstract", "", p["abstract"]]
    meta = {"final_url": f"https://doi.org/{doi}", "title": p["title"], "author": p["authors"],
            "date": str(p["year"]) if p.get("year") else None, "site": p.get("venue") or "doi.org"}
    return meta, "\n".join(lines)


async def _from_scry(client: ScryClient, dois: list[str]) -> dict[str, dict]:
    found = {}
    for i in range(0, len(dois), SCRY_BATCH):
        in_list = ",".join("'" + d.replace("\\", "\\\\").replace("'", "\\'") + "'" for d in dois[i : i + SCRY_BATCH])
        for r in await client.query(
            "SELECT doi_norm, title, publication_year AS year, abstract_inverted_index AS abstract,"
            " arrayStringConcat(arrayMap(a -> ifNull(tupleElement(tupleElement(a, 1), 1), ''), authorships), ', ')"
            f" AS authors FROM openalex.works WHERE doi_norm IN ({in_list}) LIMIT {2 * SCRY_BATCH}",
            max_seconds=120,
        ):
            if r["title"]:
                found[r["doi_norm"].lower()] = {"title": r["title"], "year": r["year"], "authors": r["authors"],
                                                "abstract": _abstract(r["abstract"]), "venue": None}
    return found


async def _from_openalex(session: aiohttp.ClientSession, dois: list[str], contact: str | None) -> dict[str, dict]:
    found = {}
    for i in range(0, len(dois), OPENALEX_BATCH):
        params = {"filter": "doi:" + "|".join(dois[i : i + OPENALEX_BATCH]), "per-page": str(OPENALEX_BATCH),
                  "select": "doi,display_name,publication_year,authorships,abstract_inverted_index,primary_location"}
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        elif contact and "@" in contact:
            params["mailto"] = contact
        try:
            async with session.get(OPENALEX_URL, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 429:
                    log.warning("doi: OpenAlex daily budget used up; set OPENALEX_API_KEY for your own")
                    break
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as e:
            log.warning("doi: OpenAlex request failed: %s", e)
            continue
        for w in data.get("results") or []:
            if not w.get("doi") or not w.get("display_name"):
                continue
            source = (w.get("primary_location") or {}).get("source") or {}
            found[w["doi"].removeprefix("https://doi.org/").lower()] = {
                "title": w["display_name"], "year": w.get("publication_year"),
                "authors": ", ".join((a.get("author") or {}).get("display_name") or "" for a in w.get("authorships") or []),
                "abstract": _abstract(w.get("abstract_inverted_index")), "venue": source.get("display_name"),
            }
    return found


async def run_doi_pass(store: Store, contact: str | None, candidates: list[tuple[str, int, str]] | None = None) -> None:
    """`candidates`, (url, length to beat, DOI), are the links to look up instead of the failed and thin ones."""
    if candidates is None:
        candidates = [(key, prev, doi) for key, prev in store.retry_candidates("generic", "doi", lambda reason: True)
                      if (doi := doi_of(key))]
    if not candidates:
        return
    dois = sorted({doi for _, _, doi in candidates})
    log.info("generic: doi pass: %d failed/thin paper links, %d DOIs", len(candidates), len(dois))

    papers: dict[str, tuple[dict, str]] = {}
    async with aiohttp.ClientSession() as session:
        key = load_key()
        if key:
            try:
                papers.update((d, (p, "scry")) for d, p in (await _from_scry(ScryClient(session, key), dois)).items())
            except ScryError as e:
                log.error("doi: Scry lookup failed: %s", e)
        missing = [d for d in dois if d not in papers]
        if missing:
            papers.update((d, (p, "openalex")) for d, p in (await _from_openalex(session, missing, contact)).items())
    log.info("generic: doi pass: %d of %d DOIs found", len(papers), len(dois))

    for url, prev, doi in candidates:
        if doi in papers:
            paper, source = papers[doi]
            meta, body = paper_markdown(doi, paper)
            if len(body) > max(prev, 0):
                chars = store.write_doc(url, {"url": url, "source": f"doi:{source}", **meta}, body)
                ok = bool(paper["abstract"]) and chars >= extract.MIN_CHARS
                store.record(url, "ok" if ok else "thin", None if ok else "no_abstract", chars=chars,
                             final_url=meta["final_url"])
        store.mark_tried(url, "doi")
    store.commit()
