"""Generic lane: async fetch of arbitrary pages, routed by Content-Type, extracted to markdown.

Pass 1 uses aiohttp. Then fallback passes over what it couldn't get, cheapest first, each
keeping a result only when it beats what came before:
- impersonate: blocked/thin pages again with curl_cffi's Chrome TLS fingerprint
- render (--render): blocked/thin pages in headless Chromium (osiris/render.py)
- doi: title/authors/abstract for paper links, via OpenAlex (osiris/doi.py)
- wayback: the newest 200 capture in the Wayback Machine for anything still failed or thin
Each URL gets each fallback pass once (manifest column `tried`); --retry-failed resets that.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import random
import time
import uuid
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from . import extract
from .cookies import BrowserCookie, into_aiohttp, to_cookiejar
from .store import REFRESH, Store

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BOT_UA = "osiris/0.1 (https://github.com/barancimen/osiris; one-off text archive of saved browser tabs)"
# Wikimedia wants automated clients to send a descriptive User-Agent with contact details: a
# browser UA, or one without a URL or email, gets 403 "Please respect our robot policy"
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
# consecutive refusals (403, or 429 after its retries) before skipping the rest of a host
# (Scholar, Reddit, Cloudflare walls); the skipped URLs keep that reason, so the fallback
# passes still get to try them
FORBIDDEN_AFTER = 5

RETRYABLE = {"timeout", "connect", "payload"} | {
    f"http_{c}" for c in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
}
CONNECTION_LEVEL = {"dns", "connect", "ssl", "timeout"}
# failures a browser TLS fingerprint can plausibly get past (bot walls, fingerprint resets)
BLOCKED = {"http_403", "http_429", "http_503", "connect", "ssl", "payload", "no_text"}
# failures no archived copy can fix
NOT_FOR_WAYBACK = ("non_text:", "too_large", "bad_url", "bug:")

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_CONCURRENCY = 4  # archive.org is a nonprofit; keep the load modest
WAYBACK_INTERVAL = 1.0  # seconds between request starts: 4 unspaced workers drew 429s within a minute


@dataclass(frozen=True)
class HostRule:
    """Hosts matching `pattern` (fnmatch on the hostname) share one queue: at most
    `concurrency` requests in flight and `delay` seconds between request starts."""

    pattern: str
    concurrency: int
    delay: float = 0.0

    @classmethod
    def parse(cls, spec: str) -> HostRule:
        """'scholar.google.*=1:8' -> one request at a time, 8 s apart."""
        pattern, _, limits = spec.partition("=")
        concurrency, _, delay = limits.partition(":")
        return cls(pattern.strip().lower(), max(1, int(concurrency or 1)), float(delay or 0))


# Google Scholar shows CAPTCHAs after a burst of requests even from home IPs, and treats
# all its country domains as one service
DEFAULT_HOST_RULES = (HostRule("scholar.google.*", 1, 8.0),)

TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
SNIFF_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/xml", "text/xml",
               "application/x-download", "application/force-download"}


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def user_agent(url: str, contact: str | None, browser_ua: str | None = None) -> str:
    if host_of(url).endswith(WIKIMEDIA):
        return f"{BOT_UA[:-1]}; {contact})" if contact else BOT_UA
    return browser_ua or BROWSER_UA


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
    via: str | None = None  # fallback that produced it: impersonate | wayback


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
    def __init__(self, reader: Reader, concurrency: int, per_host: int, contact: str | None,
                 cookies: list[BrowserCookie] = (), browser_ua: str | None = None):
        self.reader = reader
        self.contact = contact
        self.browser_ua = browser_ua
        jar = aiohttp.CookieJar(quote_cookie=False)
        into_aiohttp(list(cookies), jar)
        self.session = aiohttp.ClientSession(
            cookie_jar=jar,
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
        return await aiohttp_get(self.session, self.reader, url,
                                 headers={"User-Agent": user_agent(url, self.contact, self.browser_ua)})

    async def close(self) -> None:
        await self.session.close()


async def aiohttp_get(session: aiohttp.ClientSession, reader: Reader, url: str, **kwargs) -> Fetched:
    """GET through Reader, with aiohttp's exceptions mapped onto manifest reasons."""
    try:
        async with session.get(url, max_redirects=5, **kwargs) as resp:
            return await reader.read(resp.status, str(resp.url), resp.headers, resp.content.iter_chunked(CHUNK))
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


