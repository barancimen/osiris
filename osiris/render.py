"""Headless-browser fallback (--render): pages that only exist after JavaScript runs, and
bot-challenge pages that clear themselves in a real browser - the way ArchiveBox, Karakeep
and Linkwarden capture pages with Chromium.

Uses Playwright's Chromium (pip install -e .[render] && playwright install chromium), with
your --cookies, or a copy of your own Chrome profile via --chrome-profile (ArchiveBox's
CHROME_USER_DATA_DIR idea: logged-in sessions come for free). Needs a few hundred MB of RAM
per open page, so concurrency stays low.
"""

from __future__ import annotations

import asyncio
import logging

from .cookies import BrowserCookie, for_playwright
from .generic import Fetched

log = logging.getLogger(__name__)

# titles of interstitials that resolve themselves (Cloudflare, DDoS-Guard, Sucuri, ...)
CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser", "please wait",
                    "verifying you are human", "ddos-guard", "security check", "one more step")
SKIPPED_RESOURCES = {"image", "media", "font"}  # text is all we keep
NAV_ERRORS = (("ERR_NAME_NOT_RESOLVED", "dns"), ("ERR_CONNECTION", "connect"), ("ERR_SSL", "ssl"),
              ("ERR_CERT", "ssl"), ("ERR_TOO_MANY_REDIRECTS", "redirects"), ("Download is starting", "non_text:download"),
              ("ERR_ABORTED", "non_text:download"))


STOP_WAIT = 15.0  # seconds; the browser itself has already closed by then


class RenderClient:
    def __init__(self, concurrency: int, cookies: list[BrowserCookie] = (), browser_ua: str | None = None,
                 chrome_profile: str | None = None, challenge_wait: float = 15.0):
        self.slots = asyncio.Semaphore(concurrency)
        self.cookies = list(cookies)
        self.browser_ua = browser_ua
        self.chrome_profile = chrome_profile
        self.challenge_wait = challenge_wait
        self.started = asyncio.Lock()
        self.pw = self.browser = self.context = None

    async def start(self) -> None:
        async with self.started:
            if self.context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise RuntimeError("Playwright isn't installed. Run: .venv/bin/pip install -e '.[render]' "
                                   "&& .venv/bin/playwright install chromium") from None

            self.pw = await async_playwright().start()
            if self.chrome_profile:
                # your installed Chrome, which can decrypt its own profile's cookies
                self.context = await self.pw.chromium.launch_persistent_context(
                    self.chrome_profile, channel="chrome", headless=True)
            else:
                self.browser = await self.pw.chromium.launch(headless=True)
                self.context = await self.browser.new_context(user_agent=self.browser_ua, locale="en-US")
            if self.cookies:
                await self.context.add_cookies(for_playwright(self.cookies))
            await self.context.route("**/*", self._filter)

    @staticmethod
    async def _filter(route) -> None:
        if route.request.resource_type in SKIPPED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    async def _settle(self, page) -> bool:
        """Wait out a challenge interstitial; True once the page is no longer one."""
        for _ in range(int(self.challenge_wait * 2) + 1):
            title = (await page.title()).lower()
            if not any(t in title for t in CHALLENGE_TITLES):
                break
            await page.wait_for_timeout(500)
        else:
            return False
        try:  # let late XHR-rendered content land
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        return True

    async def fetch(self, url: str) -> Fetched:
        from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

        await self.start()
        async with self.slots:
            page = await self.context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                status = resp.status if resp else 0
                ctype = (resp.headers.get("content-type", "") if resp else "").split(";")[0].strip().lower()
                if ctype and "html" not in ctype:
                    return Fetched(status=status, error=f"non_text:{ctype}")
                cleared = await self._settle(page)
                if not cleared:
                    return Fetched(status=status, final_url=page.url, error="challenge")
                html = await page.content()
                # a challenge answers 403/503 first and then reloads into the real page
                if status >= 400 and status not in (403, 429, 503):
                    return Fetched(status=status, final_url=page.url, error=f"http_{status}")
                return Fetched(status=200 if status >= 400 else status, final_url=page.url, content_type="text/html",
                               kind="html", body=html.encode("utf-8"), via="render")
            except PlaywrightTimeout:
                return Fetched(error="timeout")
            except PlaywrightError as e:
                message = str(e)
                return Fetched(error=next((r for needle, r in NAV_ERRORS if needle in message), "render_error"))
            finally:
                await page.close()

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self.pw is not None:
            try:  # an extraction worker started after the browser holds Playwright's pipe open, so this can never finish
                await asyncio.wait_for(self.pw.stop(), STOP_WAIT)
            except asyncio.TimeoutError:
                pass
