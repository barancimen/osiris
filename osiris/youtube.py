"""YouTube lane: video transcripts, and the title and video list of playlists and channels.

YouTube blocks datacenter IPs and throttles bursts from any IP, so this lane is deliberately
slow and isolated: one worker thread of its own, paced requests, and nothing shared with the
other lanes, so a block here can't hold up another document. A blocked item stays pending;
after BLOCK_LIMIT blocks in a row the lane stops and the next run carries on from there.

A block is remembered in the archive folder, so restarting doesn't knock on YouTube's door again
before the pause is over, and for a day afterwards the lane goes at half speed.
"""

from __future__ import annotations

import asyncio
import functools
import html
import json
import logging
import random
import re
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp

from . import extract
from .scry import ScryClient, ScryError, load_key, sql_list
from .store import Store

log = logging.getLogger(__name__)

# seconds between videos, at random within this range. A video takes about 2 s more and four requests, so
# this is about 240 videos an hour, under the roughly 300 yt-dlp's documentation gives for a visitor not signed in
DEFAULT_DELAY = (10.0, 16.0)
BLOCK_WAIT_MINUTES = 30.0  # after a block, doubling with each block in a row
BLOCK_LIMIT = 5
BLOCK_MEMORY_HOURS = 24.0  # how long a block keeps the lane at BLOCK_SLOWDOWN; also the longest pause
BLOCK_SLOWDOWN = 2.0
BLOCK_CLEAR_AFTER = 25  # videos in a row that must go through before a block stops counting towards the next pause
BLOCK_FILE = "youtube_block.json"
NETWORK_WAIT_MINUTES = 5.0  # when YouTube can't be reached at all, doubling with each failure in a row
LIST_PAGE_DELAY = 3.0  # seconds between the pages of one playlist or channel: a long one takes dozens
TIMEOUT = 30
LIST_CAP = 2000  # videos listed for one playlist or channel; a channel can have tens of thousands
ENGLISH = ("en", "en-US", "en-GB", "en-CA", "en-AU")
BLOCK_TEXT = re.compile(r"sign in to confirm|not a bot|too many requests|http error 429|rate.?limit", re.I)
GONE_TEXT = re.compile(r"does not exist|not available|unavailable|private|removed|terminated|http error 404", re.I)
PARAGRAPH = 1000
SCRY_LOOKUP_BATCH = 2000  # a lookup returns only sizes, so it's cheap to ask about many at once
# of transcript in one response. Scry counts YouTube data 20 times over and its price rises as you take more,
# so this keeps one query under scry.MAX_EXPOSURE_NANODOLLARS even after a run has spent several dollars
SCRY_BATCH_CHARS = 500_000
# Some human-made captions were stored with their line breaks removed, which joined the words on either
# side of each break. Automatic captions stored the same way are intact.
SCRY_DAMAGED = "(is_auto_caption = 0 AND position(payload, char(10)) = 0 AND length(event_starts_ms) > 1)"
SOUND_LABEL = re.compile(r"\[[^\]]*\]")
MUSIC_NOTES = re.compile(r"[♪♫♩♬]+")


def parse_delay(text: str) -> tuple[float, float]:
    """`3.5-6.5` waits between 3.5 and 6.5 seconds; one number means that, give or take 30%."""
    low, dash, high = text.partition("-")
    if not dash:
        return float(low) * 0.7, float(low) * 1.3
    if float(low) > float(high):
        raise ValueError(text)
    return float(low), float(high)


class Blocked(Exception):
    """YouTube refused this IP. Nothing is known about the item itself."""


class OneRefused(Blocked):
    """YouTube's bot check refused this video, which can be about the video alone: others go through
    a second later. It only counts as a block if the next video is refused too."""


class Offline(Exception):
    """YouTube couldn't be reached, which says nothing about whether it would have refused us."""


