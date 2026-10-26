"""Generic lane: async fetch of arbitrary pages, routed by Content-Type, extracted to markdown.

Pass 1 uses aiohttp. Pass 2 re-fetches blocked/thin pages with curl_cffi's Chrome
TLS fingerprint and only keeps results that beat what pass 1 got.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from collections import Counter, OrderedDict, deque
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from . import extract
from .store import Store

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BOT_UA = "personal-archiver/0.1 (one-off text archive of saved browser tabs)"
# Wikimedia asks automated clients for a descriptive User-Agent instead of a browser one
WIKIMEDIA = (
    "wikipedia.org", "wikimedia.org", "wiktionary.org", "wikibooks.org", "wikiquote.org",
    "wikisource.org", "wikivoyage.org", "wikidata.org", "mediawiki.org", "wikinews.org",
)
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}

HTML_CAP = 5 * 2**20
PDF_CAP = 50 * 2**20
CHUNK = 1 << 16
MAX_ATTEMPTS = 3
MAX_COOLDOWN = 60.0
HOST_DOWN_AFTER = 3  # consecutive connection-level failures before skipping the rest of a host
# consecutive 403s before skipping the rest of a host (Scholar, Reddit, Cloudflare walls);
# the skipped URLs keep reason http_403, so the curl_cffi pass still gets a sample of them
FORBIDDEN_AFTER = 5

RETRYABLE = {"timeout", "connect", "payload"} | {
    f"http_{c}" for c in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
}
CONNECTION_LEVEL = {"dns", "connect", "ssl", "timeout"}
# failures a browser TLS fingerprint can plausibly get past (bot walls, fingerprint resets)
BLOCKED = ["http_403", "http_429", "http_503", "connect", "ssl", "payload", "no_text"]

TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
SNIFF_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/xml", "text/xml",
               "application/x-download", "application/force-download"}


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def user_agent(url: str, contact: str | None) -> str:
    if host_of(url).endswith(WIKIMEDIA):
        return f"{BOT_UA[:-1]}; {contact})" if contact else BOT_UA
    return BROWSER_UA


def parse_retry_after(value: str | None) -> float | None:
    try:
        return min(float(value), MAX_COOLDOWN) if value else None
    except ValueError:  # HTTP-date form; rare enough to just use the default backoff
        return None


@dataclass
class Fetched:
    status: int = 0
    final_url: str | None = None
    content_type: str | None = None
    charset: str | None = None
    kind: str | None = None  # html | pdf | text
    body: bytes | None = None
    pdf_path: str | None = None  # holds a pdf_slots permit until the caller releases it
    error: str | None = None
    retry_after: float | None = None


class Reader:
    """Turns a response (status, headers, chunk iterator) into a Fetched, client-agnostic."""

    def __init__(self, tmp_dir: Path, pdf_slots: asyncio.Semaphore):
        self.tmp_dir = tmp_dir
        self.pdf_slots = pdf_slots

    async def read(self, status: int, final_url: str, headers, chunks) -> Fetched:
        ctype, _, params = (headers.get("Content-Type") or "").partition(";")
        ctype = ctype.strip().lower()
        charset = params.partition("charset=")[2].strip(" \"'") or None
        f = Fetched(status=status, final_url=final_url, content_type=ctype or None, charset=charset)
        if status >= 400:
            f.error = f"http_{status}"
            f.retry_after = parse_retry_after(headers.get("Retry-After"))
            return f
        if not (ctype in HTML_TYPES or ctype in TEXT_TYPES or ctype in SNIFF_TYPES or ctype == "application/pdf"):
            f.error = f"non_text:{ctype}"  # images, video, archives...: never download the body
            return f
        try:
            if int(headers.get("Content-Length") or 0) > PDF_CAP:
                f.error = "too_large"
                return f
        except ValueError:
            pass

        it = chunks.__aiter__()
        try:
            first = await it.__anext__()
        except StopAsyncIteration:
            first = b""

        if ctype == "application/pdf" or first.startswith(b"%PDF"):
            f.kind = "pdf"
        elif ctype in HTML_TYPES:
            f.kind = "html"
        elif ctype in TEXT_TYPES:
            f.kind = "text"
        elif b"<html" in first[:2048].lower() or first.lstrip()[:15].lower() == b"<!doctype html>":
            f.kind = "html"
        else:
            f.error = f"non_text:{ctype or 'unknown'}"
            return f

        if f.kind == "pdf":
            await self._stream_pdf(f, first, it)
        else:
            buf = bytearray(first)
            async for chunk in it:
                buf += chunk
                if len(buf) > HTML_CAP:
                    f.error = "too_large"
                    return f
            f.body = bytes(buf)
        return f

    async def _stream_pdf(self, f: Fetched, first: bytes, it) -> None:
        await self.pdf_slots.acquire()
        path = self.tmp_dir / f"{uuid.uuid4().hex}.pdf"
        try:
            size = len(first)
            with open(path, "wb") as out:
                out.write(first)
                async for chunk in it:
                    size += len(chunk)
                    if size > PDF_CAP:
                        f.error = "too_large"
                        break
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            self.pdf_slots.release()
            raise
        if f.error:
            path.unlink(missing_ok=True)
            self.pdf_slots.release()
        else:
            f.pdf_path = str(path)

    def release_pdf(self, f: Fetched) -> None:
        if f.pdf_path:
            Path(f.pdf_path).unlink(missing_ok=True)
            f.pdf_path = None
            self.pdf_slots.release()


class AiohttpClient:
    def __init__(self, reader: Reader, concurrency: int, per_host: int, contact: str | None):
        self.reader = reader
        self.contact = contact
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=concurrency,
                limit_per_host=per_host,
                ttl_dns_cache=600,
                resolver=aiohttp.AsyncResolver(),
            ),
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=15),
            headers=BASE_HEADERS,
            max_line_size=32768,  # some sites send huge Set-Cookie/CSP headers
            max_field_size=32768,
        )

    async def fetch(self, url: str) -> Fetched:
        try:
            async with self.session.get(
                url, headers={"User-Agent": user_agent(url, self.contact)}, max_redirects=5
            ) as resp:
                return await self.reader.read(resp.status, str(resp.url), resp.headers, resp.content.iter_chunked(CHUNK))
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            return Fetched(error="timeout")
        except aiohttp.TooManyRedirects:
            return Fetched(error="redirects")
        except aiohttp.ClientConnectorDNSError:
            return Fetched(error="dns")
        except aiohttp.ClientSSLError:
            return Fetched(error="ssl")
        except aiohttp.ClientConnectorError:
            return Fetched(error="connect")
        except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError):
            return Fetched(error="payload")
        except aiohttp.ClientResponseError as e:
            return Fetched(status=e.status, error=f"http_{e.status}" if e.status else "bad_response")
        except (aiohttp.InvalidURL, ValueError, UnicodeError):
            return Fetched(error="bad_url")

    async def close(self) -> None:
        await self.session.close()


class CurlClient:
    """Browser TLS/HTTP2 fingerprint via curl_cffi, for sites that block plain clients."""

    def __init__(self, reader: Reader, concurrency: int):
        from curl_cffi.requests import AsyncSession

        self.reader = reader
        self.session = AsyncSession(impersonate="chrome", max_clients=concurrency, timeout=30)

    async def fetch(self, url: str) -> Fetched:
        resp = None
        try:
            resp = await self.session.get(url, stream=True, allow_redirects=True, max_redirects=5)
            return await self.reader.read(resp.status_code, str(resp.url), resp.headers, resp.aiter_content(CHUNK))
        except Exception as e:  # curl_cffi exception names map cleanly onto our reasons
            name = type(e).__name__
            for needle, reason in (("Timeout", "timeout"), ("DNS", "dns"), ("SSL", "ssl"), ("Certificate", "ssl"),
                                   ("Redirect", "redirects"), ("Connection", "connect")):
                if needle in name:
                    return Fetched(error=reason)
            return Fetched(error=f"error:{name}")
        finally:
            if resp is not None:
                await resp.aclose()

    async def close(self) -> None:
        await self.session.close()


class HostScheduler:
    """Hands out URLs round-robin across hosts with at most per_host in flight per host,
    skipping hosts that are cooling down after a 429/5xx/timeout. No head-of-line blocking:
    a worker never waits on a busy host while another host has work."""

    def __init__(self, urls: list[str], per_host: int):
        self.queues: OrderedDict[str, deque[str]] = OrderedDict()
        for url in urls:
            self.queues.setdefault(host_of(url), deque()).append(url)
        self.per_host = per_host
        self.active: Counter[str] = Counter()
        self.in_flight = 0
        self.cooldown: dict[str, float] = {}
        self.changed = asyncio.Event()

    async def get(self) -> tuple[str, str] | None:
        while True:
            if not self.queues and self.in_flight == 0:
                return None
            now = time.monotonic()
            wake = None
            for host, queue in self.queues.items():
                if self.active[host] >= self.per_host:
                    continue
                until = self.cooldown.get(host, 0.0)
                if until > now:
                    wake = until if wake is None else min(wake, until)
                    continue
                url = queue.popleft()
                if queue:
                    self.queues.move_to_end(host)
                else:
                    del self.queues[host]
                self.active[host] += 1
                self.in_flight += 1
                return host, url
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), None if wake is None else max(0.05, wake - now))
            except asyncio.TimeoutError:
                pass

    def done(self, host: str, requeue: str | None = None, cooldown: float = 0.0, drop: bool = False) -> list[str]:
        """Release a slot; optionally retry a URL later, cool the host down, or drop the host."""
        self.active[host] -= 1
        self.in_flight -= 1
        if cooldown:
            self.cooldown[host] = max(self.cooldown.get(host, 0.0), time.monotonic() + cooldown)
        dropped: list[str] = []
        if drop:
            dropped = list(self.queues.pop(host, ()))
        elif requeue:
            self.queues.setdefault(host, deque()).appendleft(requeue)
        self.changed.set()
        return dropped


class GenericPass:
    def __init__(self, store: Store, client, reader: Reader, pool: Executor, workers: int,
                 prev_chars: dict[str, int] | None = None):
        self.store = store
        self.client = client
        self.reader = reader
        self.pool = pool
        self.extract_slots = asyncio.Semaphore(2 * workers)
        self.prev_chars = prev_chars  # set on the upgrade pass: only better results get recorded
        self.attempts: Counter[str] = Counter()
        self.host_failures: Counter[str] = Counter()
        self.host_forbidden: Counter[str] = Counter()

    async def run(self, urls: list[str], concurrency: int, per_host: int) -> None:
        sched = HostScheduler(urls, per_host)
        await asyncio.gather(*(self._worker(sched) for _ in range(concurrency)))
        self.store.commit()

    async def _worker(self, sched: HostScheduler) -> None:
        while (item := await sched.get()) is not None:
            host, url = item
            requeue, cooldown, drop = None, 0.0, None
            try:
                fetched = await self.client.fetch(url)
                self.attempts[url] += 1
                reason = fetched.error
                self.host_failures[host] = self.host_failures[host] + 1 if reason in CONNECTION_LEVEL else 0
                self.host_forbidden[host] = self.host_forbidden[host] + 1 if reason == "http_403" else 0
                if reason is None:
                    await self._extract(url, fetched)
                    continue
                if reason == "dns" or self.host_failures[host] >= HOST_DOWN_AFTER:
                    drop = "host_down"
                elif self.host_forbidden[host] >= FORBIDDEN_AFTER:
                    drop = "http_403"
                if reason in RETRYABLE and self.attempts[url] < MAX_ATTEMPTS and not drop:
                    requeue = url
                    cooldown = fetched.retry_after or min(2 ** self.attempts[url] + random.uniform(0, 2), MAX_COOLDOWN)
                else:
                    self._fail(url, reason, fetched)
            except Exception as e:
                log.exception("unexpected error on %s", url)
                self._fail(url, f"bug:{type(e).__name__}", Fetched())
            finally:
                for lost in sched.done(host, requeue, cooldown, drop is not None):
                    self._fail(lost, drop, Fetched())

    def _fail(self, url: str, reason: str, f: Fetched) -> None:
        if self.prev_chars is not None:
            return  # upgrade pass: keep the pass-1 record
        self.store.record(url, "failed", reason, http_status=f.status or None,
                          final_url=f.final_url, content_type=f.content_type)

    async def _extract(self, url: str, f: Fetched) -> None:
        loop = asyncio.get_running_loop()
        try:
            if f.kind == "html":
                async with self.extract_slots:
                    res = await loop.run_in_executor(self.pool, extract.html_to_doc, f.body, f.final_url or url)
            elif f.kind == "pdf":
                async with self.extract_slots:
                    res = await loop.run_in_executor(self.pool, extract.pdf_to_doc, f.pdf_path)
            else:
                res = {"text": f.body.decode(f.charset or "utf-8", errors="replace")}
        except LookupError:  # unknown charset name
            res = {"text": f.body.decode("utf-8", errors="replace")}
        finally:
            self.reader.release_pdf(f)

        text = (res.get("text") or "").strip()
        if not text:
            self._fail(url, res.get("error") or "no_text", f)
            return
        if self.prev_chars is not None and len(text) <= self.prev_chars.get(url, -1):
            return
        meta = {
            "url": url,
            "final_url": f.final_url if f.final_url != url else None,
            "source": f.kind,
            "title": res.get("title"),
            "author": res.get("author"),
            "date": res.get("date"),
            "site": res.get("site"),
        }
        chars = self.store.write_doc(url, meta, text)
        ok = chars >= extract.MIN_CHARS
        self.store.record(url, "ok" if ok else "thin", None if ok else "short", chars=chars,
                          http_status=f.status, final_url=f.final_url, content_type=f.content_type)


async def run_generic(store: Store, pool: Executor, workers: int, concurrency: int, per_host: int,
                      contact: str | None, impersonate: bool) -> None:
    pdf_slots = asyncio.Semaphore(3)
    reader = Reader(store.tmp_dir, pdf_slots)

    urls = [key for key, _ in store.pending("generic")]
    log.info("generic: %d URLs across %d hosts", len(urls), len({host_of(u) for u in urls}))
    client = AiohttpClient(reader, concurrency, per_host, contact)
    try:
        await GenericPass(store, client, reader, pool, workers).run(urls, concurrency, per_host)
    finally:
        await client.close()

    if not impersonate:
        return
    candidates = store.retry_candidates("generic", BLOCKED)
    if not candidates:
        return
    log.info("generic: retrying %d blocked/thin URLs with a browser fingerprint", len(candidates))
    client = CurlClient(reader, concurrency=16)
    try:
        upgrade = GenericPass(store, client, reader, pool, workers, prev_chars=dict(candidates))
        await upgrade.run([key for key, _ in candidates], concurrency=16, per_host=2)
    finally:
        await client.close()
