"""Google Scholar: recognising its block page, leaving it alone for the rest of a run once it blocks,
and asking for a profile's whole list of works.

Scholar rations automated requests: past some number, it answers with an "unusual traffic" page, often
as an ordinary 200, and a headless browser gets that page about four times in five. Saving it as the
document loses the link, and asking again only keeps the block going, so the first block page ends
Scholar's turn for the run: that link and every Scholar link not reached yet wait for the next run.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

GOOGLE = re.compile(r"(^|\.)google(\.com?)?\.[a-z]{2,3}$")  # scholar.google.com, www.google.co.uk, ...
# the block page as Google serves it, and as text once extracted
BLOCK = re.compile(r"unusual traffic from your computer network|systems have detected unusual traffic"
                   r"|sending automated queries|automated queries from your computer", re.I)
BLOCK_PAGE_MAX = 200_000  # bytes of HTML: a real Scholar page that quotes those words is longer
PROFILE_PAGE = 100  # works on a profile page; Scholar shows 20 unless asked, and 100 is the most it gives at once

_blocked = False  # this run has seen a block page: one process is one run


def is_google(url: str | None) -> bool:
    return bool(url) and bool(GOOGLE.search((urlsplit(url).hostname or "").lower()))


def is_scholar(url: str | None) -> bool:
    return bool(url) and (urlsplit(url).hostname or "").lower().startswith("scholar.google.")


def is_block(url: str, status: int, final_url: str | None, body: bytes | str | None) -> bool:
    """Whether Google answered this request with its block page, or a refusal that means the same."""
    if not (is_google(url) or is_google(final_url)):
        return False
    if status in (403, 429) or "/sorry/" in (urlsplit(final_url or "").path or ""):
        return True
    if not body or len(body) > BLOCK_PAGE_MAX:
        return False
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return bool(BLOCK.search(text))


def blocked() -> bool:
    return _blocked


def note_block(url: str) -> None:
    global _blocked
    if not _blocked:
        log.warning("google scholar: blocked at %s, so its remaining links wait for the next run", url[:100])
    _blocked = True


def reset() -> None:
    """For tests: a fresh run."""
    global _blocked
    _blocked = False


def fetch_url(url: str) -> str:
    """The address to ask for this link: a Scholar profile's list of works gets up to 100 entries, not 20."""
    if not is_scholar(url):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if parts.path.rstrip("/") != "/citations" or not query.get("user") or query.get("view_op", "list_works") != "list_works":
        return url
    if query.get("pagesize") == str(PROFILE_PAGE) and "cstart" in query:
        return url
    query.setdefault("cstart", "0")
    query["pagesize"] = str(PROFILE_PAGE)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
