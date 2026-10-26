"""Minimal client for Scry's read-only SQL API (https://scry.io), used by the archive lanes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://api.scry.io/v1/scry/query"


class ScryError(Exception):
    pass


def load_key() -> str | None:
    """SCRY_API_KEY from the environment, else ~/.config/scry/env, else the legacy ~/.scry/.env."""
    if os.environ.get("SCRY_API_KEY"):
        return os.environ["SCRY_API_KEY"]
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for path in (xdg / "scry" / "env", Path.home() / ".scry" / ".env"):
        try:
            for line in path.read_text().splitlines():
                key, _, value = line.removeprefix("export ").partition("=")
                if key.strip() == "SCRY_API_KEY" and value.strip():
                    return value.strip().strip("'\"")
        except OSError:
            continue
    return None


class ScryClient:
    def __init__(self, session: aiohttp.ClientSession, key: str, concurrency: int = 4):
        self.session = session
        self.key = key
        self.slots = asyncio.Semaphore(concurrency)

    async def query(self, sql: str, max_seconds: int = 60) -> list[dict]:
        """Rows as dicts. Every statement needs a literal LIMIT (Scry refuses unbounded reads)."""
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "text/plain",
                   "x-scry-max-seconds": str(max_seconds)}
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
                if "rows" in data:
                    names = [c["name"] for c in data["columns"]]
                    return [dict(zip(names, row)) for row in data["rows"]]
                error = data.get("error") or data
                raise ScryError(f"{error.get('code')}: {error.get('message')}")
        raise ScryError("scry unreachable")
