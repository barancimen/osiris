"""Your browser's cookies, for sites that only serve a logged-in or trusted session
(Google Scholar, Medium, paywalled or Cloudflare-walled pages) - the way ArchiveBox and
Karakeep archive them.

Export cookies from your browser as either
- a Netscape cookies.txt, the format wget, curl and yt-dlp read (e.g. the
  "Get cookies.txt LOCALLY" extension), or
- a JSON list of {name, value, domain, path, secure, expirationDate|expires} objects
  (e.g. the Cookie-Editor extension).

Each client gets them in its own domain-aware cookie jar, so a cookie is only ever sent
to the site it belongs to, including across redirects. The file holds live sessions:
keep it private.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from pathlib import Path


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str  # leading dot: also sent to subdomains
    path: str
    secure: bool
    expires: int | None  # None: session cookie

    @property
    def host_only(self) -> bool:
        return not self.domain.startswith(".")


def _expiry(value) -> int | None:
    try:
        expires = int(float(value or 0))
    except (TypeError, ValueError):
        return None
    return expires if expires > 0 else None


def load(path: str | Path) -> list[BrowserCookie]:
    text = Path(path).read_text(encoding="utf-8")
    cookies = []
    if text.lstrip().startswith("["):
        for c in json.loads(text):
            domain = c.get("domain") or ""
            if c.get("hostOnly") is False and not domain.startswith("."):
                domain = "." + domain
            cookies.append(BrowserCookie(c["name"], str(c.get("value", "")), domain, c.get("path") or "/",
                                         bool(c.get("secure")), _expiry(c.get("expirationDate", c.get("expires")))))
    else:
        for line in text.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                continue
            domain, subdomains, cpath, secure, expires, name, value = fields
            if subdomains.upper() == "TRUE" and not domain.startswith("."):
                domain = "." + domain
            cookies.append(BrowserCookie(name, value, domain, cpath or "/", secure.upper() == "TRUE", _expiry(expires)))
    now = time.time()
    return [c for c in cookies if c.expires is None or c.expires > now]


def to_cookiejar(cookies: list[BrowserCookie]) -> CookieJar:
    """A stdlib jar (what curl_cffi takes)."""
    jar = CookieJar()
    for c in cookies:
        jar.set_cookie(Cookie(
            version=0, name=c.name, value=c.value, port=None, port_specified=False,
            domain=c.domain, domain_specified=not c.host_only, domain_initial_dot=not c.host_only,
            path=c.path, path_specified=True, secure=c.secure, expires=c.expires,
            discard=c.expires is None, comment=None, comment_url=None, rest={},
        ))
    return jar


def into_aiohttp(cookies: list[BrowserCookie], jar) -> None:
    """Load into an aiohttp.CookieJar, keeping host-only cookies host-only."""
    from http.cookies import SimpleCookie

    from yarl import URL

    for c in cookies:
        morsel = SimpleCookie()
        morsel[c.name] = c.value
        m = morsel[c.name]
        m["path"] = c.path
        if not c.host_only:
            m["domain"] = c.domain
        if c.secure:
            m["secure"] = True
        if c.expires:
            m["expires"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(c.expires))
        jar.update_cookies(morsel, URL(f"https://{c.domain.lstrip('.')}/"))


def for_playwright(cookies: list[BrowserCookie]) -> list[dict]:
    out = []
    for c in cookies:
        d = {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure}
        if c.expires:
            d["expires"] = c.expires
        out.append(d)
    return out