def _read_block(out: Path) -> tuple[float, int]:
    """When YouTube last blocked this archive and the blocks in a row by then; (0, 0) once that's old news."""
    try:
        state = json.loads((out / BLOCK_FILE).read_text(encoding="utf-8"))
        at, blocks = float(state["at"]), int(state["blocks"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0, 0
    return (at, blocks) if time.time() - at < BLOCK_MEMORY_HOURS * 3600 else (0.0, 0)


def _write_block(out: Path, at: float, blocks: int) -> None:
    (out / BLOCK_FILE).write_text(json.dumps({"at": at, "blocks": blocks}), encoding="utf-8")


def _pause(blocks: int) -> float:
    """Seconds to leave YouTube alone after this many blocks in a row."""
    return min(BLOCK_WAIT_MINUTES * 60 * 2 ** (blocks - 1), BLOCK_MEMORY_HOURS * 3600)


@functools.lru_cache(maxsize=1)
def _session():
    """One session for the whole lane, as a browser would have: a new one per video would fetch each
    watch page twice where YouTube first asks for cookie consent."""
    import requests

    class TimeoutSession(requests.Session):
        def request(self, *args, **kwargs):  # the library sets no timeout; a stalled call would hold the lane
            kwargs.setdefault("timeout", TIMEOUT)
            return super().request(*args, **kwargs)

    return TimeoutSession()


def _oembed(video_id: str) -> dict:
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://youtu.be/{video_id}", safe="")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:  # the first sign of a block can be here
            raise Blocked("http_429") from e
        return {}
    except (OSError, ValueError):  # title and channel are a nicety: a transcript alone is still worth keeping
        return {}


def _choose(transcripts: list):
    """English first, then the video's own language; human captions ahead of automatic ones."""
    ranked = [t for t in transcripts if t.language_code in ENGLISH and not t.is_generated]
    ranked += [t for t in transcripts if t.language_code in ENGLISH and t.is_generated]
    ranked += [t for t in transcripts if not t.is_generated]
    ranked += [t for t in transcripts if t.is_generated]
    return ranked[0] if ranked else None


def _paragraphs(captions) -> str:
    # "[Music]" is a sound label, and music notes only decorate the lyrics they surround
    lines = (MUSIC_NOTES.sub(" ", html.unescape(text)).replace("\n", " ") for text in captions)
    words = " ".join(line for line in lines if not SOUND_LABEL.fullmatch(line.strip()))
    return "\n\n".join(textwrap.wrap(" ".join(words.split()), PARAGRAPH, break_long_words=False, break_on_hyphens=False))


def fetch_video(video_id: str) -> dict:
    """Runs on the lane's worker thread. {status, reason, meta, body}, or raises Blocked."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as errors
    import requests

    api = YouTubeTranscriptApi(http_client=_session())
    transcript, text, reason = None, "", None
    try:
        transcript = _choose(list(api.list(video_id)))
        if transcript is None:
            reason = "no_transcript"
        else:
            text = _paragraphs(s.text for s in transcript.fetch())
    except errors.IpBlocked as e:  # a 429 or a captcha page
        raise Blocked(type(e).__name__) from e
    except errors.RequestBlocked as e:
        raise OneRefused(type(e).__name__) from e
    except errors.YouTubeRequestFailed as e:
        if "429" in str(e):
            raise Blocked("http_429") from e
        return {"status": "failed", "reason": "youtube_request_failed"}
    except requests.RequestException as e:
        raise Offline(type(e).__name__) from e
    except (errors.TranscriptsDisabled, errors.NoTranscriptFound):
        reason = "no_transcript"
    except (errors.VideoUnavailable, errors.InvalidVideoId):
        return {"status": "failed", "reason": "unavailable"}
    except errors.CouldNotRetrieveTranscript as e:  # age-restricted, unplayable, ...: --retry-failed tries again
        return {"status": "failed", "reason": type(e).__name__}

    info = _oembed(video_id)
    kind = "auto" if transcript is not None and transcript.is_generated else "human"
    return _video(video_id, info.get("title"), info.get("author_name"), text, kind, reason)


def _video(video_id: str, title: str | None, channel: str | None, text: str, kind: str, reason: str | None = None,
           via: str = "") -> dict:
    title = title or f"YouTube video {video_id}"
    head = f"# {title}" + (f"\n\n**Channel:** {channel}" if channel else "")
    return {"status": "ok" if text else "thin", "reason": reason,
            "meta": {"title": title, "author": channel, "site": "youtube.com",
                     "source": f"youtube:transcript:{kind}{via}" if text else "youtube:metadata"},
            "body": f"{head}\n\n{text}" if text else head, "has_text": bool(text)}


def _save(store: Store, doc_key: str, url: str, res: dict) -> None:
    if res["status"] == "failed":
        store.record(doc_key, "failed", res["reason"])
        return
    chars = store.write_doc(doc_key, {**res["meta"], "url": url}, res["body"])
    short = res["has_text"] and chars < extract.MIN_CHARS
    status = "thin" if short or res["status"] == "thin" else "ok"
    store.record(doc_key, status, res["reason"] or ("short" if short else None), chars=chars)


def _best_track(columns: str, videos: list[str]) -> str:
    """A query for the track to use for each video, in the order _choose prefers them."""
    return (f"SELECT video_id, {columns} FROM youtube.transcripts WHERE video_id IN ({sql_list(videos)})"
            f" AND NOT {SCRY_DAMAGED} ORDER BY video_id, (lang = 'en' OR startsWith(lang, 'en-')) DESC,"
            f" is_auto_caption ASC, word_count DESC LIMIT 1 BY video_id LIMIT {len(videos)}")


async def _scry_titles(client: ScryClient, videos: list[str]) -> dict[str, tuple[str, str | None]]:
    """Title and channel, from what Scry has seen lately or else its census of YouTube in 2021."""
    rows = await client.query(
        "SELECT video_id, argMax(title, observed_hour) AS title, argMax(channel_name, observed_hour) AS channel"
        f" FROM youtube.videos_live WHERE video_id IN ({sql_list(videos)}) AND title IS NOT NULL"
        f" GROUP BY video_id LIMIT {len(videos)}", max_seconds=90)
    found = {r["video_id"]: (r["title"], r["channel"]) for r in rows if r["title"]}
    missing = [v for v in videos if v not in found]
    if missing:
        rows = await client.query(
            f"SELECT video_id, any(title) AS title, any(uploader) AS channel FROM youtube.videos"
            f" WHERE video_id IN ({sql_list(missing)}) GROUP BY video_id LIMIT {len(missing)}", max_seconds=90)
        found.update({r["video_id"]: (r["title"], r["channel"]) for r in rows if r["title"]})
    return found


def _text_batches(sizes: dict[str, int]) -> list[list[str]]:
    """Videos grouped so that no response is too big. A transcript too big for a batch of its own is left out."""
    batches: list[list[str]] = []
    chars = 0
    for video, size in sizes.items():
        if size > SCRY_BATCH_CHARS:
            continue
        if not batches or chars + size > SCRY_BATCH_CHARS:
            batches.append([])
            chars = 0
        batches[-1].append(video)
        chars += size
    return batches


def _scry_text(payload: str) -> str:
    """Normally one caption per line; automatic captions stored on one line keep their sound labels inside it."""
    return _paragraphs(payload.split("\n") if "\n" in payload else SOUND_LABEL.split(payload))


async def fill_from_scry(store: Store, pending: list[tuple[str, str]], max_dollars: float) -> None:
    """Transcripts from Scry, so that those videos never touch YouTube. Whatever isn't saved here, because
    Scry doesn't have it, Scry failed or the spending limit was reached, comes from YouTube as usual."""
    key = load_key()
    if not key:
        log.warning("youtube: no Scry key, so every video comes from YouTube")
        return
    videos = {doc_key.split(":", 2)[2]: (doc_key, url) for doc_key, url in pending if doc_key.startswith("youtube:video:")}
    wanted = list(videos)
    stop_at = ScryClient.spent_nanodollars + int(max_dollars * 1e9)
    saved = 0

    def out_of_money() -> bool:
        if ScryClient.spent_nanodollars < stop_at:
            return False
        log.warning("youtube: reached the $%.2f limit for Scry; the rest come from YouTube", max_dollars)
        return True

    async def take_all(client: ScryClient) -> None:
        nonlocal saved
        for i in range(0, len(wanted), SCRY_LOOKUP_BATCH):
            if out_of_money():
                return
            batch = wanted[i : i + SCRY_LOOKUP_BATCH]
            rows = await client.query(_best_track("length(payload) AS chars", batch), max_seconds=120)
            sizes = {r["video_id"]: r["chars"] for r in rows if r["chars"]}
            titles = await _scry_titles(client, list(sizes)) if sizes else {}
            for part in _text_batches(sizes):
                if out_of_money():
                    return
                for row in await client.query(_best_track("is_auto_caption AS auto, payload", part), max_seconds=120):
                    video = row["video_id"]
                    text = _scry_text(row["payload"])
                    if text:
                        title, channel = titles.get(video, (None, None))
                        _save(store, *videos[video], _video(video, title, channel, text, "auto" if row["auto"] else "human", via=":scry"))
                        saved += 1
                store.commit()
                log.info("youtube: %d transcripts read from Scry so far", saved)

    async with aiohttp.ClientSession() as session:
        try:
            await take_all(ScryClient(session, key))
        except ScryError as e:
            log.warning("youtube: Scry failed (%s); the rest come from YouTube", e)
    log.info("youtube: %d of %d videos came from Scry; the rest come from YouTube", saved, len(wanted))


def list_urls(doc_key: str) -> list[str]:
    """Where to read this list; a legacy channel name may be either a /c/ or a /user/ page."""
    _, kind, name = doc_key.split(":", 2)
    if kind == "playlist":
        return [f"https://www.youtube.com/playlist?list={name}"]
    if name.startswith("@"):
        return [f"https://www.youtube.com/{name}/videos"]
    if name.startswith("UC"):
        return [f"https://www.youtube.com/channel/{name}/videos"]
    return [f"https://www.youtube.com/c/{name}/videos", f"https://www.youtube.com/user/{name}/videos"]


def fetch_list(doc_key: str) -> dict:
    """A playlist's or channel's title, description and video titles, without their transcripts."""
    import yt_dlp

    options = {"extract_flat": True, "skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True,
               "playlistend": LIST_CAP, "socket_timeout": TIMEOUT,
               "sleep_interval_requests": LIST_PAGE_DELAY, "logger": logging.getLogger("yt_dlp.quiet")}
    logging.getLogger("yt_dlp.quiet").setLevel(logging.CRITICAL)
    info = message = None
    for url in list_urls(doc_key):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except yt_dlp.utils.DownloadError as e:
            message = str(e)
            if BLOCK_TEXT.search(message):
                raise Blocked(message[:80]) from e
    if info is None:
        return {"status": "failed", "reason": "unavailable" if GONE_TEXT.search(message or "") else "list_error"}
    title = re.sub(r"\s*-\s*Videos$", "", info.get("title") or doc_key)
    channel = info.get("uploader") or info.get("channel")
    videos = [e for e in (info.get("entries") or []) if e and e.get("title")]
    total = info.get("playlist_count") or len(videos)
    lines = [f"# {title}"]
    if channel and channel != title:
        lines.append(f"**Channel:** {channel}")
    if (info.get("description") or "").strip():
        lines.append(info["description"].strip())
    if videos:
        more = f" (first {len(videos)} of {total})" if total > len(videos) else ""
        lines.append(f"## Videos{more}\n\n" + "\n".join(f"- {v['title']}" for v in videos))
    is_channel = ":channel:" in doc_key
    return {"status": "ok" if videos else "thin", "reason": None if videos else "no_videos",
            "meta": {"title": title, "author": channel, "site": "youtube.com",
                     "source": "youtube:channel" if is_channel else "youtube:playlist"},
            "body": "\n\n".join(lines), "has_text": bool(videos)}


def fetch(doc_key: str) -> dict:
    _, kind, name = doc_key.split(":", 2)
    return fetch_video(name) if kind == "video" else fetch_list(doc_key)


async def run_youtube(store: Store, delay: tuple[float, float] = DEFAULT_DELAY, scry_dollars: float | None = None) -> None:
    pending = store.pending("youtube")
    if pending and scry_dollars is not None:  # before any pause: it doesn't touch YouTube
        await fill_from_scry(store, pending, scry_dollars)
        pending = store.pending("youtube")
    if not pending:
        return
    log.info("youtube: %d videos, playlists and channels, one every %g to %g s", len(pending), *delay)
    last_block, blocks = _read_block(store.out)
    left = last_block + _pause(blocks) - time.time() if blocks else 0
    if left > 0:
        log.warning("youtube: blocked earlier and still pausing; trying again in %.0f min. Other lanes carry on. "
                    "Stopping is safe, and setting the pause to 0 skips this wait", left / 60)
        await asyncio.sleep(left)
    loop = asyncio.get_running_loop()
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="youtube")
    done = fine = 0
    suspect = None  # the video the bot check has just refused, until the next one shows if that was only about it

    async def pace() -> None:
        slowed = time.time() - last_block < BLOCK_MEMORY_HOURS * 3600
        await asyncio.sleep(random.uniform(*delay) * (BLOCK_SLOWDOWN if slowed else 1))

    try:
        for i, (doc_key, url) in enumerate(pending):
            unreachable = 0
            res = None
            while True:
                try:
                    res = await loop.run_in_executor(worker, fetch, doc_key)
                except Offline as e:
                    unreachable += 1
                    if unreachable >= BLOCK_LIMIT:
                        log.warning("youtube: can't reach YouTube (%s); stopping with %d left pending", e, len(pending) - i)
                        return
                    wait = NETWORK_WAIT_MINUTES * 60 * 2 ** (unreachable - 1)
                    log.warning("youtube: can't reach YouTube (%s); trying again in %.0f min", e, wait / 60)
                    await asyncio.sleep(wait)
                    continue
                except Blocked as e:
                    if isinstance(e, OneRefused) and suspect is None:
                        suspect = doc_key
                        break
                    suspect = None  # it's the connection, so that video stays pending with the rest
                    blocks += 1
                    fine = 0
                    last_block = time.time()
                    _write_block(store.out, last_block, blocks)
                    if blocks >= BLOCK_LIMIT:
                        log.warning("youtube: blocked %d times in a row (%s); stopping with %d left pending. "
                                    "Re-run later, from a home connection if this is a server",
                                    blocks, e, len(pending) - i)
                        return
                    wait = _pause(blocks)
                    log.warning("youtube: blocked (%s); pausing this lane for %.0f min, then going at half speed. "
                                "Other lanes carry on", e, wait / 60)
                    await asyncio.sleep(wait)
                    continue
                break
            if res is None:
                await pace()
                continue
            if suspect:
                log.info("youtube: the bot check refused %s while other videos go through; "
                         "marked as failed, and retrying failed items tries it again", suspect)
                store.record(suspect, "failed", "bot_check")
                suspect = None
            fine += 1
            if blocks and fine >= BLOCK_CLEAR_AFTER:
                blocks = 0
                _write_block(store.out, last_block, blocks)
            done += 1
            _save(store, doc_key, url, res)
            if done % 25 == 0:
                log.info("youtube: %d of %d", done, len(pending))
            if i + 1 < len(pending):
                await pace()
    finally:
        worker.shutdown(wait=False, cancel_futures=True)
        store.commit()
