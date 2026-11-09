"""`osiris ui`: the whole process from one local web page.

It listens on this machine only and refuses requests from other websites, because the page can
read your files and start jobs. Work runs as ordinary `osiris` subprocesses,
one job at a time, so the page never holds up a run and a run never depends on the page's state.
Originals are only served for files the manifest already knows about, never for an arbitrary path.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

from . import cleanup, cookies, fixes, review, search, tuning
from .github import load_token
from .localfile import candidate_paths
from .route import FORUM_LANES, LOCAL_READABLE
from .scry import load_key
from .store import TMP_DIR, Store, fill_missing_titles, md_path, read_doc_file
from .arxiv import ABSTRACT_ONLY_CHARS, BATCH as ARXIV_BATCH, DELAY as ARXIV_DELAY, FULLTEXT, FULLTEXT_DELAY
from .youtube import DEFAULT_DELAY

log = logging.getLogger(__name__)
REPO_DIR = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).with_name("ui.html")
LANES = ("twitter", "arxiv", "stackexchange", "reddit", "hackernews", "lesswrong", "github", "localfile", "generic", "youtube")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
DOC_ID = re.compile(r"^[0-9a-f]{16}$")
BILLED_KEY = "TWITTERAPI_IO_KEY"
FETCHING_JOBS = ("Archive", "Fixes")  # jobs whose failures the page reports as this run's
MAX_BODY = 400_000  # characters of a document sent to the page; the raw link has the rest
PAGE_SIZE = 100
SCAN_LIMIT = 100  # matches a text scan stops at, when there's no search index
INDEX_EVERY = 300  # seconds between search-index refreshes while a job runs
FAILURES_EVERY = 2  # seconds between looks at whether the manifest has changed
COOKIE_FILE_TYPES = (".txt", ".json", ".cookies")
HOSTNAME = re.compile(r"^[a-z0-9.-]+$")
HOST_PATTERN = re.compile(r"[a-z0-9.*?-]+")
ADDRESS_EVERY = 20  # log lines in the terminal between reminders of the page's address
STAMPED = re.compile(r"\d\d:\d\d:\d\d ")  # how `osiris` log lines start, so the page doesn't add a second time
TEXT_TYPES = {".txt", ".md", ".markdown", ".rst", ".tex", ".org"}
HTML_TYPES = {".html", ".htm", ".xhtml"}
# run options that are numbers: (name, flag, kind, smallest allowed)
NUMBER_FLAGS = (("concurrency", "--concurrency", int, 1), ("per_host", "--per-host", int, 1),
                ("worker_mem_mb", "--worker-mem-mb", int, 0), ("render_concurrency", "--render-concurrency", int, 1),
                ("challenge_wait", "--challenge-wait", float, 0))
BUILT_IN_HOST_LIMIT = "scholar.google.*"  # `osiris run` always has a rule for this one, so it's edited rather than removed
DEFAULT_CONFIG = {
    "backups": [], "local": [], "saved": [], "last_run": None,  # when the latest archive run started and ended
    "options": {"lanes": list(LANES), "forum_lanes_offered": True, "ocr": True, "ocr_lang": "eng", "refresh": "", "retry_failed": False,
                "review_after": True, "cookies": "", "user_agent": "", "user_agent_edit": False, "render": False,
                "chrome_profile": "", "keep_raw_pdfs": False, "arxiv_fulltext": True, "workers": "",
                "impersonate": True, "doi": True, "wayback": True, "refetch_comments": False, "refetch_stackexchange": False,
                "refetch_comments_days": 30, "refetch_arxiv_pdfs": False, "pdf_at_once": "",
                "scry_arxiv": False, "scry_arxiv_dollars": 5, "scry_youtube": False, "scry_youtube_dollars": 1,
                "scry_crawl": False, "scry_crawl_dollars": 1, "log_font_px": 10,
                "contact": "", "youtube_pace_updated": True, "youtube_delay_min": DEFAULT_DELAY[0], "youtube_delay_max": DEFAULT_DELAY[1],
                "scry_twitter": True, "billed_twitter": False, "advanced_open": True, "picker_dirs": {},
                "host_limits": [{"pattern": BUILT_IN_HOST_LIMIT, "at_once": 1, "seconds": 8}],
                **tuning.FLAG_DEFAULTS, **{knob.option: knob.default for knob in tuning.KNOBS},
                "retry_hosts": []},  # sites whose failed URLs the next archive run tries again
}


class Job:
    """One job at a time: a named series of subprocess steps with their output kept for the page."""

    def __init__(self, out: Path):
        self.out = out
        self.task: asyncio.Task | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.name = self.step = ""
        self.lines: deque[str] = deque(maxlen=3000)
        self.count = 0  # lines ever written, so the page only asks for what's new
        self.state = "idle"  # idle | running | stopping | done | stopped | failed
        self.started = self.finished = None
        self.env_extra: dict[str, str | None] = {}
        self.plan: dict | None = None  # set while fetching: what the time left is worked out from
        self.on_change = lambda: None  # a job started or ended
        self.on_line = lambda line: None  # a line was written

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def log(self, line: str) -> None:
        line = line.rstrip()
        line = line if not line or STAMPED.match(line) else f"{time.strftime('%H:%M:%S')} {line}"
        self.lines.append(line)
        self.count += 1
        self.on_line(line)

    def start(self, name: str, flow, env: dict[str, str | None] | None = None) -> None:
        self.name, self.step, self.state = name, "", "running"
        self.plan = None
        self.lines.clear()
        self.count = 0
        self.started, self.finished = time.time(), None
        self.env_extra = env or {}
        self.task = asyncio.create_task(self._main(flow))
        self.on_change()

    async def _main(self, flow) -> None:
        try:
            await flow(self)
            self.state = "stopped" if self.state == "stopping" else "done"
        except _StepFailed as e:
            self.state = "stopped" if self.state == "stopping" else "failed"
            self.log(str(e))
        except Exception as e:  # shown on the page: there's nowhere else to read it
            self.state = "failed"
            self.log(f"{type(e).__name__}: {e}")
        finally:
            self.finished = time.time()
            self.proc = None
            self.on_change()

    async def run(self, label: str, argv: list[str], check: bool = True) -> list[str]:
        """Run one step and return its output. A stop request ends the series after this step."""
        if self.state == "stopping":
            raise _StepFailed("stopped")
        self.step = label
        self.log(f"--- {label}")
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        for key, value in self.env_extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        # its own session, so that Ctrl-C on the page's server is a decision about the job, not a side effect
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=REPO_DIR, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=2**20,
        )
        output = []
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            output.append(line)
            self.log(line)
        code = await self.proc.wait()
        if self.state == "stopping":
            raise _StepFailed("stopped: progress is saved, and starting again resumes")
        if check and code:
            raise _StepFailed(f"{label} failed with exit code {code}")
        return output

    def osiris(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "osiris", *args, "--out", str(self.out)]

    def stop(self) -> None:
        proc = self.proc
        if not self.active:
            return
        force = self.state == "stopping"
        self.state = "stopping"
        if proc is None or proc.returncode is not None:
            return
        try:
            if force:  # asked twice: the whole group, at once
                os.killpg(proc.pid, signal.SIGKILL)
                self.log("--- stopping now")
            else:
                proc.send_signal(signal.SIGINT)
                self.log("--- stopping")
        except ProcessLookupError:
            pass


class _StepFailed(Exception):
    pass


class Index:
    """Keeps search.sqlite up to date in the background: at startup, when a job ends, and every few
    minutes while one runs, so documents become searchable soon after they're archived."""

    def __init__(self, out: Path):
        self.out = out
        self.updating = False
        self.done = self.total = 0
        self.documents = 0
        self.error: str | None = None
        self.wanted = asyncio.Event()
        self.stopping = False

    def _progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total

    async def keep(self, job: Job) -> None:
        while not self.stopping:
            self.wanted.clear()
            self.updating, self.done = True, 0
            try:
                result = await asyncio.to_thread(search.refresh, self.out, self._progress, lambda: self.stopping)
                self.documents, self.error = result["documents"], None
            except Exception as e:  # shown on the page, and tried again next time
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.updating = False
            try:
                await asyncio.wait_for(self.wanted.wait(), INDEX_EVERY if job.active else None)
            except asyncio.TimeoutError:
                pass

    def status(self) -> dict:
        return {"updating": self.updating, "done": self.done, "total": self.total, "documents": self.documents,
                "error": self.error}


