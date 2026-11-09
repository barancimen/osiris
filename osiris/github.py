"""GitHub lane: name, description and README of each saved repo, plus gists, from the REST API.

Every link into a repo (files, issues, pull requests) shares that repo's document. The token
comes from GITHUB_TOKEN in .env and allows 5,000 calls an hour; without one
GitHub allows 60, so the lane leaves its URLs pending.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import aiohttp

from . import extract
from .store import Store

log = logging.getLogger(__name__)

API = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"
CONCURRENCY = 5
MAX_ATTEMPTS = 3
MAX_GIST_FILE = 2**20
MAX_NOTEBOOK = 64 * 2**20  # nearly all of it is outputs, which are dropped
DATA_URI_RE = re.compile(r"data:[a-z]+/[a-z0-9.+-]+(?:;[a-z0-9=-]+)*,[^\"'\s>)]{256,}", re.I)

MD_IMAGE_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)")
HTML_NOISE_RE = re.compile(r"<!--.*?-->|</?(?:a|img|picture|source|p|div|span|br|hr|center|sub|sup|kbd|details|summary"
                           r"|h[1-6]|b|i|em|strong|table|thead|tbody|tr|td|th|ul|ol|li)\b[^>]*>", re.I | re.S)


def load_token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


def clean_readme(text: str) -> str:
    """Drop badges, images and layout HTML, leaving fenced code alone."""
    text = DATA_URI_RE.sub("", text)
    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.M | re.S)
    for i in range(0, len(parts), 2):
        parts[i] = HTML_NOISE_RE.sub("", MD_IMAGE_RE.sub("", parts[i]))
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def repo_doc(repo: dict, readme: str | None) -> tuple[dict, str]:
    name, description = repo["full_name"], (repo.get("description") or "").strip()
    facts = [("Language", repo.get("language")), ("Stars", repo.get("stargazers_count")),
             ("Topics", ", ".join(repo.get("topics") or [])),
             ("License", (repo.get("license") or {}).get("name")), ("Homepage", repo.get("homepage")),
             ("Fork of", (repo.get("parent") or {}).get("full_name")), ("Archived", "yes" if repo.get("archived") else "")]
    lines = [f"# {name}"] + ([description] if description else [])
    listed = [f"- **{key}:** {value}" for key, value in facts if value not in (None, "")]
    if listed:
        lines.append("\n".join(listed))
    if readme and (cleaned := clean_readme(readme)):
        lines.append("## README\n\n" + cleaned)
    meta = {"title": f"{name}: {description}" if description else name, "author": (repo.get("owner") or {}).get("login"),
            "date": (repo.get("pushed_at") or "")[:10], "site": "github.com", "final_url": repo.get("html_url")}
    return meta, "\n\n".join(lines)


def notebook_text(content: str) -> str:
    """Markdown and code cells of a Jupyter notebook; outputs are mostly embedded images."""
    try:
        cells = json.loads(content)["cells"]
    except (ValueError, KeyError, TypeError):
        return content
    return "\n\n".join("".join(c.get("source") or "") for c in cells if isinstance(c, dict)) or content


def gist_doc(gist: dict, contents: dict[str, str]) -> tuple[dict, str]:
    description = (gist.get("description") or "").strip()
    owner = (gist.get("owner") or {}).get("login")
    lines = [f"# {description or next(iter(contents), gist['id'])}"]
    for name, content in contents.items():
        lines.append(f"## {name}\n\n{content.strip()}")
    meta = {"title": description or next(iter(contents), gist["id"]), "author": owner,
            "date": (gist.get("updated_at") or "")[:10], "site": "gist.github.com", "final_url": gist.get("html_url")}
    return meta, "\n\n".join(lines)


class GitHub:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.paused_until = 0.0
        self.unauthorized = False

    async def get(self, url: str, raw: bool = False) -> tuple[int, str]:
        """(status, body). Waits out rate limits, which only ever holds up this lane."""
        accept = "application/vnd.github.raw" if raw else "application/vnd.github+json"
        status, body = 0, ""
        for attempt in range(MAX_ATTEMPTS):
            while (wait := self.paused_until - time.time()) > 0:
                await asyncio.sleep(min(wait, 60))
            try:
                async with self.session.get(url, headers={"Accept": accept}) as resp:
                    status = resp.status
                    body = await resp.text(errors="replace")
                    headers = resp.headers
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.debug("github: %s: %s", url, e)
                status, body = 0, ""
                await asyncio.sleep(2 ** attempt)
                continue
            if status == 401:
                self.unauthorized = True
                return status, body
            if status in (403, 429) and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"):
                reset = headers.get("X-RateLimit-Reset")
                until = time.time() + int(headers["Retry-After"]) if headers.get("Retry-After") else int(reset or 0) + 1
                self.paused_until = max(self.paused_until, until)
                log.warning("github: rate limited, pausing this lane for %.0f s", self.paused_until - time.time())
                continue
            if status < 500:
                return status, body
            await asyncio.sleep(2 ** attempt)
        return status, body


async def _fetch_repo(gh: GitHub, name: str) -> tuple[int, dict | None, str | None]:
    status, body = await gh.get(f"{API}/repos/{name}")
    if status != 200:
        return status, None, None
    repo = json.loads(body)
    status, readme = await gh.get(f"{API}/repos/{repo['full_name']}/readme", raw=True)
    return 200, repo, readme if status == 200 else None


async def _fetch_gist(gh: GitHub, gist_id: str) -> tuple[int, dict | None, dict[str, str]]:
    status, body = await gh.get(f"{API}/gists/{gist_id}")
    if status != 200:
        return status, None, {}
    gist = json.loads(body)
    contents: dict[str, str] = {}
    for name, f in (gist.get("files") or {}).items():
        content = f.get("content")
        is_notebook = name.lower().endswith(".ipynb")
        limit = MAX_NOTEBOOK if is_notebook else MAX_GIST_FILE
        if f.get("truncated") and f.get("raw_url") and (f.get("size") or 0) <= limit:
            raw_status, raw = await gh.get(f["raw_url"], raw=True)
            content = raw if raw_status == 200 else content
        if content:
            contents[name] = DATA_URI_RE.sub("", notebook_text(content) if is_notebook else content)
    return 200, gist, contents


async def run_github(store: Store) -> None:
    pending = store.pending("github")
    if not pending:
        return
    token = load_token()
    if not token:
        log.warning("github: %s not set; leaving %d documents pending", TOKEN_ENV, len(pending))
        return
    log.info("github: %d repos and gists", len(pending))
    slots = asyncio.Semaphore(CONCURRENCY)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "osiris", "X-GitHub-Api-Version": "2022-11-28"}

    async def one(gh: GitHub, doc_key: str, url: str) -> None:
        async with slots:
            if gh.unauthorized:
                return
            kind, _, name = doc_key.partition(":")
            try:
                if kind == "gist":
                    status, item, contents = await _fetch_gist(gh, name)
                    doc = gist_doc(item, contents) if item else None
                    has_text = bool(contents)
                    source = "github:gist"
                else:
                    status, item, readme = await _fetch_repo(gh, name)
                    doc = repo_doc(item, readme) if item else None
                    has_text = bool(readme)
                    source = "github:api"
            except (ValueError, KeyError, TypeError) as e:  # a response that isn't the JSON documented
                store.record(doc_key, "failed", f"bad_response:{type(e).__name__}")
                return
        if gh.unauthorized:
            return
        if doc is None:
            reason = "not_found" if status == 404 else ("network" if status == 0 else f"http_{status}")
            store.record(doc_key, "failed", reason, http_status=status or None)
            return
        meta, body = doc
        chars = store.write_doc(doc_key, {**meta, "url": url, "source": source}, body)
        ok = has_text and chars >= extract.MIN_CHARS
        store.record(doc_key, "ok" if ok else "thin", None if ok else ("short" if has_text else "no_readme"),
                     chars=chars, http_status=200, final_url=meta.get("final_url"))

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        gh = GitHub(session)
        await asyncio.gather(*(one(gh, key, url) for key, url in pending))
        if gh.unauthorized:
            log.error("github: the token was refused (401); unfinished documents stay pending")
    store.commit()
