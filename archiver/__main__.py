"""CLI: archiver run URLFILE | archiver stats | archiver export"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from . import cookies, extract
from .arxiv import run_arxiv
from .generic import DEFAULT_HOST_RULES, HostRule, run_generic
from .reddit import run_reddit
from .route import route
from .stackexchange import run_stackexchange
from .store import Store
from .twitter import run_twitter

log = logging.getLogger("archiver")

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_URLFILE = REPO_DIR / "all_urls.md"  # written by extract_urls.py
DEFAULT_OUT = REPO_DIR / "data"

LANES = ("twitter", "arxiv", "stackexchange", "reddit", "generic")
STATUSES = ("pending", "ok", "thin", "failed", "skipped")
REPORT_EVERY = 15.0


def lane_counts(store: Store) -> dict[str, Counter]:
    counts: dict[str, Counter] = {}
    for lane, status, n in store.db.execute("SELECT lane, status, COUNT(*) FROM urls GROUP BY lane, status"):
        counts.setdefault(lane, Counter())[status] = n
    return counts


async def _report(store: Store) -> None:
    loop = asyncio.get_running_loop()
    start, last_done = loop.time(), None
    while True:
        await asyncio.sleep(REPORT_EVERY)
        counts = lane_counts(store)
        parts = []
        for lane in LANES:
            c = counts.get(lane)
            if c and c["pending"]:
                total = sum(c.values())
                parts.append(f"{lane} {total - c['pending']}/{total} (ok {c['ok']}, thin {c['thin']}, failed {c['failed']})")
        done = sum(c[s] for c in counts.values() for s in ("ok", "thin", "failed"))
        if last_done is not None:
            rate = (done - last_done) / REPORT_EVERY
            parts.append(f"{rate:.1f} URL/s, {loop.time() - start:.0f}s elapsed")
        last_done = done
        if parts:
            log.info(" | ".join(parts))


async def _run_lanes(store: Store, args: argparse.Namespace, lanes: list[str]) -> None:
    browser_cookies = cookies.load(args.cookies) if args.cookies else []
    if args.cookies:
        log.info("using %d browser cookies for %d sites", len(browser_cookies),
                 len({c.domain.lstrip(".") for c in browser_cookies}))
    pool = extract.WorkerPool(args.workers, args.worker_mem_mb)
    reporter = asyncio.create_task(_report(store))
    jobs = {
        "twitter": lambda: run_twitter(store),
        "arxiv": lambda: run_arxiv(store),
        "stackexchange": lambda: run_stackexchange(store),
        "reddit": lambda: run_reddit(store),
        "generic": lambda: run_generic(store, pool, args.workers, args.concurrency, args.per_host,
                                       args.contact, impersonate=not args.no_impersonate,
                                       keep_raw=args.keep_raw, wayback=not args.no_wayback,
                                       doi=not args.no_doi, rules=host_rules(args.host_limit),
                                       cookies=browser_cookies, browser_ua=args.user_agent, render=args.render,
                                       render_concurrency=args.render_concurrency,
                                       chrome_profile=args.chrome_profile, challenge_wait=args.challenge_wait),
    }
    try:
        results = await asyncio.gather(*(jobs[lane]() for lane in lanes), return_exceptions=True)
        for lane, result in zip(lanes, results):
            if isinstance(result, BaseException):
                log.error("%s lane crashed", lane, exc_info=result)
    finally:
        reporter.cancel()
        store.commit()
        pool.shutdown()


def host_rules(specs: list[str]) -> tuple[HostRule, ...]:
    """--host-limit rules, plus the defaults for patterns they don't override."""
    rules = [HostRule.parse(spec) for spec in specs]
    given = {r.pattern for r in rules}
    return (*rules, *(r for r in DEFAULT_HOST_RULES if r.pattern not in given))


def cmd_run(args: argparse.Namespace) -> None:
    import uvloop

    store = Store(args.out)
    for stale in store.tmp_dir.glob("*.pdf"):
        stale.unlink()

    routes = []
    with open(args.urlfile, encoding="utf-8", errors="replace") as f:
        for line in f:
            r = route(line)
            if r is not None:
                routes.append(r)
    added, known = store.add(routes)
    by_lane = Counter(r.lane for r in routes)
    log.info("%d URLs read from %s (%s)", len(routes), args.urlfile,
             ", ".join(f"{lane} {n}" for lane, n in by_lane.most_common()))
    log.info("%d new URLs queued; %d already in the manifest (same dedup key as extract_urls.py)", added, known)

    rerouted = store.reroute(route)
    if rerouted:
        log.info("%d unfinished URLs re-routed by the current rules", rerouted)
    lanes = [lane for lane in (args.only or LANES) if lane not in args.skip]
    if args.retry_failed:
        log.info("re-queued %d failed URLs", store.requeue(lanes))

    try:
        uvloop.run(_run_lanes(store, args, lanes))
    except KeyboardInterrupt:
        log.warning("interrupted: progress is saved, re-run the same command to resume")
    finally:
        store.commit()
        print_stats(store)
        store.close()