class WaybackClient:
    """The newest successful Wayback Machine capture of a URL, as originally served
    (the id_ form: no toolbar, no rewritten links)."""

    def __init__(self, reader: Reader):
        self.reader = reader
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=90, connect=15, sock_read=45),
            headers={"User-Agent": BOT_UA},
        )
        self.slots = asyncio.Semaphore(WAYBACK_CONCURRENCY)
        self.paused_until = 0.0  # archive.org rate limits apply to all our requests at once
        self.next_start = 0.0
        self.spacing = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self.spacing:
            start = max(time.monotonic(), self.next_start, self.paused_until)
            self.next_start = start + WAYBACK_INTERVAL
        await asyncio.sleep(max(0.0, start - time.monotonic()))

    async def _pause_if_limited(self, status: int, headers) -> bool:
        if status != 429:
            return False
        wait = parse_retry_after(headers.get("Retry-After")) or 30.0
        self.paused_until = max(self.paused_until, time.monotonic() + wait)
        log.warning("wayback: rate limited, pausing %.0fs", wait)
        return True

    async def _latest_capture(self, url: str) -> tuple[str, str] | str:
        """(timestamp, original url) of the newest 200 capture, or a failure reason."""
        params = {"url": url, "output": "json", "fl": "timestamp,original", "filter": "statuscode:200",
                  "limit": "-1"}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._throttle()
            try:
                async with self.session.get(WAYBACK_CDX, params=params) as resp:
                    if await self._pause_if_limited(resp.status, resp.headers):
                        continue
                    if resp.status >= 400:
                        return f"wayback_http_{resp.status}"
                    rows = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                await asyncio.sleep(2**attempt)
                continue
            return (rows[1][0], rows[1][1]) if rows and len(rows) > 1 else "wayback_none"
        return "wayback_unreachable"

    async def fetch(self, url: str) -> Fetched:
        async with self.slots:
            capture = await self._latest_capture(url)
            if isinstance(capture, str):
                return Fetched(error=capture)
            timestamp, original = capture
            for _ in range(MAX_ATTEMPTS):
                await self._throttle()
                f = await aiohttp_get(self.session, self.reader, f"https://web.archive.org/web/{timestamp}id_/{original}")
                if f.status != 429 or not await self._pause_if_limited(429, {}):
                    break
            f.via = "wayback"
            return f

    async def close(self) -> None:
        await self.session.close()


