"""Minimal client for Scry's read-only SQL API (https://scry.io), used by the archive lanes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Iterable

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://api.scry.io/v1/scry/query"
TOOL_URL = "https://api.scry.io/mcp"  # Scry's tools that aren't queries, called as its agents call them
# Most a single query may hold against the wallet. Uncapped, Scry reserves the worst case at
# its 50 MB response cap (~$5 once the free egress band is used up), so a modest wallet admits
# one query at a time. Our largest responses (10k-comment threads) stay well under this.
MAX_EXPOSURE_NANODOLLARS = 500_000_000  # $0.50


class ScryError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code  # Scry's own name for what went wrong, such as query_resource_limit_exceeded


def sql_list(values: Iterable[str]) -> str:
    """Values for a SQL IN (...)."""
    return ",".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


def load_key() -> str | None:
    return os.environ.get("SCRY_API_KEY") or None


class ScryClient:
    spent_nanodollars = 0  # across all clients in this process, reported at the end of a run

    def __init__(self, session: aiohttp.ClientSession, key: str, concurrency: int = 4):
        self.session = session
        self.key = key
        self.slots = asyncio.Semaphore(concurrency)

    async def query(self, sql: str, max_seconds: int = 60) -> list[dict]:
        """Rows as dicts. Every statement needs a literal LIMIT (Scry refuses unbounded reads)."""
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "text/plain",
                   "x-scry-max-seconds": str(max_seconds), "x-scry-max-exposure": str(MAX_EXPOSURE_NANODOLLARS)}
        async with self.slots:
            for attempt in range(1, 4):
                try:
                    async with self.session.post(API_URL, data=sql, headers=headers,
                                                 timeout=aiohttp.ClientTimeout(total=max_seconds + 60)) as resp:
                        # long queries stream keepalive whitespace before the JSON body
                        data = json.loads((await resp.text()).strip() or "{}")
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    log.warning("scry: %s (try %d)", e, attempt)
                    await asyncio.sleep(2**attempt)
                    continue
                ScryClient.spent_nanodollars += data.get("spend_nanodollars") or 0
                if "rows" in data:
                    names = [c["name"] for c in data["columns"]]
                    return [dict(zip(names, row)) for row in data["rows"]]
                error = data.get("error") or data
                raise ScryError(f"{error.get('code')}: {error.get('message')}", error.get("code"))
        raise ScryError("scry unreachable")

    async def tool(self, name: str, arguments: dict) -> dict:
        """What one of Scry's tools answers, for those that aren't queries, such as twitter_watch."""
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json, text/event-stream"}
        try:
            async with self.session.post(TOOL_URL, json=call, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ScryError(f"scry unreachable: {e}") from None
        for line in text.splitlines():  # the answer is plain JSON, or one event of a stream
            line = line.removeprefix("data:").strip()
            if not line.startswith("{"):
                continue
            try:
                answer = json.loads(line)
                if "error" in answer:
                    raise ScryError(f"{name}: {answer['error'].get('message')}")
                said = answer["result"]["content"][0]["text"]
                if answer["result"].get("isError"):
                    raise ScryError(f"{name}: {said[:300]}")
                found = json.loads(said)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                break
            if isinstance(found, dict):
                return found
            break
        raise ScryError(f"{name}: Scry's answer wasn't understood")