class Failures:
    """What failed and why, kept ready in memory so the page never waits for it. A background task reads
    the manifest again whenever it has changed, which during a run is about once a second."""

    def __init__(self, out: Path):
        self.out = out
        last = load_config(out)["last_run"] or {}
        self.run: tuple[float, float | None] | None = (last["started"], last.get("finished")) if last.get("started") else None
        self.version = 0  # differs whenever anything below does
        self.summary: dict | None = None
        self.by_reason: dict[str, list[dict]] = {}
        self._seen = None

    def note_run(self, started: float, finished: float | None) -> None:
        self.run = (started, finished)
        config = load_config(self.out)
        config["last_run"] = {"started": started, "finished": finished}
        _save_config(self.out, config)

    def _signature(self) -> tuple:
        stamps = []
        for name in ("manifest.sqlite", "manifest.sqlite-wal"):
            try:
                found = os.stat(self.out / name)
                stamps.append((found.st_mtime_ns, found.st_size))
            except OSError:
                stamps.append(None)
        return (*stamps, self.run)

    def read(self) -> None:
        run = self.run
        first, last = (_iso(run[0]), _iso(run[1]) if run[1] else "9999") if run else ("", "")  # "9999": still going
        finished_sql = "SELECT COUNT(DISTINCT doc_id) FROM urls WHERE status IN ('ok', 'thin', 'failed') AND lane != 'skip'"
        db = read_manifest(self.out)
        try:
            rows = db.execute("SELECT doc_id, url, lane, reason, updated_at FROM urls WHERE status = 'failed' AND lane != 'skip'"
                              " AND doc_id IS NOT NULL ORDER BY updated_at DESC, rowid DESC").fetchall()
            finished = db.execute(finished_sql).fetchone()[0]
            run_finished = db.execute(finished_sql + " AND updated_at >= ? AND updated_at <= ?", (first, last)).fetchone()[0] if run else 0
        finally:
            db.close()
        by_reason: dict[str, list[dict]] = {}
        seen = set()
        for did, url, lane, reason, at in rows:
            if did not in seen:  # several links to one document are one failure
                seen.add(did)
                reason = reason or "unknown"
                by_reason.setdefault(reason, []).append({
                    "id": did, "url": url, "lane": lane, "host": _host(url), "reason": reason, "at": at,
                    "in_run": bool(run and at and first <= at <= last)})
        reasons = [{"reason": reason, "count": len(items), "run": sum(it["in_run"] for it in items),
                    "hosts": Counter(it["host"] for it in items).most_common(5)}
                   for reason, items in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
        summary = {"total": len(seen), "finished": finished, "reasons": reasons,
                   "run": {"started": run[0], "finished": run[1], "done": run_finished,
                           "failed": sum(r["run"] for r in reasons)} if run else None}
        if summary != self.summary or by_reason != self.by_reason:
            self.summary, self.by_reason = summary, by_reason
            self.version = time.time_ns()  # never repeats, even across restarts of this page's server

    async def keep(self) -> None:
        while True:
            signature = self._signature()
            if signature != self._seen:
                try:
                    await asyncio.to_thread(self.read)
                    self._seen = signature
                except sqlite3.Error as e:  # a busy moment: the next look tries again
                    log.debug("failures: %s", e)
            await asyncio.sleep(FAILURES_EVERY)

    def page(self, reasons: list[str], run_only: bool, host: str, offset: int, limit: int) -> dict:
        """Failed documents, reason by reason in the order asked for, newest first within each."""
        by_reason = self.by_reason  # one read: a refresh swaps in a whole new one
        items = [it for reason in (reasons or by_reason) for it in by_reason.get(reason, ()) if it["in_run"] or not run_only]
        hosts = Counter(it["host"] for it in items)
        if host:
            items = [it for it in items if it["host"] == host]
        return {"version": self.version, "total": len(items), "items": items[offset:offset + limit],
                "counts": Counter(it["reason"] for it in items), "hosts": hosts.most_common(300),
                "run": sum(it["in_run"] for reason in (reasons or by_reason) for it in by_reason.get(reason, ()))}


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "local file"
    except ValueError:  # a malformed address in a bookmark
        return "unknown"


def read_manifest(out: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(out / 'manifest.sqlite'))}?mode=ro", uri=True, timeout=5)


def config_path(out: Path) -> Path:
    return out / "ui.json"


def load_config(out: Path) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        saved = json.loads(config_path(out).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return config
    for key in ("backups", "local", "saved"):
        if isinstance(saved.get(key), list):
            config[key] = [str(p) for p in saved[key]]
    if isinstance(saved.get("last_run"), dict):
        config["last_run"] = saved["last_run"]
    options = saved.get("options", {})
    if options.get("youtube_delay") not in ("", None) and "youtube_delay_min" not in options:  # one number, before the range
        try:
            options.update(youtube_delay_min=round(float(options["youtube_delay"]) * 0.7, 2),
                           youtube_delay_max=round(float(options["youtube_delay"]) * 1.3, 2))
        except (TypeError, ValueError):
            pass
    if "youtube_pace_updated" not in options:  # saved with the earlier defaults, which got people blocked
        if (options.get("youtube_delay_min"), options.get("youtube_delay_max")) == (3.5, 6.5):
            del options["youtube_delay_min"], options["youtube_delay_max"]
        if options.get("youtube_block_wait") == 5:
            del options["youtube_block_wait"]
    if options.get("user_agent") and "user_agent_edit" not in options:  # typed by hand, before it was filled in for you
        options["user_agent_edit"] = True
    if "keep_raw" in options:  # its name before web pages stopped being kept
        options.setdefault("keep_raw_pdfs", options.pop("keep_raw"))
    if "audit_after" in options:  # its name before review was called review everywhere
        options.setdefault("review_after", options.pop("audit_after"))
    if isinstance(options.get("lanes"), list) and "forum_lanes_offered" not in options:  # saved before they existed
        options["lanes"] = [*options["lanes"], *FORUM_LANES]
    config["options"].update({k: v for k, v in options.items() if k in config["options"]})
    return config


# -- request guards ------------------------------------------------------------------------------

@web.middleware
async def guard(request: web.Request, handler):
    host = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    if host not in LOCAL_HOSTS:  # another site's name pointed at this address
        raise web.HTTPForbidden(text="This page only answers on localhost.")
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Wrong origin.")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON only.")
    return await handler(request)


