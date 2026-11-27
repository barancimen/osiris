"""A summary of the archive's state written by an LLM: what is waiting, failed or thin, why, and what
can be done about it. It is worked out from detailed counts of the manifest, the page's settings and
the end of the log, and written by DeepSeek's deepseek-flash with DEEPSEEK_API_KEY from .env.

DeepSeek is sent those counts with the sites, reasons and some example links of each, the settings
(whether a cookies file is set, not its path), the end of the log, and README.clank.md, which explains
the reasons and options. No document's text is sent. The manifest is only read, so a summary
can be asked for at any time, during a run too.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from .report import NAMES, RANK

API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
KEY_ENV = "DEEPSEEK_API_KEY"
TIMEOUT = 240  # seconds for one answer: it thinks before it writes
ATTEMPTS = 3
MAX_TOKENS = 16_000  # its thinking counts too
README = Path(__file__).resolve().parent.parent / "README.clank.md"
PAGE = Path(__file__).with_name("ui.html")  # its list of what each failure reason means
HOSTS, EXAMPLES, TOP_HOSTS, LOG_LINES, LINE_CHARS, DAYS = 10, 5, 50, 150, 300, 14
START, END, FAILED = "--- LLM summary of current status", "--- End of the LLM summary", "--- LLM summary failed"
STATUSES = ("pending", "ok", "thin", "failed", "skipped")
STATUS_NAMES = {"pending": "waiting", "ok": "saved", "thin": "thin", "failed": "failed", "skipped": "skipped"}
SETTINGS = (("impersonate", "Retry with a browser's TLS fingerprint"), ("render", "Headless browser pass"),
            ("doi", "DOI lookup for failed papers"), ("wayback", "Wayback Machine pass"),
            ("scry_crawl", "Use Scry's copy of the page"), ("retry_failed", "Try failed URLs again"),
            ("ocr", "OCR"), ("arxiv_fulltext", "arXiv full text"), ("scry_twitter", "Tweets from Scry"),
            ("billed_twitter", "Allow twitterapi.io"), ("scry_arxiv", "arXiv from Scry"), ("scry_youtube", "YouTube from Scry"),
            ("review_after", "Check for garbage after a run"))

SYSTEM = """You read the state of osiris, a program that archives a person's saved links and files as plain-text \
documents, and write its owner a short report on it. You are given detailed counts from its database, its settings, \
the current job and the end of its log, and the parts of its manual that explain the reasons and options.

Report the most important issues first: what is still waiting or failed, why, and what the owner can do about each \
from the osiris page, naming the box or option as the manual does. Weigh how many documents an issue affects and \
whether anything can be done about it. Say plainly when something needs no action, such as links skipped on purpose, \
and when the only remedies are waiting or saving the page by hand. If a job is running, say what it is doing and \
whether anything in the log looks wrong, and otherwise don't mention jobs or the log. Use only numbers, options and facts from the material, and don't guess \
at causes it doesn't support.

Be very concise and to the point: one line on the overall state, then at most six lines starting with "- ", one \
issue each, ranked, each with its number of documents and the remedy, 150 words in all at most. Leave out issues \
that need no action unless they are large enough to worry the owner. No preamble, no closing remarks, no headings, \
bold, tables or code formatting. Plain, ordinary English."""


class Failed(Exception):
    """A summary that couldn't be written, with the reason, for the log."""


def _host(url: str) -> str:
    parts = urlsplit(url)
    return (parts.hostname or "").removeprefix("www.") or parts.scheme or "?"


def _iso(moment: float | None) -> str | None:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _n(k: int) -> str:
    return f"{k:,}"


def _meanings() -> list[tuple[re.Pattern, str]]:
    """What each failure reason means, from the list the page shows on hover."""
    try:
        block = PAGE.read_text(encoding="utf-8").partition("const REASONS = [")[2].partition("\n];")[0]
    except OSError:
        return []
    found = []
    for pattern, meaning in re.findall(r'\[/(.+?)/, "((?:[^"\\]|\\.)*)"\]', block):
        try:
            found.append((re.compile(pattern), meaning.replace('\\"', '"')))
        except re.error:
            pass
    return found


