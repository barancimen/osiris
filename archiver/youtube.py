"""YouTube lane: video transcripts, and the title and video list of playlists and channels.

YouTube blocks datacenter IPs and throttles bursts from any IP, so this lane is deliberately
slow and isolated: one worker thread of its own, paced requests, and nothing shared with the
other lanes, so a block here can't hold up another document. A blocked item stays pending;
after BLOCK_LIMIT blocks in a row the lane stops and the next run carries on from there.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import extract
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_DELAY = 5.0
BLOCK_WAITS = (300.0, 600.0, 1200.0, 2400.0)  # seconds to wait after each block in a row
BLOCK_LIMIT = len(BLOCK_WAITS) + 1
TIMEOUT = 30
LIST_CAP = 2000  # videos listed for one playlist or channel; a channel can have tens of thousands
ENGLISH = ("en", "en-US", "en-GB", "en-CA", "en-AU")
BLOCK_TEXT = re.compile(r"sign in to confirm|not a bot|too many requests|http error 429|rate.?limit", re.I)
GONE_TEXT = re.compile(r"does not exist|not available|unavailable|private|removed|terminated|http error 404", re.I)
PARAGRAPH = 1000
SOUND_LABEL = re.compile(r"\[[^\]]*\]")
MUSIC_NOTES = re.compile(r"[♪♫♩♬]+")


class Blocked(Exception):
    """YouTube refused this IP. Nothing is known about the item itself."""


def _session():
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
    except (OSError, ValueError):  # title and channel are a nicety: a transcript alone is still worth keeping
        return {}


def _choose(transcripts: list):
    """English first, then the video's own language; human captions ahead of automatic ones."""
    ranked = [t for t in transcripts if t.language_code in ENGLISH and not t.is_generated]
    ranked += [t for t in transcripts if t.language_code in ENGLISH and t.is_generated]
    ranked += [t for t in transcripts if not t.is_generated]
    ranked += [t for t in transcripts if t.is_generated]
    return ranked[0] if ranked else None


def _paragraphs(snippets) -> str:
    # "[Music]" is a sound label, and music notes only decorate the lyrics they surround
    lines = (MUSIC_NOTES.sub(" ", html.unescape(s.text)).replace("\n", " ") for s in snippets)
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
            text = _paragraphs(transcript.fetch())
    except errors.RequestBlocked as e:
        raise Blocked(type(e).__name__) from e
    except errors.YouTubeRequestFailed as e:
        if "429" in str(e):
            raise Blocked("http_429") from e
        return {"status": "failed", "reason": "youtube_request_failed"}
    except requests.RequestException as e:
        raise Blocked(f"network:{type(e).__name__}") from e
    except (errors.TranscriptsDisabled, errors.NoTranscriptFound):
        reason = "no_transcript"
    except (errors.VideoUnavailable, errors.InvalidVideoId):
        return {"status": "failed", "reason": "unavailable"}
    except errors.CouldNotRetrieveTranscript as e:  # age-restricted, unplayable, ...: --retry-failed tries again
        return {"status": "failed", "reason": type(e).__name__}

    info = _oembed(video_id)
    title, channel = info.get("title") or f"YouTube video {video_id}", info.get("author_name")
    head = f"# {title}" + (f"\n\n**Channel:** {channel}" if channel else "")
    kind = "auto" if transcript is not None and transcript.is_generated else "human"
    return {"status": "ok" if text else "thin", "reason": reason,
            "meta": {"title": title, "author": channel, "site": "youtube.com",
                     "source": f"youtube:transcript:{kind}" if text else "youtube:metadata"},
            "body": f"{head}\n\n{text}" if text else head, "has_text": bool(text)}


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
               "playlistend": LIST_CAP, "socket_timeout": TIMEOUT, "logger": logging.getLogger("yt_dlp.quiet")}
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


async def run_youtube(store: Store, delay: float = DEFAULT_DELAY) -> None:
    pending = store.pending("youtube")
    if not pending:
        return
    log.info("youtube: %d videos, playlists and channels, about one every %.0f s", len(pending), delay)
    loop = asyncio.get_running_loop()
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="youtube")
    blocks = done = 0
    try:
        for i, (doc_key, url) in enumerate(pending):
            while True:
                try:
                    res = await loop.run_in_executor(worker, fetch, doc_key)
                except Blocked as e:
                    blocks += 1
                    if blocks >= BLOCK_LIMIT:
                        log.warning("youtube: blocked %d times in a row (%s); stopping with %d left pending. "
                                    "Re-run later, from a home connection if this is a server",
                                    blocks, e, len(pending) - i)
                        return
                    wait = BLOCK_WAITS[blocks - 1]
                    log.warning("youtube: blocked (%s); pausing this lane for %.0f min. Other lanes carry on",
                                e, wait / 60)
                    await asyncio.sleep(wait)
                    continue
                break
            blocks = 0
            done += 1
            if res["status"] == "failed":
                store.record(doc_key, "failed", res["reason"])
            else:
                meta = {**res["meta"], "url": url}
                chars = store.write_doc(doc_key, meta, res["body"])
                short = res["has_text"] and chars < extract.MIN_CHARS
                status = "thin" if short or res["status"] == "thin" else "ok"
                store.record(doc_key, status, res["reason"] or ("short" if short else None), chars=chars)
            if done % 25 == 0:
                log.info("youtube: %d of %d", done, len(pending))
            if i + 1 < len(pending):
                await asyncio.sleep(delay * random.uniform(0.7, 1.3))
    finally:
        worker.shutdown(wait=False, cancel_futures=True)
        store.commit()