async def index(_: web.Request) -> web.StreamResponse:
    return web.FileResponse(PAGE, headers={
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
                                   " connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'",
    })


# -- status --------------------------------------------------------------------------------------

def _lane_table(out: Path) -> dict:
    db = read_manifest(out)
    try:
        lanes: dict[str, Counter] = {}
        for lane, status, n in db.execute("SELECT lane, status, COUNT(*) FROM urls GROUP BY lane, status"):
            lanes.setdefault(lane, Counter())[status] = n
        docs, chars = db.execute(
            "SELECT COUNT(DISTINCT doc_id), COALESCE(SUM(chars), 0) FROM"
            " (SELECT doc_id, MAX(chars) AS chars FROM urls WHERE status IN ('ok', 'thin') GROUP BY doc_id)"
        ).fetchone()
        scholar = dict(db.execute("SELECT status, COUNT(*) FROM urls WHERE url LIKE 'https://scholar.google.%'"
                                  " OR url LIKE 'http://scholar.google.%' GROUP BY status"))
        full_text = dict(db.execute(  # by paper, as the log counts them: pending = not looked for yet, thin = none to be had
            "SELECT CASE WHEN NOT looked THEN 'pending' WHEN chars >= ? THEN 'ok' ELSE 'thin' END AS state, COUNT(*) FROM"
            " (SELECT MAX(instr(tried, ?) > 0) AS looked, MAX(chars) AS chars FROM urls"
            "  WHERE lane = 'arxiv' AND status = 'ok' GROUP BY doc_key) GROUP BY state",
            (ABSTRACT_ONLY_CHARS, f",{FULLTEXT}")))
    finally:
        db.close()
    return {"lanes": {lane: dict(c) for lane, c in lanes.items()}, "scholar": scholar, "arxiv_full_text": full_text, "documents": docs, "chars": chars}


def _chosen_lanes(options: dict) -> list[str]:
    return [lane for lane in options.get("lanes", LANES) if lane in LANES]


def _plan(lanes: list[str], options: dict) -> dict:
    """What a run will fetch, as far as its time left depends on it."""
    low = _number(options, "youtube_delay_min", float, 0, DEFAULT_DELAY[0])
    high = _number(options, "youtube_delay_max", float, 0, DEFAULT_DELAY[1])
    return {"lanes": lanes, "full_text": bool(options.get("arxiv_fulltext", True)), "youtube_each": (low + high) / 2}


def _progress(out: Path, since: float, plan: dict, full_text_left: int) -> dict:
    """What a run has finished, and what each of its sources has left, stage by stage. `each` is the
    fewest seconds one item can take, for the sources that work to a fixed schedule."""
    db = read_manifest(out)
    try:
        counts = {lane: (left, done) for lane, left, done in db.execute(
            "SELECT lane, COUNT(DISTINCT CASE WHEN status = 'pending' THEN doc_key END),"
            " COALESCE(SUM(status NOT IN ('pending', 'skipped') AND updated_at >= ?), 0) FROM urls GROUP BY lane",
            (_iso(since),))}
    finally:
        db.close()
    stages = []
    for lane in plan["lanes"]:
        left = counts.get(lane, (0, 0))[0]
        if lane == "arxiv":
            stages.append({"source": lane, "stage": lane, "left": left, "each": ARXIV_DELAY / ARXIV_BATCH})
            if plan["full_text"]:  # each abstract still to come brings a paper to read
                stages.append({"source": lane, "stage": "full_text", "left": full_text_left + left, "each": FULLTEXT_DELAY})
        else:
            stages.append({"source": lane, "stage": lane, "left": left, "each": plan["youtube_each"] if lane == "youtube" else None})
    return {"done": sum(counts.get(lane, (0, 0))[1] for lane in plan["lanes"]),
            "waiting": sum(stage["left"] for stage in stages), "stages": stages}


async def state(request: web.Request) -> web.Response:
    app = request.app
    out, job, failures = app["out"], app["job"], app["failures"]
    since = int(request.query.get("since", 0))
    new = max(job.count - since, 0)
    lines = list(job.lines)[-new:] if new else []
    db = review.connect(out)
    try:
        counts = review.counts(db)
    finally:
        db.close()
    table = _lane_table(out)
    return web.json_response({
        **table,
        "review": counts,
        "job": {"name": job.name, "step": job.step, "state": job.state, "active": job.active, "count": job.count,
                "lines": lines, "started": job.started, "finished": job.finished,
                "progress": _progress(out, job.started, job.plan, table["arxiv_full_text"].get("pending", 0)) if job.active and job.plan else None},
        "failures": None if request.query.get("failures") == str(failures.version) else {**(failures.summary or {}), "version": failures.version, "ready": failures.summary is not None},
        "index": app["index"].status() if app["index"] else None,
        "keys": {"Scry": bool(load_key()), "GitHub": bool(load_token()), "twitterapi.io": bool(os.environ.get(BILLED_KEY)),
                 "Stack Exchange": bool(os.environ.get("STACKEXCHANGE_KEY"))},
        "out": str(out),
    })


# -- folders -------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _ocr_langs() -> list[str]:
    """The languages tesseract has installed, for choosing one when queueing OCR."""
    try:
        listed = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [lang for lang in listed.split()[1:] if re.fullmatch(r"[A-Za-z_]+", lang) and lang != "osd"]


def _has_browser() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def _config_for_page(app: web.Application, config: dict) -> dict:
    return {**config, "search_index": app["index"] is not None,  # not a setting: `osiris ui --search-index`
            "ocr_langs": _ocr_langs(),
            "defaults": {**DEFAULT_CONFIG["options"], "workers": os.cpu_count() or 1, "pdf_at_once": max(3, 2 * (os.cpu_count() or 1))}}


async def get_config(request: web.Request) -> web.Response:
    return web.json_response(_config_for_page(request.app, load_config(request.app["out"])))


async def set_config(request: web.Request) -> web.Response:
    out = request.app["out"]
    body = await request.json()
    config = load_config(out)
    for key in ("backups", "local", "saved"):
        if isinstance(body.get(key), list):
            config[key] = [str(p) for p in body[key]]
    config["options"].update({k: v for k, v in body.get("options", {}).items() if k in config["options"]})
    config["options"]["retry_hosts"] = _retry_hosts(config["options"])
    _save_config(out, config)
    return web.json_response(_config_for_page(request.app, config))


def _retry_hosts(options: dict) -> list[str]:
    hosts = options.get("retry_hosts")
    return sorted({str(h).lower() for h in hosts if HOSTNAME.match(str(h).lower())}) if isinstance(hosts, list) else []


def _save_config(out: Path, config: dict) -> None:
    tmp = config_path(out).with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.replace(tmp, config_path(out))


async def browse(request: web.Request) -> web.Response:
    """Folder names for the picker. Files are only counted, unless it's choosing a cookies file (files=1):
    then the names of files that could be one are listed too. Files are never read."""
    path = Path(request.query.get("path") or Path.home()).expanduser()
    want_files = request.query.get("files") == "1"
    try:
        path = path.resolve()
        if want_files and path.is_file():  # typed in full
            return web.json_response({"file": str(path), "folder": str(path.parent)})
        entries = list(os.scandir(path))
    except OSError as e:
        raise web.HTTPBadRequest(text=f"Can't open {path}: {e.strerror}")
    folders, files, names = [], 0, []
    for entry in entries:
        try:
            if entry.is_dir():
                if not entry.name.startswith("."):
                    folders.append(entry.name)
            else:
                files += 1
                lower = entry.name.lower()
                if want_files and not lower.startswith(".") and (lower.endswith(COOKIE_FILE_TYPES) or "cookie" in lower):
                    names.append(entry.name)
        except OSError:
            continue
    return web.json_response({"path": str(path), "parent": str(path.parent) if path.parent != path else None,
                              "folders": sorted(folders, key=str.lower), "files": files, "file_names": sorted(names, key=str.lower)})