def _manual() -> str:
    try:
        return README.read_text(encoding="utf-8")
    except OSError:
        return ""


def _counted(counter: Counter, top: int = HOSTS) -> str:
    shown = ", ".join(f"{k} {_n(v)}" for k, v in counter.most_common(top))
    return shown + (f", and {_n(len(counter) - top)} more" if len(counter) > top else "")


def _group(items: list[dict], window: tuple[str, str] | None, meaning: str = "") -> str:
    """One line on a set of documents: how many, the sites, the answers they got, where they were sent,
    when they were tried, the fallbacks already run on them, and some of their addresses."""
    times = [it["at"] for it in items if it["at"]]
    bits = [f"{_n(len(items))} documents"]
    if meaning:
        bits.append(f"meaning: {meaning}")
    bits.append(f"{len({it['host'] for it in items})} sites: " + _counted(Counter(it["host"] for it in items)))
    if codes := Counter(it["code"] for it in items if it["code"]):
        bits.append("HTTP status: " + _counted(codes))
    if moved := Counter(it["landed"] for it in items if it["landed"] and it["landed"] != it["host"]):
        bits.append(f"{_n(sum(moved.values()))} were redirected to another site: " + _counted(moved, 5))
    if kinds := Counter(it["type"] for it in items if it["type"]):
        bits.append("content types: " + _counted(kinds, 5))
    if times:
        bits.append(f"last tried between {min(times)[:10]} and {max(times)[:10]}")
        if window:
            bits.append(f"{_n(sum(1 for t in times if window[0] <= t <= window[1]))} in the latest run")
    else:
        bits.append("never tried")
    bits.append("attempts: " + _counted(Counter(f"{min(it['attempts'], 5)}{'+' * (it['attempts'] >= 5)}" for it in items)))
    if tried := Counter(p for it in items for p in it["tried"].split(",") if p):
        bits.append("fallback passes already run on them: " + _counted(tried))
    newest = sorted(items, key=lambda it: it["at"] or "", reverse=True)[:EXAMPLES]
    bits.append("newest examples: " + " ; ".join(it["url"][:160] for it in newest))
    return "; ".join(bits)


