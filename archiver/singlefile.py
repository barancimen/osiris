"""`archiver import`: pages you saved yourself with the SingleFile browser extension, stored
as markdown like every other document.

SingleFile records where a page came from in a comment at the top of the file. That URL goes
through the same dedup as `archiver run`: a page already in the manifest is upgraded when the
saved copy has more text, and a URL imported here is never queued for fetching afterwards.
Saved pages are often logged-in or paywalled ones no fetcher can reach, so URLs that `archiver
run` skips on purpose (chat apps, GitHub, X profiles) are stored too.

The files are mostly inlined CSS and images (a 20 MB page can hold 100 KB of text), which are
cut with regexes before parsing to keep extraction fast and memory flat.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import extract
from .route import route
from .store import DONE, Store

log = logging.getLogger(__name__)

SUFFIXES = {".html", ".htm"}
MAX_BYTES = 512 * 2**20
HEAD_BYTES = 16 * 1024  # SingleFile's comment follows the <html> tag; don't match page text about it
# lanes whose API documents hold the complete post: a saved page only adds site chrome. Not
# Twitter: its API documents hold one tweet, where a saved page has the thread, X Article and replies.
COMPLETE_LANES = {"stackexchange", "reddit"}

HEADER_RE = re.compile(rb"(?:Page saved with|Archive processed by) SingleFile\s+url:\s*(\S+)"
                       rb"(?:\s+saved date:\s*([^\r\n]+))?")
CANONICAL_RE = re.compile(rb"<link\b[^>]*\brel=[\"']?canonical\b[^>]*>", re.I)
HREF_RE = re.compile(rb"\bhref=[\"']?([^\"'\s>]+)", re.I)
# JSON-LD stays: trafilatura reads author and date from it
NOISE_RE = re.compile(rb"<style\b[^>]*>.*?</style\s*>|<script\b(?![^>]*ld\+json)[^>]*>.*?</script\s*>"
                      rb"|<noscript\b[^>]*>.*?</noscript\s*>", re.I | re.S)
DATA_URI_RE = re.compile(rb"data:[a-z]+/[a-z0-9.+-]+(?:;[a-z0-9=-]+)*,[^\"'\s>)]{256,}", re.I)
TITLE_RE = re.compile(rb"<title\b[^>]*>(.*?)</title\s*>", re.I | re.S)
X_URL_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/", re.I)
X_USER_RE = re.compile(r"^(.*?)@(\w{1,15})(?:·|$)", re.S)
X_STATUS_RE = re.compile(r"/status/(\d+)")


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(2**20):
            digest.update(chunk)
    return digest.hexdigest()


def _saved_at(value: bytes | None) -> str | None:
    """'Wed Jul 08 2026 13:35:25 GMT+0300 (Türkiye Standard Time)' -> UTC ISO 8601."""
    if not value:
        return None
    try:
        when = datetime.strptime(value.decode("utf-8", "replace").split(" (")[0].strip(), "%a %b %d %Y %H:%M:%S GMT%z")
    except ValueError:
        return None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_tree(data: bytes):
    """Parsed page without what SingleFile kept but marked hidden, and with saved shadow DOM
    exposed, which extractors skip as an inert <template>. None if it won't parse."""
    import lxml.html

    try:
        tree = lxml.html.fromstring(data, parser=lxml.html.HTMLParser(encoding="utf-8", huge_tree=True))
        for el in tree.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' sf-hidden ')]"):
            if el.getparent() is not None:
                el.drop_tree()
        for el in tree.xpath("//template[@shadowrootmode or @shadowmode]"):
            el.tag = "div"
        return tree
    except Exception:
        return None


def _clean_dom(data: bytes) -> bytes:
    import lxml.html

    tree = _clean_tree(data)
    return data if tree is None else lxml.html.tostring(tree, encoding="utf-8")  # keep the page rather than lose it


def _node_text(el) -> str:
    """Text of an element, with emoji, which X renders as <img alt>."""
    if not isinstance(el.tag, str):  # comment
        return ""
    parts = [el.get("alt") or "" if el.tag == "img" else "", el.text or ""]
    for child in el:
        parts += [_node_text(child), child.tail or ""]
    return "".join(parts)