def _files(n: int, kind: str) -> str:
    return f"{n:,} {kind} file{'' if n == 1 else 's'}"


def _scan_cookies(path: Path) -> dict:
    """How many cookies a file holds, without their values leaving this function."""
    if not path.is_file():
        return {"ok": False, "summary": "File not found"}
    try:
        found = cookies.load(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, "summary": "This doesn't look like a cookies file"}
    if not found:
        return {"ok": False, "summary": "No cookies in this file, or they've all expired"}
    return {"ok": True, "summary": f"{len(found):,} cookies for {len({c.domain.lstrip('.') for c in found}):,} sites"}


def _scan_folder(path: Path, kind: str) -> dict:
    if kind == "cookies":
        return _scan_cookies(path)
    if not path.is_dir():
        return {"ok": False, "summary": "Folder not found"}
    if kind == "backups":
        bookmarks = len(list(path.glob("bookmarks_*.html")))
        sessions = len(list(path.glob("session-buddy-*.json")))
        if not bookmarks and not sessions:
            return {"ok": False, "summary": "No bookmarks_*.html or session-buddy-*.json files here"}
        return {"ok": True, "summary": f"{_files(bookmarks, 'bookmark')}, {_files(sessions, 'Session Buddy')}"}
    readable, other, html = Counter(), 0, 0
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in ("html", "htm"):
                html += 1
            if ext in LOCAL_READABLE:
                readable[ext] += 1
            else:
                other += 1
    if kind == "saved":
        return {"ok": bool(html), "summary": f"{_files(html, 'HTML')}" if html else "No HTML files here"}
    total = sum(readable.values())
    if not total:
        return {"ok": False, "summary": "No readable files here"}
    top = ", ".join(f"{n:,} {ext}" for ext, n in readable.most_common(4))
    return {"ok": True, "summary": f"{total:,} readable ({top})" + (f", {other:,} other files skipped" if other else "")}


async def scan_folder(request: web.Request) -> web.Response:
    path = Path(request.query.get("path", "")).expanduser()
    return web.json_response(await asyncio.to_thread(_scan_folder, path, request.query.get("kind", "local")))


# -- jobs ----------------------------------------------------------------------------------------

def _run_args(options: dict, url_file: Path, every_lane: bool = False) -> list[str]:
    args = ["run", str(url_file)]
    lanes = LANES if every_lane else _chosen_lanes(options)
    if not lanes:
        raise web.HTTPBadRequest(text="Choose at least one kind of source.")
    if len(lanes) < len(LANES):
        for lane in lanes:
            args += ["--only", lane]
    if not options.get("ocr", True):
        args.append("--no-ocr")
    lang = str(options.get("ocr_lang") or "eng")
    if not re.fullmatch(r"[A-Za-z_]+(\+[A-Za-z_]+)*", lang):
        raise web.HTTPBadRequest(text=f"OCR languages should look like eng+tur, not {lang}")
    args += ["--ocr-lang", lang]
    if options.get("refresh") in ("web", "local", "both"):
        args += ["--refresh-pdfs", options["refresh"]]
    if not options.get("retry_failed"):  # which already covers every site
        for host in _retry_hosts(options):
            args += ["--retry-host", host]
    for option, flag in (("retry_failed", "--retry-failed"), ("render", "--render"), ("keep_raw_pdfs", "--keep-raw-pdfs"),
                         ("refetch_comments", "--refetch-comments"), ("refetch_stackexchange", "--refetch-stackexchange"),
                         ("refetch_arxiv_pdfs", "--refetch-arxiv-pdfs"), ("scry_arxiv", "--scry-arxiv"),
                         ("scry_youtube", "--scry-youtube"), ("scry_crawl", "--scry-crawl")):
        if options.get(option):
            args.append(flag)
    if options.get("scry_arxiv"):
        args += ["--scry-arxiv-max-dollars", f"{_number(options, 'scry_arxiv_dollars', float, 0, 5):g}"]
    if options.get("scry_youtube"):
        args += ["--scry-youtube-max-dollars", f"{_number(options, 'scry_youtube_dollars', float, 0, 1):g}"]
    if options.get("scry_crawl"):
        args += ["--scry-crawl-max-dollars", f"{_number(options, 'scry_crawl_dollars', float, 0, 1):g}"]
    if options.get("refetch_comments"):
        args += ["--refetch-comments-days", f"{_number(options, 'refetch_comments_days', float, 0, 0):g}"]
    if options.get("pdf_at_once") not in ("", None):
        args += ["--pdf-at-once", str(_number(options, "pdf_at_once", int, 1, 1))]
    for option, flag in (("arxiv_fulltext", "--no-arxiv-fulltext"), ("impersonate", "--no-impersonate"), ("doi", "--no-doi"),
                         ("wayback", "--no-wayback"), ("scry_twitter", "--no-scry-tweets")):
        if not options.get(option, True):
            args.append(flag)
    if options.get("cookies"):
        cookie_file = Path(options["cookies"]).expanduser()
        if not cookie_file.is_file():
            raise web.HTTPBadRequest(text=f"The cookies file isn't at {cookie_file} any more. Choose it again, or empty that box.")
        args += ["--cookies", str(cookie_file)]
    if options.get("user_agent"):
        args += ["--user-agent", str(options["user_agent"])]
    if options.get("render") and options.get("chrome_profile"):
        args += ["--chrome-profile", str(Path(options["chrome_profile"]).expanduser())]
    if str(options.get("contact") or "").strip():
        args += ["--contact", str(options["contact"]).strip()]
    if options.get("workers") not in ("", None):
        args += ["--workers", str(_number(options, "workers", int, 1, 1))]
    for name, flag, kind, smallest in NUMBER_FLAGS:
        if (value := _number(options, name, kind, smallest, tuning.FLAG_DEFAULTS[name])) != tuning.FLAG_DEFAULTS[name]:
            args += [flag, f"{value:g}"]
    for knob in tuning.KNOBS:
        if (value := _number(options, knob.option, knob.kind, knob.minimum, knob.default)) != knob.default:
            args += [knob.flag, f"{value:g}"]
    low, high = sorted((_number(options, "youtube_delay_min", float, 0, DEFAULT_DELAY[0]),
                        _number(options, "youtube_delay_max", float, 0, DEFAULT_DELAY[1])))
    args += ["--youtube-delay", f"{low:g}-{high:g}"]
    for rule in _host_limits(options):
        args += ["--host-limit", rule]
    return args


def _number(options: dict, name: str, kind: type, smallest: float, default):
    """An option that should be a number. Empty means the default."""
    value = options.get(name)
    if value in ("", None):
        return default
    try:
        number = kind(float(value))
    except (TypeError, ValueError, OverflowError):
        number = None
    if number is None or not smallest <= number < float("inf"):
        raise web.HTTPBadRequest(text=f"{name.replace('_', ' ').capitalize()} should be a number, {smallest:g} or more.")
    return number


