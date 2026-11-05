"""Normalize input URLs and route each one to a lane with a canonical doc key."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit, urlunsplit

from .dedup import YOUTUBE_HOST, dedup_key, youtube_target

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

X_HOSTS = {"x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"}
TWEET_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_]{1,15}|i/web|i)/status(?:es)?/(\d{5,25})(?:/|$)")

ARXIV_HOSTS = {"arxiv.org", "export.arxiv.org"}
# new-style 2401.12345 / 0704.0001, old-style hep-th/9901001 or math.GT/0309136
ARXIV_ID_RE = re.compile(
    r"^/(?:abs|pdf|html)/(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?$"
)

REDDIT_POST_RE = re.compile(r"^/(?:r/[^/]+/)?comments/([a-z0-9]{2,12})(?:/|$)", re.I)
REDDIT_SHORT_RE = re.compile(r"^/([a-z0-9]{2,12})/?$", re.I)  # redd.it/<post id>

SE_HOSTS = {"stackoverflow.com", "superuser.com", "serverfault.com", "askubuntu.com", "mathoverflow.net",
            "stackapps.com"}
SE_QUESTION_RE = re.compile(r"^/(?:questions|q)/(\d+)(?:/|$)")

GITHUB_HOSTS = {"github.com", "gist.github.com", "raw.githubusercontent.com", "gist.githubusercontent.com"}
# *.github.io sites and raw file hosts are ordinary pages and go through the generic lane
GITHUB_REPO_RE = re.compile(r"^/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)")
GIST_RE = re.compile(r"^/(?:[\w-]+/)?([0-9a-f]{20,})(?:/|$)", re.I)
# github.com's own pages, which look like /owner/repo but aren't
GITHUB_RESERVED = {"about", "account", "apps", "codespaces", "collections", "contact", "customer-stories", "dashboard",
                   "enterprise", "events", "explore", "features", "issues", "login", "marketplace", "new", "notifications",
                   "orgs", "organizations", "pricing", "pulls", "search", "security", "sessions", "settings", "signup",
                   "site", "sponsors", "stars", "team", "topics", "trending", "users", "watching"}

# local files osiris can read; anything else (images, /etc/hostname, ...) stays out
LOCAL_READABLE = {"pdf", "html", "htm", "xhtml", "txt", "md", "markdown", "rst", "tex", "org",
                  "docx", "epub", "mobi", "azw", "azw3"}

# Logged-in chat apps: conversations are private and the pages are JS shells.
PRIVATE_APP_HOSTS = {"chatgpt.com", "chat.openai.com", "claude.ai", "grok.com", "gemini.google.com"}

MEDIA_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "bmp", "tif", "tiff", "avif", "heic",
    "mp4", "webm", "mov", "avi", "mkv", "m4v", "mp3", "wav", "ogg", "m4a", "flac", "aac",
    "zip", "tar", "gz", "tgz", "bz2", "xz", "rar", "7z", "exe", "dmg", "iso", "apk", "msi",
    "woff", "woff2", "ttf", "otf",
}


@dataclass(frozen=True)
class Route:
    url: str  # normalized URL, primary key in the manifest
    lane: str  # generic | twitter | arxiv | stackexchange | reddit | github | youtube | localfile | skip
    doc_key: str | None = None  # URLs sharing a doc_key share one document
    reason: str | None = None  # why it was skipped
    # extract_urls.py's notion of "same page" (tracking params, host aliases, ...): a URL
    # whose dedup key is already in the manifest is not queued again from a new list
    dedup_key: str | None = None

    @property
    def doc_id(self) -> str | None:
        return doc_id(self.doc_key) if self.doc_key else None


def doc_id(doc_key: str) -> str:
    return hashlib.sha1(doc_key.encode()).hexdigest()[:16]


def extract_url(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.lower().startswith(("http://", "https://")) and " " not in line:
        return line
    if line.lower().startswith("file:/"):
        return line
    m = URL_RE.search(line)
    return m.group(0) if m else None


def normalize(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return None
    netloc = host
    if port and not (scheme == "http" and port == 80 or scheme == "https" and port == 443):
        netloc = f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _is_local(host: str) -> bool:
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")) or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return not ip.is_global


def route(raw: str) -> Route | None:
    """Return the route for one input URL, or None if the line holds no URL."""
    found = extract_url(raw)
    if found is None:
        return None
    return replace(_route(found), dedup_key=safe_dedup_key(found))


def safe_dedup_key(url: str) -> str:
    try:
        return dedup_key(url)
    except ValueError:  # unparseable URL: only identical strings match
        return url


def _route_file(found: str) -> Route:
    parts = urlsplit(found)
    netloc = "" if parts.netloc.lower() == "localhost" else parts.netloc
    url = urlunsplit(("file", netloc, parts.path, "", ""))
    name = unquote(parts.path).rsplit("/", 1)[-1]
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    if ext not in LOCAL_READABLE:
        return Route(url, "skip", reason="local_other")
    return Route(url, "localfile", doc_key=f"file:{netloc}{unquote(parts.path)}")


def _route_github(url: str, bare: str, path: str) -> Route:
    if bare in ("raw.githubusercontent.com", "gist.githubusercontent.com"):
        return Route(url, "generic", doc_key=url)
    if bare == "gist.github.com":
        m = GIST_RE.match(path)
        return Route(url, "github", doc_key=f"gist:{m.group(1).lower()}") if m else Route(url, "skip", reason="github_account")
    m = GITHUB_REPO_RE.match(path)
    first = path.strip("/").split("/")[0].lower()
    if first in GITHUB_RESERVED:
        return Route(url, "skip", reason="github_other")
    if not m:  # by request: a user or organization page would only add a name and bio
        return Route(url, "skip", reason="github_account")
    return Route(url, "github", doc_key=f"github:{m.group(1)}/{m.group(2)}".lower())


def _route(found: str) -> Route:
    if found.lower().startswith("file:/"):
        return _route_file(found)
    url = normalize(found)
    if url is None:
        return Route(found, "skip", reason="bad_url")

    parts = urlsplit(url)
    host = parts.hostname or ""
    bare = host[4:] if host.startswith("www.") else host
    path = parts.path

    if _is_local(host):
        return Route(url, "skip", reason="local")
    if bare in PRIVATE_APP_HOSTS:
        return Route(url, "skip", reason="private_app")
    if bare in GITHUB_HOSTS:
        return _route_github(url, bare, path)
    if YOUTUBE_HOST.search(host):
        target = youtube_target(url)
        return Route(url, "youtube", doc_key="youtube:" + ":".join(target)) if target else Route(url, "skip", reason="youtube_other")
    if bare in X_HOSTS:
        m = TWEET_PATH_RE.match(path)
        if m:
            return Route(url, "twitter", doc_key=f"tweet:{m.group(1)}")
        return Route(url, "skip", reason="x_non_status")
    if bare in ARXIV_HOSTS:
        m = ARXIV_ID_RE.match(path)
        if m:
            # math.GT/0309136 is an alias of math/0309136, the form the API returns
            arxiv_id = re.sub(r"^([a-z\-]+)\.[A-Z]{2}/", r"\1/", m.group(1))
            return Route(url, "arxiv", doc_key=f"arxiv:{arxiv_id}")
        return Route(url, "skip", reason="arxiv_other")
    if bare == "reddit.com" or bare.endswith(".reddit.com") or bare == "redd.it":
        m = (REDDIT_SHORT_RE if bare == "redd.it" else REDDIT_POST_RE).match(path)
        if m:  # subreddit/user pages and /s/ share links go through the generic lane
            return Route(url, "reddit", doc_key=f"reddit:{m.group(1).lower()}")
    if bare in SE_HOSTS or bare.endswith((".stackexchange.com", ".stackoverflow.com", ".mathoverflow.net")):
        m = SE_QUESTION_RE.match(path)
        if m:  # anything else (tag listings, user pages) goes through the generic lane
            return Route(url, "stackexchange", doc_key=f"se:{bare}:{m.group(1)}")

    ext = path.rsplit("/", 1)[-1].rpartition(".")[2].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if ext in MEDIA_EXTS:
        return Route(url, "skip", reason=f"media:{ext}")
    return Route(url, "generic", doc_key=url)