def _x_page_doc(data: bytes, url: str) -> dict | None:
    """Tweets of a saved X page, read from X's own markup. Main-content extraction is no use here:
    it treats each tweet as a block to keep or drop, and loses ~40% of them, sometimes the main one."""
    tree = _clean_tree(data)
    if tree is None:
        return None
    status = X_STATUS_RE.search(url)
    blocks, focal, first_text = [], None, None
    for node in tree.xpath('//article[@data-testid="tweet"] | //h2'):
        if node.tag == "h2":
            if _node_text(node).strip() == "Discover more" and not node.xpath("ancestor::article"):
                break  # the rest is X's recommendations, not this conversation
            continue
        when = node.xpath(".//time/@datetime")
        date = when[0][:10] if when else None
        if focal is None and status and node.xpath(f'.//a[contains(@href, "/status/{status.group(1)}")]//time'):
            focal = (node, date)
        parts, quoting = [], False
        for el in node.xpath('.//*[@data-testid="User-Name" or @data-testid="tweetText"'
                             ' or @data-testid="twitter-article-title" or @data-testid="longformRichTextComponent"]'):
            testid = el.get("data-testid")
            if testid == "User-Name":
                who = X_USER_RE.match(" ".join(_node_text(el).split()))
                if not parts:
                    byline = f"{who.group(1).strip()} (@{who.group(2)})" if who else ""
                    parts.append(f"**{byline}**" + (f" · {date}" if date else ""))
                else:  # a second name inside a tweet is the tweet it quotes
                    quoting = True
                    parts.append(f"> **{who.group(1).strip()} (@{who.group(2)})**" if who else "> ")
            elif testid == "twitter-article-title":
                parts.append(f"# {_node_text(el).strip()}")
                if focal and focal[0] is node:
                    first_text = _node_text(el).strip()
            else:  # an X Article's paragraphs are adjacent blocks with no text between them
                pieces = el.xpath('.//*[@data-block="true"]') or [el]
                text = "\n\n".join(t for t in (_node_text(p).strip() for p in pieces) if t)
                parts.append("\n".join(f"> {line}" for line in text.splitlines()) if quoting else text)
                if focal and focal[0] is node and first_text is None:
                    first_text = text
        if len(parts) > 1:  # a media-only tweet has nothing but its byline
            blocks.append("\n\n".join(parts))
    if not blocks:
        return None
    doc = {"text": "\n\n---\n\n".join(blocks), "site": "x.com"}
    if focal:  # same shape as the Twitter lane's documents
        names = focal[0].xpath('.//*[@data-testid="User-Name"]')
        who = X_USER_RE.match(" ".join(_node_text(names[0]).split())) if names else None
        if who:
            doc["author"] = f"{who.group(1).strip()} (@{who.group(2)})"
            if first_text:
                doc["title"] = f"@{who.group(2)}: " + " ".join(first_text.split())[:100]
        doc["date"] = focal[1]
    return doc


def saved_page_to_doc(path: str) -> dict:
    """Runs in an extraction worker. Text and metadata of one saved page, plus its origin."""
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            return {"text": "", "error": "too_large"}
        data = Path(path).read_bytes()
        head = data[:HEAD_BYTES]
        if b"data-sfz" in head:  # the "self-extracting ZIP" format: the page is compressed inside
            return {"text": "", "error": "singlefile_zip"}
        data = DATA_URI_RE.sub(b"data:,", NOISE_RE.sub(b"", data))
    except OSError as e:
        return {"text": "", "error": f"read_error:{type(e).__name__}"}
    except MemoryError:
        return {"text": "", "error": "too_large"}

    header = HEADER_RE.search(head)
    if header:
        url = header.group(1).decode("utf-8", "replace")
    else:
        tag = CANONICAL_RE.search(data[:HEAD_BYTES])
        href = HREF_RE.search(tag.group(0)) if tag else None
        url = html.unescape(href.group(1).decode("utf-8", "replace")) if href else None
    tag = TITLE_RE.search(data)
    title = " ".join(html.unescape(tag.group(1).decode("utf-8", "replace")).split()) if tag else ""
    title = re.sub(r"^\(\d+\+?\)\s*", "", title)  # the unread count browsers show in the tab
    doc = _x_page_doc(data, url) if url and X_URL_RE.match(url) else None
    if doc is None:
        if b"sf-hidden" in data or b"shadowrootmode" in data or b"shadowmode" in data:
            data = _clean_dom(data)
        doc = extract.html_to_doc(data, url)
    if title and not (doc.get("site") == "x.com" and doc.get("title")):
        doc["title"] = title  # what the page called itself; extractors can pick a hidden heading instead
    return {**doc, "url": url, "singlefile": bool(header), "saved_at": _saved_at(header.group(2)) if header else None}