def _host_limits(options: dict) -> list[str]:
    """The per-site rules as `osiris run` takes them. A row with no site in it is one still being filled in."""
    rules = []
    for row in options.get("host_limits") or []:
        pattern = str(row.get("pattern") or "").strip().lower()
        if not pattern:
            continue
        if not HOST_PATTERN.fullmatch(pattern):
            raise web.HTTPBadRequest(text=f"{pattern} doesn't look like a site. Use something like example.com or *.example.com")
        rules.append(f"{pattern}={_number(row, 'at_once', int, 1, 1)}:{_number(row, 'seconds', float, 0, 0):g}")
    return rules


def _write_local_urls(folders: list[str], out_file) -> int:
    n = 0
    for folder in folders:
        for root, dirs, names in os.walk(Path(folder).expanduser()):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                out_file.write(Path(root, name).resolve().as_uri() + "\n")
                n += 1
    return n


async def start_archive(request: web.Request) -> web.Response:
    app = request.app
    job, out = app["job"], app["out"]
    if job.active:
        raise web.HTTPConflict(text="A job is already running.")
    config = load_config(out)
    options = config["options"]
    url_file = out / "ui_urls.txt"
    read_files = out / "ui_inputs.txt"  # the tab backups behind this URL list
    run_args = _run_args(options, url_file)

    async def flow(j: Job) -> None:
        for empty in (url_file, read_files):
            empty.write_text("", encoding="utf-8")
        for folder in config["backups"]:
            part = out / TMP_DIR / "ui_extract.txt"
            await j.run(f"Extracting URLs from {folder}",
                        [sys.executable, str(REPO_DIR / "extract_urls.py"), "--input-dir",
                         str(Path(folder).expanduser()), "--output", str(part),
                         "--manifest", str(out / "manifest.sqlite"), "--hashes-out", str(read_files)])
            with open(url_file, "a", encoding="utf-8") as f:
                f.write(part.read_text(encoding="utf-8"))
            part.unlink(missing_ok=True)
        if config["local"]:
            j.step = "Listing local files"
            with open(url_file, "a", encoding="utf-8") as f:
                n = await asyncio.to_thread(_write_local_urls, config["local"], f)
            j.log(f"--- Listed {n:,} local files")
        if config["saved"]:
            await j.run("Importing saved pages", j.osiris("import", *[str(Path(p).expanduser()) for p in config["saved"]]))
        j.plan = _plan(_chosen_lanes(options), options)
        await j.run("Archiving", j.osiris(*run_args, "--mark-inputs", str(read_files)))
        j.plan = None
        if retried := _retry_hosts(options):  # done: later runs leave those sites alone again
            latest = load_config(out)
            latest["options"]["retry_hosts"] = [h for h in _retry_hosts(latest["options"]) if h not in retried]
            _save_config(out, latest)
        if options.get("review_after", True):
            await j.run("Checking for garbage", j.osiris("review", "scan"))

    # twitterapi.io bills per tweet, so a run only sees that key when you've said so
    job.start("Archive", flow, env={} if options.get("billed_twitter") else {BILLED_KEY: None})
    return web.json_response({"ok": True})


async def start_review(request: web.Request) -> web.Response:
    job = request.app["job"]
    if job.active:
        raise web.HTTPConflict(text="A job is already running.")

    async def flow(j: Job) -> None:
        await j.run("Checking for garbage", j.osiris("review", "scan"))

    job.start("Garbage check", flow)
    return web.json_response({"ok": True})


async def start_fixes(request: web.Request) -> web.Response:
    app = request.app
    job, out = app["job"], app["out"]
    if job.active:
        raise web.HTTPConflict(text="A job is already running.")
    empty = out / "ui_no_urls.txt"
    empty.write_text("", encoding="utf-8")
    run_args = _run_args(load_config(out)["options"], empty, every_lane=True)  # your settings, for any source
    db = review.connect(out)
    try:
        billed = db.execute("SELECT 1 FROM decisions WHERE decision = 'refetch' AND method = 'twitterapi'"
                            " AND applied_at IS NULL LIMIT 1").fetchone() is not None
    finally:
        db.close()

    async def flow(j: Job) -> None:
        await j.run("Carrying out fixes", j.osiris(*run_args, "--fixes"))
        await j.run("Checking the results", j.osiris("review", "scan"))

    # twitterapi.io bills per tweet, so the run only sees that key when you queued a fix that asks for it
    job.start("Fixes", flow, env={} if billed else {BILLED_KEY: None})
    return web.json_response({"ok": True})


async def start_export(request: web.Request) -> web.Response:
    job = request.app["job"]
    if job.active:
        raise web.HTTPConflict(text="A job is already running.")

    async def flow(j: Job) -> None:
        await j.run("Exporting", j.osiris("export"))

    job.start("Export", flow)
    return web.json_response({"ok": True})


CLEAN_MODES = {"preview": ("Cleanup preview", "Previewing the cleanup", ["--dry-run"]),
               "clean": ("Text cleanup", "Cleaning up text", []),
               "undo": ("Undoing the cleanup", "Putting the originals back", ["--undo"])}
REPORT_PAGE = 200
MAX_FAILURE_ROWS = 20_000


async def start_clean(request: web.Request) -> web.Response:
    job = request.app["job"]
    if job.active:
        raise web.HTTPConflict(text="A job is already running.")
    mode = (await request.json()).get("mode", "clean")
    if mode not in CLEAN_MODES:
        raise web.HTTPBadRequest(text="Unknown cleanup.")
    name, step, flags = CLEAN_MODES[mode]

    async def flow(j: Job) -> None:
        await j.run(step, j.osiris("clean", *flags))

    job.start(name, flow)
    return web.json_response({"ok": True})


def _report(app: web.Application) -> tuple[dict, list[tuple[str, ...]]] | None:
    """The last cleanup report, read again only when it has changed."""
    path = app["out"] / cleanup.REPORT_FILE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = app.get("report")
    if not cached or cached[0] != mtime:
        app["report"] = cached = (mtime, cleanup.read_report(app["out"]))
    return cached[1]


async def cleanup_state(request: web.Request) -> web.Response:
    report = await asyncio.to_thread(_report, request.app)
    return web.json_response({"report": report[0] if report else None,
                              "kept": await asyncio.to_thread(cleanup.kept_originals, request.app["out"])})


async def cleanup_report(request: web.Request) -> web.Response:
    """A page of the last report's rows, optionally of one rule, with how many each rule has."""
    report = await asyncio.to_thread(_report, request.app)
    if report is None:
        raise web.HTTPNotFound(text="There's no cleanup report yet. Preview the cleanup to make one.")
    summary, rows = report
    rule, offset = request.query.get("rule", ""), max(int(request.query.get("offset", 0)), 0)
    chosen = [r for r in rows if len(r) == 6 and (not rule or r[2] == rule)]
    return web.json_response({
        "summary": summary, "total": len(chosen),
        "rules": Counter(r[2] for r in rows if len(r) == 6).most_common(),
        "items": [dict(zip(cleanup.REPORT_COLUMNS, r)) for r in chosen[offset:offset + REPORT_PAGE]]})


async def stop_job(request: web.Request) -> web.Response:
    request.app["job"].stop()
    return web.json_response({"ok": True})


# -- review --------------------------------------------------------------------------------------

DECISION_FILTERS = ("none", *review.DECISIONS, review.CHOOSE, review.FIXED)  # none: not decided yet