def print_stats(store: Store, failed: bool = False, thin: bool = False) -> None:
    counts = lane_counts(store)
    print(f"\n{'lane':<14}{'total':>8}" + "".join(f"{s:>9}" for s in STATUSES))
    for lane in (*LANES, "skip"):
        c = counts.get(lane)
        if c:
            print(f"{lane:<14}{sum(c.values()):>8}" + "".join(f"{c[s]:>9}" for s in STATUSES))
    docs, chars = store.db.execute(
        "SELECT COUNT(DISTINCT doc_id), COALESCE(SUM(chars), 0) FROM"
        " (SELECT doc_id, MAX(chars) AS chars FROM urls WHERE status IN ('ok','thin') GROUP BY doc_id)"
    ).fetchone()
    print(f"\n{docs} documents, {chars / 1e6:.1f}M characters of text")

    reasons = store.db.execute(
        "SELECT status, reason, COUNT(*) FROM urls WHERE status IN ('failed','skipped','thin')"
        " GROUP BY status, reason ORDER BY COUNT(*) DESC LIMIT 25"
    ).fetchall()
    if reasons:
        print("\nreasons:")
        for status, reason, n in reasons:
            print(f"  {n:>7}  {status:<8} {reason}")

    for flag, status in ((failed, "failed"), (thin, "thin")):
        if not flag:
            continue
        hosts = Counter()
        host_reasons: dict[str, Counter] = {}
        for url, reason in store.db.execute("SELECT url, reason FROM urls WHERE status = ?", (status,)):
            host = urlsplit(url).hostname or ""
            hosts[host] += 1
            host_reasons.setdefault(host, Counter())[reason] += 1
        print(f"\ntop {status} hosts:")
        for host, n in hosts.most_common(30):
            top = ", ".join(f"{r} {k}" for r, k in host_reasons[host].most_common(3))
            print(f"  {n:>6}  {host:<40} {top}")


def cmd_stats(args: argparse.Namespace) -> None:
    store = Store(args.out)
    print_stats(store, failed=args.failed, thin=args.thin)
    store.close()


def cmd_export(args: argparse.Namespace) -> None:
    store = Store(args.out)
    out = Path(args.output) if args.output else store.out / "corpus.jsonl"
    tmp = out.with_suffix(out.suffix + ".tmp")
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for did, lane, urls in store.done_docs():
            doc = store.read_doc(did)
            if doc is None:
                continue
            meta, body = doc
            record = {
                "id": did,
                "url": meta.get("url"),
                "urls": urls,
                "lane": lane,
                "source": meta.get("source"),
                "title": meta.get("title"),
                "author": meta.get("author"),
                "date": meta.get("date"),
                "site": meta.get("site"),
                "text": body,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, out)
    store.close()
    print(f"wrote {n} documents to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="archiver", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="fetch and extract every pending URL (resumable)")
    run.add_argument("urlfile", nargs="?", default=DEFAULT_URLFILE,
                     help=f"text file with one URL per line (default: {DEFAULT_URLFILE}, from extract_urls.py)")
    run.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    run.add_argument("--concurrency", type=int, default=64, help="max requests in flight (default: 64)")
    run.add_argument("--per-host", type=int, default=4, help="max requests in flight per host (default: 4)")
    run.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="extraction processes (default: CPUs)")
    run.add_argument("--worker-mem-mb", type=int, default=1536,
                     help="memory cap per extraction process; a document past it fails instead of swapping "
                          "the machine (default 1536, 0 = none)")
    run.add_argument("--only", choices=LANES, action="append", help="run only this lane (repeatable)")
    run.add_argument("--skip", choices=LANES, action="append", default=[],
                     help="don't run this lane; its URLs stay pending (repeatable), e.g. --skip twitter")
    run.add_argument("--retry-failed", action="store_true", help="re-queue previously failed URLs")
    run.add_argument("--no-impersonate", action="store_true", help="skip the curl_cffi retry pass")
    run.add_argument("--no-doi", action="store_true", help="skip the DOI (OpenAlex abstract) fallback pass")
    run.add_argument("--no-wayback", action="store_true", help="skip the Wayback Machine fallback pass")
    run.add_argument("--render", action="store_true",
                     help="headless-browser fallback pass for blocked/thin pages (needs: pip install -e .[render] "
                          "&& playwright install chromium; a few hundred MB of RAM per page)")
    run.add_argument("--render-concurrency", type=int, default=2, help="pages open at once in --render (default 2)")
    run.add_argument("--chrome-profile", metavar="DIR",
                     help="--render with a COPY of your Chrome profile directory (uses installed Chrome) so its "
                          "logged-in sessions apply; Chrome must not have that copy open")
    run.add_argument("--challenge-wait", type=float, default=15.0,
                     help="seconds --render waits for bot-challenge pages to clear (default 15)")
    run.add_argument("--host-limit", action="append", default=[], metavar="PATTERN=N[:SECONDS]",
                     help="hosts matching the glob share one queue: N requests in flight, SECONDS between "
                          "starts (repeatable; default scholar.google.*=1:8)")
    run.add_argument("--keep-raw", action="store_true",
                     help="also keep fetched pages under <out>/raw/: HTML as .html.zst (roughly 1-1.5 GB for "
                          "~20k pages), PDFs as-is (~2 MB each)")
    run.add_argument("--cookies", metavar="FILE",
                     help="your browser's cookies (Netscape cookies.txt or JSON export), each sent only to its "
                          "own site: for Scholar, Medium, logged-in and bot-walled pages")
    run.add_argument("--user-agent", help="browser User-Agent to send; match the browser the cookies came from")
    run.add_argument("--contact", help="email/URL appended to the User-Agent sent to Wikimedia sites")
    run.set_defaults(func=cmd_run)

    stats = sub.add_parser("stats", help="summarize the manifest")
    stats.add_argument("--out", default=DEFAULT_OUT)
    stats.add_argument("--failed", action="store_true", help="break failures down by host")
    stats.add_argument("--thin", action="store_true", help="break thin pages down by host")
    stats.set_defaults(func=cmd_stats)

    export = sub.add_parser("export", help="write all documents to a JSONL corpus")
    export.add_argument("--out", default=DEFAULT_OUT)
    export.add_argument("-o", "--output", help="output path (default: <out>/corpus.jsonl)")
    export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    args.func(args)


if __name__ == "__main__":
    main()