def _store_page(store: Store, path: Path, sha: str, res: dict) -> str:
    """Write the document unless the archive already has a better one; returns the outcome."""
    text, saved_url = res["text"], res["url"]
    r = route(saved_url) if saved_url else None
    if r is None or r.reason == "bad_url":  # no origin, or a file:// or browser-internal one
        url = key = None
        row = None
    else:
        row = store.find_url(r)
        url = row["url"] if row else r.url
        key = (row and row["doc_key"]) or r.doc_key or r.url
    if key is None:
        url, key = f"file:{sha[:16]}/{path.name}", f"file:{sha}"
    if row and row["status"] in DONE and (row["chars"] or 0) >= len(text):
        return "kept existing, it has more text"
    if row and row["status"] == "ok" and row["lane"] in COMPLETE_LANES:
        return f"kept existing {row['lane']} document"

    meta = {"url": saved_url or url, "source": "singlefile" if res["singlefile"] else "html_file",
            "title": res.get("title") or path.stem, "author": res.get("author"), "date": res.get("date"),
            "site": res.get("site")}
    if res["saved_at"]:
        meta["fetched_at"] = res["saved_at"]
    chars = store.write_doc(key, meta, text)
    ok = chars >= extract.MIN_CHARS
    store.record_import(url, key, r.dedup_key if r and r.reason != "bad_url" else key, "ok" if ok else "thin",
                        None if ok else "short", chars, saved_url)
    outcome = "replaced an existing document" if row and row["status"] in DONE else "saved"
    return outcome if ok else f"{outcome} (under {extract.MIN_CHARS} characters)"


async def run_import(store: Store, paths: list[Path], pool: extract.WorkerPool, workers: int) -> None:
    files = sorted({f for p in paths for f in ([p] if p.is_file() else p.rglob("*"))
                    if f.is_file() and f.suffix.lower() in SUFFIXES})
    log.info("%d HTML files found", len(files))
    imported = store.imported_files()
    slots = asyncio.Semaphore(workers + 1)
    outcomes: Counter = Counter()
    failures: list[tuple[Path, str]] = []

    async def one(path: Path) -> None:
        async with slots:
            try:
                sha = await asyncio.to_thread(file_sha1, path)
            except OSError as e:
                failures.append((path, f"read_error:{type(e).__name__}"))
                return
            if sha in imported:
                outcomes["already imported"] += 1
                return
            res = await pool.run(saved_page_to_doc, str(path))
        if not res["text"]:
            failures.append((path, res.get("error") or "no_text"))  # not recorded: a later run tries again
            return
        outcomes[_store_page(store, path, sha, res)] += 1
        store.mark_imported(sha, str(path.resolve()), res["url"])
        done = sum(outcomes.values()) + len(failures)
        if done % 100 == 0:
            log.info("%d of %d files", done, len(files))

    try:
        await asyncio.gather(*(one(f) for f in files))
    finally:
        store.commit()
        for outcome, n in outcomes.most_common():
            print(f"{n:>7}  {outcome}")
        if failures:
            print(f"{len(failures):>7}  failed:")
            for path, reason in failures:
                print(f"         {reason:<24} {path}")