def _decision_filter(value: str) -> tuple[str, list] | None:
    if value == "none":
        return "d.doc_id IS NULL", []
    if value in DECISION_FILTERS:
        return "d.decision = ?", [value]
    return None


def _review_facts(out: Path, ids: list[str]) -> dict[str, tuple]:
    """{doc_id: (score, signals, decision, note, method)} from the garbage check, for listing."""
    if not ids:
        return {}
    db = review.connect(out)
    try:
        return {did: rest for did, *rest in db.execute(
            "SELECT s.doc_id, s.score, s.signals, d.decision, d.note, d.method FROM scores s"
            f" LEFT JOIN decisions d USING (doc_id) WHERE s.doc_id IN ({','.join('?' * len(ids))})", ids)}
    finally:
        db.close()


def _with_review(out: Path, items: list[dict]) -> list[dict]:
    facts = _review_facts(out, [it["id"] for it in items])
    for it in items:
        score, signals, decision, note, method = facts.get(it["id"], (None, "{}", None, None, None))
        it.update(score=score, signals=json.loads(signals or "{}"), decision=decision, note=note, method=method)
    return items


def _fix_method(decision: str | None, method) -> str | None:
    if method in (None, "", "again"):
        return None
    if decision == "refetch" and method in fixes.METHODS:
        return method
    if decision == "ocr" and re.fullmatch(r"[A-Za-z_]+(\+[A-Za-z_]+)*", str(method)):
        return method
    raise web.HTTPBadRequest(text="That way of fixing it doesn't exist.")


async def decide(request: web.Request) -> web.Response:
    app = request.app
    body = await request.json()
    decision = body.get("decision")
    if decision is not None and decision not in review.DECISIONS:
        raise web.HTTPBadRequest(text="Unknown decision.")
    method = _fix_method(decision, body.get("method"))
    if isinstance(body.get("filter"), dict):  # the whole list as filtered: what's flagged and still undecided in it
        where, params = _list_where({**body["filter"], "decision": "none"})
        manifest = _list_db(app["out"])
        try:
            ids = [did for (did,) in manifest.execute(
                f"SELECT DISTINCT u.doc_id FROM urls u{LIST_JOINS} WHERE {where} AND s.score >= ?", (*params, review.FLAGGED))]
        finally:
            manifest.close()
    else:
        ids = [d for d in body.get("ids", []) if DOC_ID.match(str(d))]
    db = review.connect(app["out"])
    try:
        review.ensure_scored(db, app["store"], ids)  # never checked, or failed: it can still be decided on
        n = review.decide(db, ids, decision, str(body.get("note") or "") or None, method)
        return web.json_response({"changed": n, "counts": review.counts(db)})
    finally:
        db.close()


async def choose_version(request: web.Request) -> web.Response:
    app = request.app
    if app["job"].active:  # it could be writing this document or the manifest
        raise web.HTTPConflict(text="Wait for the current job to finish first.")
    body = await request.json()
    did, keep = str(body.get("id", "")), body.get("keep")
    if not DOC_ID.match(did) or keep not in ("new", "earlier"):
        raise web.HTTPBadRequest(text="Unknown document or version.")
    db = review.connect(app["out"])
    try:
        try:
            review.choose(app["store"], db, did, keep == "new")
        except ValueError:
            raise web.HTTPConflict(text="This document isn't waiting for a choice any more.") from None
        return web.json_response({"counts": review.counts(db)})
    finally:
        db.close()


async def delete_document(request: web.Request) -> web.Response:
    app = request.app
    if app["job"].active:  # it could be writing this document or the manifest
        raise web.HTTPConflict(text="Wait for the current job to finish first.")
    did = str((await request.json()).get("id", ""))
    if not DOC_ID.match(did):
        raise web.HTTPBadRequest(text="Unknown document.")
    store: Store = app["store"]
    if not store.delete(did):
        raise web.HTTPNotFound(text="Not in the archive.")
    review.forget(store, {did})
    cleanup.forget(app["out"], did)
    if app["index"]:
        app["index"].wanted.set()  # the next refresh drops it from the search index
    return web.json_response({"ok": True})


# -- documents -----------------------------------------------------------------------------------

def _fold(text: str | None) -> str:
    return text.casefold() if text else ""


LIST_JOINS = " LEFT JOIN r.scores s ON s.doc_id = u.doc_id LEFT JOIN r.decisions d ON d.doc_id = u.doc_id"


def _list_db(out: Path) -> sqlite3.Connection:
    """The manifest, with the garbage check's scores and decisions alongside as `r`."""
    db = read_manifest(out)
    db.create_function("fold", 1, _fold, deterministic=True)  # SQLite's LIKE only ignores case for A to Z
    db.execute("ATTACH DATABASE ? AS r", (f"file:{quote(str(out / review.DB_FILE))}?mode=ro",))
    return db


def _list_where(q) -> tuple[str, list]:
    where, params = ["u.lane != 'skip'", "u.doc_id IS NOT NULL"], []
    for column in ("lane", "status", "reason"):
        if q.get(column):
            where.append(f"u.{column} = ?")
            params.append(q[column])
    if term := str(q.get("q", "")).strip():
        where.append("(u.url LIKE ? OR fold(u.title) LIKE ?)")
        params += [f"%{term}%", f"%{term.casefold()}%"]
    if decided := _decision_filter(q.get("decision", "")):
        where.append(decided[0])
        params += decided[1]
    return " AND ".join(where), params


async def documents(request: web.Request) -> web.Response:
    """The plain list, newest first or most likely garbage first, filtered by URL or title text and the
    rest: for every status, including documents with no text, which the search index doesn't hold."""
    q = request.query
    where, params = _list_where(q)
    order = "COALESCE(s.score, -1) DESC, u.doc_id" if q.get("sort") == "garbage" else "MIN(u.rowid) DESC"
    offset = max(int(q.get("offset", 0)), 0)
    manifest = _list_db(request.app["out"])
    try:
        rows = manifest.execute(
            f"SELECT u.doc_id, MIN(u.url), u.lane, u.status, u.reason, MAX(u.chars), MAX(u.title) FROM urls u{LIST_JOINS}"
            f" WHERE {where} GROUP BY u.doc_id ORDER BY {order} LIMIT ? OFFSET ?", (*params, PAGE_SIZE, offset),
        ).fetchall()
        total = manifest.execute(f"SELECT COUNT(DISTINCT u.doc_id) FROM urls u{LIST_JOINS} WHERE {where}", params).fetchone()[0]
    finally:
        manifest.close()
    return web.json_response({"total": total, "items": _with_review(request.app["out"], [
        {"id": did, "url": url, "lane": lane, "status": status, "reason": reason, "chars": chars,
         "title": title or None, "host": urlsplit(url).hostname or "local file"}
        for did, url, lane, status, reason, chars, title in rows])})


async def failures_list(request: web.Request) -> web.Response:
    """A page of failed documents for the reasons asked for, from the list kept ready."""
    q = request.query
    return web.json_response(request.app["failures"].page(
        q.getall("reason", []), q.get("scope") == "run", q.get("host", ""),
        max(int(q.get("offset", 0)), 0), min(max(int(q.get("limit", REPORT_PAGE)), 1), MAX_FAILURE_ROWS)))


