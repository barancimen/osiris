"""`osiris ui`: the whole process from one local web page.

It listens on this machine only, and every request needs a token kept in the output directory,
because the page can read your files and start jobs. Work runs as ordinary `osiris` subprocesses,
one job at a time, so the page never holds up a run and a run never depends on the page's state.
Originals are only served for files the manifest already knows about, never for an arbitrary path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import sqlite3
import sys
import time
import webbrowser
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

from . import cleanup, review, search
from .github import load_token
from .localfile import candidate_paths
from .route import FORUM_LANES, LOCAL_READABLE
from .scry import load_key
from .store import TMP_DIR, Store, md_path, read_doc_file

REPO_DIR = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).with_name("ui.html")
LANES = ("twitter", "arxiv", "stackexchange", "reddit", "hackernews", "lesswrong", "github", "localfile", "generic", "youtube")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
DOC_ID = re.compile(r"^[0-9a-f]{16}$")
BILLED_KEY = "TWITTERAPI_IO_KEY"
MAX_BODY = 400_000  # characters of a document sent to the page; the raw link has the rest
PAGE_SIZE = 100
SCAN_LIMIT = 100  # matches a text scan stops at, when there's no search index
INDEX_EVERY = 300  # seconds between search-index refreshes while a job runs
PROGRESS_JOBS = ("Archive", "Fixes")  # jobs whose progress the manifest shows
HOSTNAME = re.compile(r"^[a-z0-9.-]+$")
TEXT_TYPES = {".txt", ".md", ".markdown", ".rst", ".tex", ".org"}
HTML_TYPES = {".html", ".htm", ".xhtml"}
DEFAULT_CONFIG = {
    "backups": [], "local": [], "saved": [],
    "options": {"lanes": list(LANES), "forum_lanes_offered": True, "ocr": True, "ocr_lang": "eng", "refresh": "", "retry_failed": False,
                "review_after": True, "cookies": "", "user_agent": "", "render": False, "keep_raw_pdfs": False, "arxiv_fulltext": True,
                "workers": "", "youtube_delay": "", "billed_twitter": False,
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
        self.on_change = lambda: None  # a job started or ended

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def log(self, line: str) -> None:
        self.lines.append(line.rstrip())
        self.count += 1

    def start(self, name: str, flow, env: dict[str, str | None] | None = None) -> None:
        self.name, self.step, self.state = name, "", "running"
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
    options = saved.get("options", {})
    if "keep_raw" in options:  # its name before web pages stopped being kept
        options.setdefault("keep_raw_pdfs", options.pop("keep_raw"))
    if "audit_after" in options:  # its name before review was called review everywhere
        options.setdefault("review_after", options.pop("audit_after"))
    if isinstance(options.get("lanes"), list) and "forum_lanes_offered" not in options:  # saved before they existed
        options["lanes"] = [*options["lanes"], *FORUM_LANES]
    config["options"].update({k: v for k, v in options.items() if k in config["options"]})
    return config


def load_ui_token(out: Path) -> str:
    """Kept across restarts so a bookmarked address keeps working."""
    path = out / "ui-token"
    try:
        token = path.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


# -- request guards ------------------------------------------------------------------------------

@web.middleware
async def guard(request: web.Request, handler):
    app = request.app
    host = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    if host not in LOCAL_HOSTS:  # another site's name pointed at this address
        raise web.HTTPForbidden(text="This page only answers on localhost.")
    given = request.query.get("token")
    if given and secrets.compare_digest(given, app["token"]):
        response = web.HTTPFound("/")
        response.set_cookie("osiris_ui", app["token"], httponly=True, samesite="Strict", max_age=31536000)
        raise response
    if not secrets.compare_digest(request.cookies.get("osiris_ui", ""), app["token"]):
        raise web.HTTPForbidden(text="Open the address that `osiris ui` printed in your terminal.")
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
    finally:
        db.close()
    return {"lanes": {lane: dict(c) for lane, c in lanes.items()}, "documents": docs, "chars": chars}


def _progress(out: Path, since: float) -> dict:
    """URLs finished since the job started, and how many are still waiting."""
    started = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = read_manifest(out)
    try:
        done, waiting = db.execute(
            "SELECT COALESCE(SUM(status NOT IN ('pending', 'skipped') AND updated_at >= ?), 0),"
            " COALESCE(SUM(status = 'pending'), 0) FROM urls", (started,)).fetchone()
    finally:
        db.close()
    return {"done": done, "waiting": waiting}


async def state(request: web.Request) -> web.Response:
    app = request.app
    out, job = app["out"], app["job"]
    since = int(request.query.get("since", 0))
    new = max(job.count - since, 0)
    lines = list(job.lines)[-new:] if new else []
    db = review.connect(out)
    try:
        counts = review.counts(db)
    finally:
        db.close()
    return web.json_response({
        **_lane_table(out),
        "review": counts,
        "job": {"name": job.name, "step": job.step, "state": job.state, "active": job.active, "count": job.count,
                "lines": lines, "started": job.started, "finished": job.finished,
                "progress": _progress(out, job.started) if job.active and job.name in PROGRESS_JOBS else None},
        "index": app["index"].status() if app["index"] else None,
        "keys": {"Scry": bool(load_key()), "GitHub": bool(load_token()), "twitterapi.io": bool(os.environ.get(BILLED_KEY)),
                 "Stack Exchange": bool(os.environ.get("STACKEXCHANGE_KEY"))},
        "out": str(out),
    })


# -- folders -------------------------------------------------------------------------------------

def _config_for_page(app: web.Application, config: dict) -> dict:
    return {**config, "search_index": app["index"] is not None}  # not a setting: `osiris ui --search-index`


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
    """Folder names for the picker. Files are only counted, never listed or read."""
    path = Path(request.query.get("path") or Path.home()).expanduser()
    try:
        path = path.resolve()
        entries = list(os.scandir(path))
    except OSError as e:
        raise web.HTTPBadRequest(text=f"Can't open {path}: {e.strerror}")
    folders, files = [], 0
    for entry in entries:
        try:
            if entry.is_dir():
                if not entry.name.startswith("."):
                    folders.append(entry.name)
            else:
                files += 1
        except OSError:
            continue
    return web.json_response({"path": str(path), "parent": str(path.parent) if path.parent != path else None,
                              "folders": sorted(folders, key=str.lower), "files": files})


def _files(n: int, kind: str) -> str:
    return f"{n:,} {kind} file{'' if n == 1 else 's'}"


def _scan_folder(path: Path, kind: str) -> dict:
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

def _run_args(options: dict, url_file: Path) -> list[str]:
    args = ["run", str(url_file)]
    lanes = [lane for lane in options.get("lanes", LANES) if lane in LANES]
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
    for option, flag in (("retry_failed", "--retry-failed"), ("render", "--render"),
                         ("keep_raw_pdfs", "--keep-raw-pdfs")):
        if options.get(option):
            args.append(flag)
    if not options.get("arxiv_fulltext", True):
        args.append("--no-arxiv-fulltext")
    if options.get("cookies"):
        args += ["--cookies", str(Path(options["cookies"]).expanduser())]
    if options.get("user_agent"):
        args += ["--user-agent", str(options["user_agent"])]
    if str(options.get("workers") or "").isdigit() and int(options["workers"]) > 0:
        args += ["--workers", str(int(options["workers"]))]
    try:
        if options.get("youtube_delay") not in ("", None):
            args += ["--youtube-delay", str(float(options["youtube_delay"]))]
    except ValueError:
        raise web.HTTPBadRequest(text="The YouTube delay should be a number of seconds.")
    return args


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
    run_args = _run_args(options, url_file)

    async def flow(j: Job) -> None:
        with open(url_file, "w", encoding="utf-8"):
            pass
        for folder in config["backups"]:
            part = out / TMP_DIR / "ui_extract.txt"
            await j.run(f"Extracting URLs from {folder}",
                        [sys.executable, str(REPO_DIR / "extract_urls.py"), "--input-dir",
                         str(Path(folder).expanduser()), "--output", str(part)])
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
        await j.run("Archiving", j.osiris(*run_args))
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
    lang = str(load_config(out)["options"].get("ocr_lang") or "eng")
    empty = out / "ui_no_urls.txt"
    empty.write_text("", encoding="utf-8")

    async def flow(j: Job) -> None:
        output = await j.run("Applying fixes", j.osiris("review", "apply", "--lang", lang))
        lanes = next((ln.removeprefix("lanes to run:").split() for ln in reversed(output)
                      if ln.startswith("lanes to run:")), [])
        if lanes:
            only = [arg for lane in lanes for arg in ("--only", lane)]
            await j.run("Fetching again", j.osiris("run", str(empty), *only, "--no-ocr"))
        await j.run("Checking the results", j.osiris("review", "scan"))

    job.start("Fixes", flow, env={BILLED_KEY: None})
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

REVIEW_VIEWS = {"to_review": "d.doc_id IS NULL AND s.score >= ?", **{d: "d.decision = ? " for d in review.DECISIONS}}


def _review_where(view: str, host: str | None) -> tuple[str, list]:
    if view not in REVIEW_VIEWS:
        raise web.HTTPBadRequest(text="Unknown view.")
    where = REVIEW_VIEWS[view]
    params: list = [review.FLAGGED if view == "to_review" else view]
    if host:
        where += " AND s.host = ?"
        params.append(host)
    return where, params


def _row_for_page(row: tuple) -> dict:
    did, url, lane, source, host, title, chars, score, signals, excerpt, decision, note = row
    return {"id": did, "url": url, "lane": lane, "source": source, "host": host, "title": title, "chars": chars,
            "score": score, "signals": json.loads(signals), "excerpt": excerpt, "decision": decision, "note": note,
            "pdf": review.is_pdf_source(source)}


async def review_list(request: web.Request) -> web.Response:
    q = request.query
    view = q.get("view", "to_review")
    where, params = _review_where(view, q.get("host") or None)
    offset = max(int(q.get("offset", 0)), 0)
    db = review.connect(request.app["out"])
    try:
        rows = db.execute(
            "SELECT s.doc_id, s.url, s.lane, s.source, s.host, s.title, s.chars, s.score, s.signals, s.excerpt,"
            " d.decision, d.note FROM scores s LEFT JOIN decisions d USING (doc_id)"
            f" WHERE {where} ORDER BY s.score DESC, s.doc_id LIMIT ? OFFSET ?",
            (*params, PAGE_SIZE, offset),
        ).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM scores s LEFT JOIN decisions d USING (doc_id) WHERE {where}",
                           params).fetchone()[0]
        view_where, view_params = _review_where(view, None)
        hosts = db.execute(
            f"SELECT s.host, COUNT(*) AS n, ROUND(AVG(s.score), 2) FROM scores s LEFT JOIN decisions d USING (doc_id)"
            f" WHERE {view_where} GROUP BY s.host ORDER BY n DESC LIMIT 40", view_params,
        ).fetchall()
        return web.json_response({"items": [_row_for_page(r) for r in rows], "total": total,
                                  "hosts": [{"host": h, "count": n, "score": s} for h, n, s in hosts],
                                  "counts": review.counts(db)})
    finally:
        db.close()


async def decide(request: web.Request) -> web.Response:
    body = await request.json()
    decision = body.get("decision")
    if decision is not None and decision not in review.DECISIONS:
        raise web.HTTPBadRequest(text="Unknown decision.")
    db = review.connect(request.app["out"])
    try:
        if body.get("host"):  # one decision for a whole group: only what's still undecided in it
            ids = [did for (did,) in db.execute(
                "SELECT s.doc_id FROM scores s LEFT JOIN decisions d USING (doc_id)"
                " WHERE d.doc_id IS NULL AND s.score >= ? AND s.host = ?", (review.FLAGGED, body["host"]))]
        else:
            ids = [d for d in body.get("ids", []) if DOC_ID.match(str(d))]
        n = review.decide(db, ids, decision, str(body.get("note") or "") or None)
        return web.json_response({"changed": n, "counts": review.counts(db)})
    finally:
        db.close()


# -- documents -----------------------------------------------------------------------------------

def _titles(out: Path, db: sqlite3.Connection, ids: list[str], indexed: bool) -> dict[str, str]:
    """From the search index if it's on, since it has every document, then the garbage check's scores."""
    titles = search.titles(out, ids) if indexed else {}
    rest = [did for did in ids if did not in titles]
    if rest:
        titles.update(db.execute(f"SELECT doc_id, title FROM scores WHERE title IS NOT NULL AND doc_id IN"
                                 f" ({','.join('?' * len(rest))})", rest))
    return titles


async def documents(request: web.Request) -> web.Response:
    """The plain list, newest first, filtered by URL or title text: for every status, including
    documents with no text, which the search index doesn't hold."""
    q = request.query
    out = request.app["out"]
    indexed = request.app["index"] is not None
    where, params = ["lane != 'skip'"], []
    for column in ("lane", "status", "reason"):
        if q.get(column):
            where.append(f"{column} = ?")
            params.append(q[column])
    term = q.get("q", "").strip()
    db = review.connect(out)
    try:
        if term:
            titled = set(search.matching_titles(out, term)) if indexed else set()
            titled.update(did for (did,) in db.execute("SELECT doc_id FROM scores WHERE title LIKE ? LIMIT 2000",
                                                       (f"%{term}%",)))
            where.append(f"(url LIKE ? OR doc_id IN ({','.join('?' * len(titled))}))")
            params += [f"%{term}%", *titled]
        offset = max(int(q.get("offset", 0)), 0)
        clause = " AND ".join(where)
        manifest = read_manifest(out)
        try:
            rows = manifest.execute(
                f"SELECT doc_id, MIN(url), lane, status, reason, MAX(chars) FROM urls WHERE {clause} AND doc_id IS NOT NULL"
                " GROUP BY doc_id ORDER BY MIN(rowid) DESC LIMIT ? OFFSET ?", (*params, PAGE_SIZE, offset),
            ).fetchall()
            total = manifest.execute(f"SELECT COUNT(DISTINCT doc_id) FROM urls WHERE {clause}", params).fetchone()[0]
        finally:
            manifest.close()
        titles = _titles(out, db, [r[0] for r in rows], indexed)
    finally:
        db.close()
    return web.json_response({"total": total, "items": [
        {"id": did, "url": url, "lane": lane, "status": status, "reason": reason, "chars": chars,
         "title": titles.get(did), "host": urlsplit(url).hostname or "local file"}
        for did, url, lane, status, reason, chars in rows]})


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