class CurlClient:
    """Browser TLS/HTTP2 fingerprint via curl_cffi, for sites that block plain clients."""

    def __init__(self, reader: Reader, concurrency: int, cookies: list[BrowserCookie] = ()):
        from curl_cffi.requests import AsyncSession, Cookies

        self.reader = reader
        self.session = AsyncSession(impersonate="chrome", max_clients=concurrency, timeout=30)
        if cookies:
            self.session.cookies = Cookies(to_cookiejar(list(cookies)))

    async def fetch(self, url: str) -> Fetched:
        resp = None
        try:
            resp = await self.session.get(url, stream=True, allow_redirects=True, max_redirects=5)
            f = await self.reader.read(resp.status_code, str(resp.url), resp.headers, resp.aiter_content(CHUNK))
            f.via = "impersonate"
            return f
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

    def __init__(self, urls: list[str], per_host: int, rules: tuple[HostRule, ...] = ()):
        self.rules = rules
        self.queues: OrderedDict[str, deque[str]] = OrderedDict()
        for url in urls:
            self.queues.setdefault(self.key_of(url), deque()).append(url)
        self.per_host = per_host
        self.limits = {r.pattern: r for r in rules}
        self.active: Counter[str] = Counter()
        self.in_flight = 0
        self.cooldown: dict[str, float] = {}
        self.changed = asyncio.Event()

    def key_of(self, url: str) -> str:
        """The queue a URL waits in: its host, or the pattern of the first rule matching it."""
        host = host_of(url)
        return next((r.pattern for r in self.rules if fnmatch.fnmatch(host, r.pattern)), host)

    async def get(self) -> tuple[str, str] | None:
        while True:
            if not self.queues and self.in_flight == 0:
                return None
            now = time.monotonic()
            wake = None
            for key, queue in self.queues.items():
                rule = self.limits.get(key)
                if self.active[key] >= (rule.concurrency if rule else self.per_host):
                    continue
                until = self.cooldown.get(key, 0.0)
                if until > now:
                    wake = until if wake is None else min(wake, until)
                    continue
                url = queue.popleft()
                if queue:
                    self.queues.move_to_end(key)
                else:
                    del self.queues[key]
                if rule and rule.delay:
                    self.cooldown[key] = max(until, now + rule.delay * random.uniform(0.75, 1.25))
                self.active[key] += 1
                self.in_flight += 1
                return key, url
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
    def __init__(self, store: Store, client, reader: Reader, pool: extract.WorkerPool, workers: int,
                 keep_raw: bool = False, prev_chars: dict[str, int] | None = None,
                 pass_name: str | None = None, rules: tuple[HostRule, ...] = (), pdf_only: bool = False):
        self.store = store
        self.pdf_only = pdf_only  # a refreshed PDF isn't replaced by whatever page its URL now serves
        self.keep_raw = keep_raw
        self.pass_name = pass_name  # fallback passes mark each URL they finish in `tried`
        self.rules = rules
        self.client = client
        self.reader = reader
        self.pool = pool
        self.extract_slots = asyncio.Semaphore(2 * workers)
        self.prev_chars = prev_chars  # set on the upgrade pass: only better results get recorded
        self.attempts: Counter[str] = Counter()
        self.host_failures: Counter[str] = Counter()
        self.host_forbidden: Counter[str] = Counter()

    async def run(self, urls: list[str], concurrency: int, per_host: int) -> None:
        sched = HostScheduler(urls, per_host, self.rules)
        await asyncio.gather(*(self._worker(sched) for _ in range(concurrency)))
        self.store.commit()

    async def _worker(self, sched: HostScheduler) -> None:
        while (item := await sched.get()) is not None:
            key, url = item  # key: the host, or the HostRule pattern grouping several hosts
            requeue, cooldown, drop = None, 0.0, None
            interrupted = False
            try:
                fetched = await self.client.fetch(url)
                self.attempts[url] += 1
                reason = fetched.error
                self.host_failures[key] = self.host_failures[key] + 1 if reason in CONNECTION_LEVEL else 0
                if reason is None:
                    self.host_forbidden[key] = 0
                    await self._extract(url, fetched)
                    continue
                if reason == "dns" or self.host_failures[key] >= HOST_DOWN_AFTER:
                    drop = "host_down"
                retry = reason in RETRYABLE and self.attempts[url] < MAX_ATTEMPTS and drop is None
                if reason == "http_403" or (reason == "http_429" and not retry):
                    self.host_forbidden[key] += 1
                    if drop is None and self.host_forbidden[key] >= FORBIDDEN_AFTER:
                        drop = reason
                elif reason != "http_429":
                    self.host_forbidden[key] = 0
                if retry:
                    requeue = url
                    cooldown = fetched.retry_after or min(2 ** self.attempts[url] + random.uniform(0, 2), MAX_COOLDOWN)
                else:
                    self._fail(url, reason, fetched)
            except asyncio.CancelledError:
                interrupted = True  # not finished, so the next run must try it again
                raise
            except Exception as e:
                log.exception("unexpected error on %s", url)
                self._fail(url, f"bug:{type(e).__name__}", Fetched())
            finally:
                if requeue is None and not interrupted:
                    self._mark_tried(url)
                for lost in sched.done(key, requeue, cooldown, drop is not None):
                    self._fail(lost, drop, Fetched())
                    self._mark_tried(lost)

    def _mark_tried(self, url: str) -> None:
        if self.pass_name:
            self.store.mark_tried(url, self.pass_name)

    def _fail(self, url: str, reason: str, f: Fetched, raw_path: str | None = None) -> None:
        if self.prev_chars is not None:
            return  # upgrade pass: keep the pass-1 record
        self.store.record(url, "failed", reason, http_status=f.status or None,
                          final_url=f.final_url, content_type=f.content_type, raw_path=raw_path)

    def _save_raw(self, url: str, f: Fetched) -> str:
        if f.kind == "pdf":
            return self.store.save_raw(url, "pdf", move_from=f.pdf_path)
        return self.store.save_raw(url, "html.zst" if f.kind == "html" else "txt.zst", data=f.body)

    async def _extract(self, url: str, f: Fetched) -> None:
        if self.pdf_only and f.kind != "pdf":
            self.reader.release_pdf(f)
            return
        raw_path = None
        try:
            try:
                if f.kind == "html":
                    async with self.extract_slots:
                        # an archived copy is still the original page as far as metadata goes
                        page_url = url if f.via == "wayback" else (f.final_url or url)
                        res = await self.pool.run(extract.html_to_doc, f.body, page_url)
                elif f.kind == "pdf":
                    async with self.extract_slots:
                        res = await self.pool.run(extract.pdf_to_doc, f.pdf_path)
                else:
                    res = {"text": f.body.decode(f.charset or "utf-8", errors="replace")}
            except LookupError:  # unknown charset name
                res = {"text": f.body.decode("utf-8", errors="replace")}
            text = (res.get("text") or "").strip()
            # the upgrade pass only records (and keeps raw for) results that beat pass 1
            better = self.prev_chars is None or len(text) > max(self.prev_chars.get(url, -1), 0)
            if self.keep_raw and better:
                raw_path = self._save_raw(url, f)
        finally:
            if raw_path and f.kind == "pdf":  # the download now lives in raw/; just free the slot
                f.pdf_path = None
                self.reader.pdf_slots.release()
            else:
                self.reader.release_pdf(f)

        if not text:
            self._fail(url, res.get("error") or "no_text", f, raw_path)
            return
        if not better:
            return
        meta = {
            "url": url,
            "final_url": f.final_url if f.final_url != url else None,
            "source": f"{f.kind}:{f.via}" if f.via else f.kind,
            "title": res.get("title"),
            "author": res.get("author"),
            "date": res.get("date"),
            "site": res.get("site"),
        }
        chars = self.store.write_doc(url, meta, text)
        ok = chars >= extract.MIN_CHARS
        self.store.record(url, "ok" if ok else "thin", None if ok else "short", chars=chars,
                          http_status=f.status, final_url=f.final_url, content_type=f.content_type,
                          raw_path=raw_path)