async def reasons(request: web.Request) -> web.Response:
    """Why documents failed, most common first, with the sites each reason hits most."""
    lane = request.query.get("lane", "")
    db = read_manifest(request.app["out"])
    try:
        rows = db.execute(
            "SELECT reason, url, doc_id FROM urls WHERE status = 'failed' AND lane != 'skip'"
            + (" AND lane = ?" if lane else ""), (lane,) if lane else ()).fetchall()
    finally:
        db.close()
    docs: dict[str, set] = {}
    hosts: dict[str, Counter] = {}
    for reason, url, did in rows:
        reason = reason or "unknown"
        if did not in docs.setdefault(reason, set()):
            docs[reason].add(did)
            hosts.setdefault(reason, Counter())[urlsplit(url).hostname or "local file"] += 1
    return web.json_response({"reasons": [
        {"reason": r, "count": len(ids), "hosts": [{"host": h, "count": n} for h, n in hosts[r].most_common(5)]}
        for r, ids in sorted(docs.items(), key=lambda kv: -len(kv[1]))]})


def _scan_text(out: Path, term: str, lane: str, status: str, garbage_first: bool = False, decision: str = "") -> dict:
    """Without the search index: a plain case-insensitive scan of the documents, newest first,
    stopping at the limit. The filters apply while scanning, so a filtered scan isn't cut short."""
    where, params = ["status IN ('ok', 'thin')", "doc_id IS NOT NULL"], []
    for column, value in (("lane", lane), ("status", status)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    db = read_manifest(out)
    try:
        rows = db.execute(f"SELECT doc_id, MIN(url), lane, status, reason, MAX(chars) FROM urls WHERE {' AND '.join(where)}"
                          " GROUP BY doc_id ORDER BY MIN(rowid) DESC", params).fetchall()
    finally:
        db.close()
    pattern = re.compile(re.escape(term), re.I)
    items, searched = [], 0
    for did, url, lane_, status_, reason, chars in rows:
        searched += 1
        doc = read_doc_file(md_path(out, did))
        if doc is None:
            continue
        meta, body = doc
        if not (pattern.search(body) or pattern.search(meta.get("title") or "")):
            continue
        items.append({"id": did, "url": url, "lane": lane_, "status": status_, "reason": reason, "chars": chars,
                      "title": meta.get("title"), "host": urlsplit(url).hostname or "local file",
                      "snippet": search.snippet(body, pattern)})
        if len(items) >= SCAN_LIMIT:
            break
    items = _with_review(out, items)
    if decision == "none":
        items = [it for it in items if it["decision"] is None]
    elif decision in DECISION_FILTERS:
        items = [it for it in items if it["decision"] == decision]
    if garbage_first:
        items.sort(key=lambda it: -(it["score"] if it["score"] is not None else -1))
    return {"items": items, "total": len(items), "limited": len(items) >= SCAN_LIMIT, "searched": searched,
            "terms": [{"word": term, "substring": True}]}


async def search_documents(request: web.Request) -> web.Response:
    """Ranked full-text search from the search index, or a plain scan of the text without one."""
    q = request.query
    term = q.get("q", "").strip()
    index, out = request.app["index"], request.app["out"]
    if index is None:
        if len(term) < 2:
            raise web.HTTPBadRequest(text="Search for at least 2 characters.")
        found = await asyncio.to_thread(_scan_text, out, term, q.get("lane", ""), q.get("status", ""),
                                        q.get("sort") == "garbage", q.get("decision", ""))
        return web.json_response(found)
    if not term:
        raise web.HTTPBadRequest(text="Type something to search for.")
    found = await asyncio.to_thread(search.search, out, term, q.get("lane", ""), q.get("status", ""),
                                    search.PAGE, max(int(q.get("offset", 0)), 0), q.get("sort") == "garbage",
                                    _decision_filter(q.get("decision", "")))
    found["items"] = _with_review(out, found["items"])
    return web.json_response({**found, "index": index.status()})


def _doc_rows(out: Path, did: str) -> list[tuple]:
    db = read_manifest(out)
    try:
        return db.execute("SELECT url, lane, status, reason, http_status, final_url, chars, tried, raw_path, doc_key"
                          " FROM urls WHERE doc_id = ? ORDER BY rowid", (did,)).fetchall()
    finally:
        db.close()


def _original(out: Path, rows: list[tuple]) -> tuple[Path | None, str | None]:
    """A file we may serve for this document, known to the manifest: yours, or our raw copy."""
    for url, lane, *_ , raw_path, _key in rows:
        if lane == "localfile" and urlsplit(url).scheme == "file":
            for path in candidate_paths(url):
                if path.is_file():
                    return path, "local"
    for row in rows:
        raw_path = row[8]
        if raw_path:
            path = (out / raw_path).resolve()
            if path.is_relative_to((out / "raw").resolve()) and path.is_file():
                return path, "raw"
    return None, None


def _saved_page(out: Path, rows: list[tuple]) -> Path | None:
    if not rows or rows[0][1] != "singlefile":
        return None
    db = read_manifest(out)
    try:
        for url, *_, key in rows:  # a page with no URL of its own is keyed by its hash
            row = db.execute("SELECT name FROM imported_files WHERE url = ? OR 'file:' || sha1 = ? LIMIT 1",
                             (url, key)).fetchone()
            if row and row[0] and Path(row[0]).is_absolute() and Path(row[0]).is_file():
                return Path(row[0])
    finally:
        db.close()
    return None


async def document(request: web.Request) -> web.Response:
    did = request.match_info["id"]
    if not DOC_ID.match(did):
        raise web.HTTPBadRequest(text="Bad document id.")
    out = request.app["out"]
    rows = _doc_rows(out, did)
    if not rows:
        raise web.HTTPNotFound(text="Not in the archive.")
    store: Store = request.app["store"]
    doc = store.read_doc(did)
    meta, body = doc if doc else ({}, "")
    path, kind = _original(out, rows)
    saved = _saved_page(out, rows)
    db = review.connect(out)
    try:
        scored = db.execute(
            "SELECT s.score, s.signals, s.source, d.decision, d.note, d.method FROM scores s LEFT JOIN decisions d USING (doc_id)"
            " WHERE s.doc_id = ?", (did,)).fetchone()
        earlier = review.earlier_version(db, did)
    finally:
        db.close()
    url, lane, status, reason, http_status, final_url, chars, tried, *_ = rows[0]
    local = lane == "localfile" or urlsplit(url).scheme == "file"
    return web.json_response({
        "id": did, "meta": meta, "body": body[:MAX_BODY], "truncated": len(body) > MAX_BODY, "length": len(body),
        "urls": [r[0] for r in rows], "url": url, "final_url": final_url, "lane": lane, "status": status,
        "host": urlsplit(url).hostname,
        "reason": reason, "http_status": http_status, "chars": chars,
        "tried": [t for t in (tried or "").split(",") if t],
        "web_url": None if local else url,
        "file": str(store.md_path(did)) if doc else None,
        "original": {"available": bool(path or saved), "kind": kind or ("saved" if saved else None),
                     "name": (path or saved).name if (path or saved) else None,
                     "path": str(path or saved) if kind == "local" or saved else None,
                     "viewer": _viewer(path or saved) if (path or saved) else None},
        "local_missing": local and not (path or saved),
        "score": scored[0] if scored else None, "signals": json.loads(scored[1]) if scored else {},
        "decision": scored[3] if scored else None, "note": scored[4] if scored else None,
        "method": scored[5] if scored else None,
        "pdf": review.is_pdf_source(meta.get("source") or (scored[2] if scored else None)),
        "methods": fixes.methods_for(rows[0][-1], lane, bool(load_key()), _has_browser()),
        "earlier": {**earlier, "body": earlier["body"][:MAX_BODY], "truncated": earlier["length"] > MAX_BODY}
        if earlier else None,
    })


def _suffixes(path: Path) -> str:
    return path.name[:-4].rsplit(".", 1)[-1].lower() if path.suffix == ".zst" else path.suffix.lstrip(".").lower()


def _viewer(path: Path) -> str:
    ext = f".{_suffixes(path)}"
    if ext == ".pdf":
        return "pdf"
    if ext in HTML_TYPES:
        return "html"
    if ext in TEXT_TYPES:
        return "text"
    return "download"


def _sandboxed(body: bytes, content_type: str, name: str) -> web.Response:
    # a saved page is someone else's page: it mustn't run as this one
    return web.Response(body=body, headers={
        "Content-Type": content_type, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}",
    })