def _scan_text(out: Path, term: str, lane: str, status: str) -> dict:
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
        found = await asyncio.to_thread(_scan_text, out, term, q.get("lane", ""), q.get("status", ""))
        return web.json_response(found)
    if not term:
        raise web.HTTPBadRequest(text="Type something to search for.")
    found = await asyncio.to_thread(search.search, out, term, q.get("lane", ""), q.get("status", ""),
                                    search.PAGE, max(int(q.get("offset", 0)), 0))
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
            "SELECT s.score, s.signals, s.source, d.decision, d.note FROM scores s LEFT JOIN decisions d USING (doc_id)"
            " WHERE s.doc_id = ?", (did,)).fetchone()
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
        "pdf": review.is_pdf_source(scored[2]) if scored else False,
        "decision": scored[3] if scored else None, "note": scored[4] if scored else None,
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

async def _start_index(app: web.Application) -> None:
    app["index_task"] = asyncio.create_task(app["index"].keep(app["job"]))


async def _stop_job_on_exit(app: web.Application) -> None:
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
    app["token"] = load_ui_token(out)
    app["job"] = Job(out)
    app["index"] = Index(out) if search_index else None  # off unless asked for: it takes disk and CPU
    review.connect(out).close()
    if search_index:
        app["job"].on_change = app["index"].wanted.set
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
        web.get("/api/review", review_list),
        web.post("/api/decide", decide),
        web.get("/api/documents", documents),
        web.get("/api/search", search_documents),
        web.get("/api/reasons", reasons),
        web.get("/api/document/{id}", document),
        web.get("/original/{id}", original),
        web.get("/saved/{id}", raw_text),
    ])
    return app


def serve(out: str | os.PathLike, port: int, no_browser: bool, search_index: bool = False) -> None:
    app = make_app(out, search_index)
    address = f"http://127.0.0.1:{port}/?token={app['token']}"

    async def announce(_: web.Application) -> None:
        print(f"\nosiris is open at\n\n  {address}\n\nPress Ctrl-C here to stop it.", flush=True)
        if os.environ.get("SSH_CONNECTION"):
            print(f"Over ssh, first run this on your own machine: ssh -N -L {port}:127.0.0.1:{port} <this server>",
                  flush=True)
        # a terminal browser would take over this window, so only open one where there's a desktop
        desktop = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform == "darwin"
        if desktop and not no_browser and not os.environ.get("SSH_CONNECTION"):
            webbrowser.open(address)

    app.on_startup.append(announce)
    web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None)