def facts(out: Path, extras: dict) -> str:
    """Everything DeepSeek reads, as plain text."""
    db = sqlite3.connect(f"file:{quote(str(out / 'manifest.sqlite'))}?mode=ro", uri=True, timeout=10)
    try:
        # one row per document: the best of its links (a document saved through any link is saved)
        rows = db.execute(
            f"SELECT url, lane, status, reason, attempts, updated_at, tried, chars, http_status, final_url, content_type FROM ("
            f" SELECT *, ROW_NUMBER() OVER (PARTITION BY COALESCE(doc_id, url) ORDER BY {RANK} DESC, updated_at DESC) AS n"
            f" FROM urls) WHERE n = 1").fetchall()
        links = db.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
    except sqlite3.OperationalError as e:
        raise Failed(f"the manifest couldn't be read ({e})") from None
    finally:
        db.close()

    config, run = extras.get("config") or {}, (extras.get("config") or {}).get("last_run") or {}
    window = (_iso(run["started"]), _iso(run.get("finished")) or "9999") if run.get("started") else None
    now = datetime.now(timezone.utc)
    day, week = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_lane: dict[str, Counter] = defaultdict(Counter)
    failed, pending, thin, skipped = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    failed_hosts: dict[str, Counter] = defaultdict(Counter)
    recent: dict[str, Counter] = {"the last 24 hours": Counter(), "the last 7 days": Counter()}
    daily: dict[str, Counter] = defaultdict(Counter)
    host_status: dict[str, Counter] = defaultdict(Counter)
    for url, lane, status, reason, attempts, at, tried, chars, code, final, ctype in rows:
        by_lane[lane][status] += 1
        if at and at >= week:
            recent["the last 7 days"][status] += 1
            if at >= day:
                recent["the last 24 hours"][status] += 1
        if at and at >= (now - timedelta(days=DAYS)).strftime("%Y-%m-%d"):
            daily[at[:10]][status] += 1
        item = {"url": url, "host": _host(url), "attempts": attempts or 0, "at": at, "tried": tried or "", "chars": chars or 0,
                "code": code, "landed": _host(final) if final else "", "type": ctype}
        host_status[item["host"]][status] += 1
        if status == "failed":
            failed[(lane, reason or "unknown")].append(item)
            failed_hosts[item["host"]][reason or "unknown"] += 1
        elif status == "pending":
            pending[lane].append(item)
        elif status == "thin":
            thin[lane].append(item)
        elif status == "skipped":
            skipped[reason or "unknown"].append(item)

    name = lambda lane: "Skipped links (skip)" if lane == "skip" else f"{NAMES.get(lane, lane)} ({lane})"
    lines = [f"Time now: {now:%Y-%m-%d %H:%M} UTC. The manifest has {_n(links)} links, which make {_n(len(rows))} documents.",
             "", "# Documents by source: " + " / ".join(STATUS_NAMES[s] for s in STATUSES)]
    for lane, counts in sorted(by_lane.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"{name(lane)}: " + " / ".join(_n(counts[s]) for s in STATUSES))
    if window:
        lines += ["", f"The latest archive run started {window[0]}" + (f" and ended {window[1]}." if window[1] != "9999" else " and hasn't ended.")]
    for when, counts in recent.items():
        lines.append(f"Documents last changed in {when}: " + (", ".join(f"{STATUS_NAMES.get(s, s)} {_n(k)}" for s, k in counts.most_common()) or "none"))
    lines += ["", f"# Documents by the day they last changed, the last {DAYS} days"]
    lines += [f"{d}: " + ", ".join(f"{STATUS_NAMES.get(s, s)} {_n(k)}" for s, k in daily[d].most_common()) for d in sorted(daily)] or ["None."]

    meanings = _meanings()
    lines += ["", "# Failed documents, by source and reason"]
    for (lane, reason), items in sorted(failed.items(), key=lambda kv: -len(kv[1])):
        meaning = next((m for pattern, m in meanings if pattern.search(reason)), "")
        lines.append(f"{name(lane)}, reason {reason}: " + _group(items, window, meaning))
    if not failed:
        lines.append("None.")
    lines += ["", f"# The {TOP_HOSTS} sites with the most failed documents, with all their documents by status and the reasons they failed"]
    for host, reasons in sorted(failed_hosts.items(), key=lambda kv: -sum(kv[1].values()))[:TOP_HOSTS]:
        counts = host_status[host]
        lines.append(f"{host}: " + ", ".join(f"{STATUS_NAMES[s]} {_n(counts[s])}" for s in STATUSES if counts[s])
                     + "; failed because: " + _counted(reasons, 6))
    lines += ["", "# Documents waiting, by source"]
    lines += [f"{name(lane)}: " + _group(items, window) for lane, items in sorted(pending.items(), key=lambda kv: -len(kv[1]))] or ["None."]
    lines += ["", "# Thin documents (saved, but with little text), by source"]
    for lane, items in sorted(thin.items(), key=lambda kv: -len(kv[1])):
        sizes = sorted(it["chars"] for it in items)
        lines.append(f"{name(lane)}: {_n(sizes[0])} to {_n(sizes[-1])} characters, median {_n(sizes[len(sizes) // 2])}; " + _group(items, window))
    if not thin:
        lines.append("None.")
    lines += ["", "# Skipped links, by reason (osiris skips these on purpose and doesn't fetch them)"]
    lines += [f"{reason}: " + _group(items, None) for reason, items in sorted(skipped.items(), key=lambda kv: -len(kv[1]))] or ["None."]
    lines += ["", "# Garbage check", ", ".join(f"{k} {_n(v)}" for k, v in (extras.get("review") or {}).items()) or "Not run."]
    lines += ["", *settings(config, extras), "", *job(extras)]
    if manual := _manual():
        lines += ["", "# The osiris manual (README.clank.md)", manual]
    return "\n".join(lines)