MAX_INLINE = 200 * 2**20


async def original(request: web.Request) -> web.StreamResponse:
    did = request.match_info["id"]
    if not DOC_ID.match(did):
        raise web.HTTPBadRequest(text="Bad document id.")
    out = request.app["out"]
    rows = _doc_rows(out, did)
    path, _ = _original(out, rows)
    path = path or _saved_page(out, rows)
    if path is None:
        raise web.HTTPNotFound(text="No copy of the original on this machine.")
    viewer = _viewer(path)
    name = path.name.removesuffix(".zst")
    if path.suffix == ".zst":
        import zstandard

        data = await asyncio.to_thread(
            lambda: zstandard.ZstdDecompressor().stream_reader(path.read_bytes()).read(MAX_INLINE))
        kind = "text/html; charset=utf-8" if viewer == "html" else "text/plain; charset=utf-8"
        return _sandboxed(data, kind, name)
    if viewer in ("html", "text"):
        if path.stat().st_size > MAX_INLINE:
            raise web.HTTPRequestEntityTooLarge(MAX_INLINE, path.stat().st_size)
        data = await asyncio.to_thread(path.read_bytes)
        kind = "text/html; charset=utf-8" if viewer == "html" else "text/plain; charset=utf-8"
        return _sandboxed(data, kind, name)
    disposition = "inline" if viewer == "pdf" else "attachment"
    return web.FileResponse(path, headers={
        "Content-Type": "application/pdf" if viewer == "pdf" else "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
    })


async def raw_text(request: web.Request) -> web.StreamResponse:
    did = request.match_info["id"]
    if not DOC_ID.match(did):
        raise web.HTTPBadRequest(text="Bad document id.")
    path = request.app["store"].md_path(did)
    if not path.is_file():
        raise web.HTTPNotFound(text="No saved text for this document.")
    return web.FileResponse(path, headers={"Content-Type": "text/plain; charset=utf-8",
                                           "X-Content-Type-Options": "nosniff"})


# -- app -----------------------------------------------------------------------------------------

async def _start_failures(app: web.Application) -> None:
    app["failures_task"] = asyncio.create_task(app["failures"].keep())


async def _fill_titles(app: web.Application) -> None:
    try:
        filled = await asyncio.to_thread(fill_missing_titles, app["out"], app["titles_stop"])
        if filled:
            print(f"--- Added the titles of {filled:,} earlier documents to the list", flush=True)
    except (sqlite3.Error, OSError) as e:  # what's left is filled in at the next start
        log.warning("couldn't fill in document titles: %s", e)


async def _start_titles(app: web.Application) -> None:
    app["titles_task"] = asyncio.create_task(_fill_titles(app))


async def _start_index(app: web.Application) -> None:
    app["index_task"] = asyncio.create_task(app["index"].keep(app["job"]))


async def _stop_job_on_exit(app: web.Application) -> None:
    app["failures_task"].cancel()
    app["titles_stop"].set()
    await app["titles_task"]
    index = app["index"]
    if index:
        index.stopping = True  # the refresh in progress stops at its next document
        index.wanted.set()
    job = app["job"]
    if job.active:
        job.stop()
        try:
            await asyncio.wait_for(asyncio.shield(job.task), 15)
        except asyncio.TimeoutError:
            job.stop()
    if index:
        try:
            await asyncio.wait_for(app["index_task"], 30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    app["store"].close()


def make_app(out: str | os.PathLike, search_index: bool = False) -> web.Application:
    out = Path(out).resolve()
    app = web.Application(middlewares=[guard], client_max_size=2**20)
    app["out"] = out
    app["store"] = Store(out)  # creates and upgrades the manifest, so the read-only connections can open it
    app["job"] = Job(out)
    app["index"] = Index(out) if search_index else None  # off unless asked for: it takes disk and CPU
    app["failures"] = Failures(out)
    app["titles_stop"] = threading.Event()
    review.connect(out).close()

    def job_changed() -> None:
        job = app["job"]
        if job.name in FETCHING_JOBS:
            app["failures"].note_run(job.started, job.finished)
        if app["index"]:
            app["index"].wanted.set()

    app["job"].on_change = job_changed
    app.on_startup.append(_start_failures)
    app.on_startup.append(_start_titles)
    if search_index:
        search.connect(out).close()
        app.on_startup.append(_start_index)
    app.on_shutdown.append(_stop_job_on_exit)
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/api/config", get_config),
        web.post("/api/config", set_config),
        web.get("/api/browse", browse),
        web.get("/api/scan", scan_folder),
        web.post("/api/job/archive", start_archive),
        web.post("/api/job/review", start_review),
        web.post("/api/job/fixes", start_fixes),
        web.post("/api/job/export", start_export),
        web.post("/api/job/clean", start_clean),
        web.get("/api/cleanup", cleanup_state),
        web.get("/api/cleanup/report", cleanup_report),
        web.post("/api/job/stop", stop_job),
        web.post("/api/decide", decide),
        web.post("/api/review/choose", choose_version),
        web.post("/api/delete", delete_document),
        web.get("/api/documents", documents),
        web.get("/api/search", search_documents),
        web.get("/api/reasons", reasons),
        web.get("/api/failures", failures_list),
        web.get("/api/document/{id}", document),
        web.get("/original/{id}", original),
        web.get("/saved/{id}", raw_text),
    ])
    return app


def terminal_log(address: str):
    """Writes each log line to the terminal too, with the page's address every ADDRESS_EVERY lines."""
    printed = 0

    def write(line: str) -> None:
        nonlocal printed
        try:
            print(line, flush=True)
            printed += bool(line)
            if line and printed % ADDRESS_EVERY == 0:
                print(f"--- osiris is open at {address}", flush=True)
        except (OSError, ValueError):  # a character it can't show, or it's closing and the job is being stopped in order
            pass

    return write


def serve(out: str | os.PathLike, port: int, search_index: bool = False) -> None:
    app = make_app(out, search_index)
    address = f"http://127.0.0.1:{port}/"
    app["job"].on_line = terminal_log(address)

    def terminal_closed() -> None:
        raise web.GracefulExit()

    async def announce(_: web.Application) -> None:
        # the same way out as Ctrl-C: a job has its own session, so nothing else would tell it the terminal has gone
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, terminal_closed)
        print(f"\nosiris is open at\n\n  {address}\n\nPress Ctrl-C here to stop it.", flush=True)
        if os.environ.get("SSH_CONNECTION"):
            print(f"Over ssh, first run this on your own machine: ssh -N -L {port}:127.0.0.1:{port} <this server>",
                  flush=True)

    app.on_startup.append(announce)
    web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None)