async def run_generic(store: Store, pool: extract.WorkerPool, workers: int, concurrency: int, per_host: int,
                      contact: str | None, impersonate: bool, keep_raw: bool, wayback: bool, doi: bool = True,
                      rules: tuple[HostRule, ...] = DEFAULT_HOST_RULES, cookies: list[BrowserCookie] = (),
                      browser_ua: str | None = None, render: bool = False, render_concurrency: int = 2,
                      chrome_profile: str | None = None, challenge_wait: float = 15.0,
                      refresh: list[tuple[str, str, int]] = ()) -> None:
    pdf_slots = asyncio.Semaphore(3)
    reader = Reader(store.tmp_dir, pdf_slots)

    urls = [key for key, _ in store.pending("generic")]
    log.info("generic: %d URLs across %d hosts", len(urls), len({host_of(u) for u in urls}))
    client = AiohttpClient(reader, concurrency, per_host, contact, cookies, browser_ua)
    try:
        await GenericPass(store, client, reader, pool, workers, keep_raw, rules=rules).run(urls, concurrency, per_host)
        if refresh:
            log.info("generic: refreshing %d PDFs that have text; each keeps its text unless the new copy has more",
                     len(refresh))
            await GenericPass(store, client, reader, pool, workers, keep_raw, prev_chars={key: chars for key, _, chars in refresh},
                              pass_name=REFRESH, rules=rules, pdf_only=True).run([key for key, _, _ in refresh],
                                                                                 concurrency, per_host)
    finally:
        await client.close()

    async def fallback(name, make_client, pass_concurrency, pass_per_host, pass_rules, eligible, what, url_ok=None):
        candidates = [(key, chars) for key, chars in store.retry_candidates("generic", name, eligible)
                      if url_ok is None or url_ok(key)]
        if not candidates:
            return
        log.info("generic: %s pass: %d %s", name, len(candidates), what)
        client = make_client()
        try:
            upgrade = GenericPass(store, client, reader, pool, workers, keep_raw, prev_chars=dict(candidates),
                                  pass_name=name, rules=pass_rules)
            await upgrade.run([key for key, _ in candidates], pass_concurrency, pass_per_host)
        finally:
            await client.close()

    # cheapest first: each pass only sees what the passes before it couldn't fix
    if impersonate:
        await fallback("impersonate", lambda: CurlClient(reader, 16, cookies), 16, 2, rules,
                       lambda reason: reason in BLOCKED, "blocked/thin URLs with a browser fingerprint")
    if render:
        from .render import RenderClient

        await fallback("render", lambda: RenderClient(render_concurrency, cookies, browser_ua, chrome_profile,
                                                      challenge_wait),
                       render_concurrency, 1, rules, lambda reason: reason in BLOCKED,
                       "blocked/thin pages in a headless browser", url_ok=lambda u: not u.lower().endswith(".pdf"))
    if doi:
        from .doi import run_doi_pass

        await run_doi_pass(store, contact)
    if wayback:
        # requests go to archive.org, so the per-site rules don't apply
        await fallback("wayback", lambda: WaybackClient(reader), WAYBACK_CONCURRENCY, WAYBACK_CONCURRENCY, (),
                       lambda reason: not reason.startswith(NOT_FOR_WAYBACK), "failed/thin URLs from the Wayback Machine")