def settings(config: dict, extras: dict) -> list[str]:
    options = config.get("options") or {}
    lanes = options.get("lanes") or []
    unticked = [lane for lane in NAMES if lane not in lanes and lane not in ("singlefile", "chat")]
    lines = ["# Settings on the page",
             "Sources ticked: " + (", ".join(NAMES.get(lane, lane) for lane in lanes) or "none")
             + ". Not ticked: " + (", ".join(NAMES.get(lane, lane) for lane in unticked) or "none") + "."]
    lines.append("; ".join(f"{label}: {'on' if options.get(key) else 'off'}" for key, label in SETTINGS))
    lines.append(f"Cookies file: {'set' if options.get('cookies') else 'none'}; Chrome profile: {'set' if options.get('chrome_profile') else 'none'};"
                 f" user agent: {'set' if options.get('user_agent') else 'none'}; headless browser installed: {'yes' if extras.get('browser') else 'no'}.")
    if options.get("retry_hosts"):
        lines.append("Sites whose failed links the next run tries again: " + ", ".join(options["retry_hosts"]))
    if options.get("host_limits"):
        lines.append("Slower sites: " + ", ".join(f"{r.get('pattern')} {r.get('at_once')} at a time, {r.get('seconds')} s apart"
                                                  for r in options["host_limits"] if isinstance(r, dict)))
    lines.append("Folders listed: " + ", ".join(f"{key} {len(config.get(key) or [])}" for key in ("backups", "local", "tweets", "chats")))
    keys = extras.get("keys") or {}
    lines.append("API keys found: " + (", ".join(k for k, v in keys.items() if v) or "none")
                 + ". Missing: " + (", ".join(k for k, v in keys.items() if not v) or "none") + ".")
    return lines


def job(extras: dict) -> list[str]:
    j = extras.get("job") or {}
    if not j.get("name"):
        lines = ["# Current job", "No job has run since the page's server started."]
    else:
        state = j.get("state")
        lines = ["# Current job", f"{j['name']}: {state}" + (f", at the step {j['step']}" if j.get("step") and state in ("running", "stopping") else "")
                 + f"; started {_iso(j.get('started'))}" + (f", ended {_iso(j.get('finished'))}" if j.get("finished") else "") + "."]
    tail = log_tail(extras.get("log") or [])
    if tail:
        lines += [f"The last {len(tail)} lines of its log:", *tail]
    return lines


def log_tail(lines: list[str]) -> list[str]:
    """The end of the log, without earlier summaries."""
    kept, inside = [], False
    for line in lines:
        if START in line:
            inside = True
        elif inside and (END in line or FAILED in line):
            inside = False
        elif not inside and line.strip():
            kept.append(line[:LINE_CHARS])
    return kept[-LOG_LINES:]


async def ask(key: str, material: str) -> tuple[str, dict]:
    payload = {"model": MODEL, "max_tokens": MAX_TOKENS,
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": material}]}
    error = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            await asyncio.sleep(3 * attempt)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
                async with session.post(API, json=payload, headers={"Authorization": f"Bearer {key}"}) as r:
                    status, body = r.status, await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            error = f"no answer from DeepSeek ({type(e).__name__})"
            continue
        if status == 200:
            text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if text.strip():
                return text, body.get("usage") or {}
            error = "DeepSeek's answer was empty"
            continue
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        error = f"DeepSeek answered {status}" + (f": {message.replace(key, '...')[:200]}" if message else "")
        if status not in (429, 500, 502, 503, 504):
            break
    raise Failed(error)


def clean(text: str) -> list[str]:
    """The answer as log lines, without markdown it may have used anyway."""
    lines = [re.sub(r"\*\*|__|`", "", re.sub(r"^#+\s*", "", line)).rstrip() for line in text.strip().splitlines()]
    return [line for i, line in enumerate(lines) if line or (i and lines[i - 1])]


async def summarize(out: Path, extras: dict) -> tuple[list[str], dict]:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise Failed(f"it needs a DeepSeek key. Add {KEY_ENV} to .env in the osiris folder and start the page again")
    material = await asyncio.to_thread(facts, Path(out), extras)
    text, usage = await ask(key, material)
    return clean(text), usage
