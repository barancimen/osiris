"""CLI: osiris ui | run URLFILE | import PATH | ocr | review | clean | search QUERY | stats | export"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from collections import Counter
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from . import arxiv, clean, cookies, extract, fixes, review, safety, tuning, youtube
from .arxiv import run_arxiv
from .doi import doi_of, run_doi_pass
from .forums import lesswrong_from_scry, run_hackernews, run_lesswrong
from .generic import DEFAULT_HOST_RULES, HostRule, run_generic
from .github import run_github
from .localfile import run_localfile
from .ocr import DEFAULT_LANG as OCR_LANG, LANES as OCR_LANES, run_ocr, unavailable as ocr_unavailable
from .reddit import run_reddit
from .route import route
from .env import load_env
from .scry import ScryClient
from .singlefile import run_import
from .stackexchange import run_stackexchange
from .store import REFRESH_FINISHED, Store, now_iso
from .twitter import run_twitter
from .youtube import DEFAULT_DELAY as YOUTUBE_DELAY, parse_delay, run_youtube

log = logging.getLogger("osiris")

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_URLFILE = REPO_DIR / "all_urls.md"  # written by extract_urls.py
DEFAULT_OUT = REPO_DIR / "data"

LANES = ("twitter", "arxiv", "stackexchange", "reddit", "hackernews", "lesswrong", "github", "localfile", "generic", "youtube")
STATUSES = ("pending", "ok", "thin", "failed", "skipped")
REPORT_EVERY = 15.0
TEMP_DOWNLOAD = re.compile(r"[0-9a-f]{32}\.pdf")
REFRESH_LANES = {"web": ("generic",), "local": ("localfile",), "both": ("generic", "localfile")}


def lane_counts(store: Store) -> dict[str, Counter]:
    counts: dict[str, Counter] = {}
    for lane, status, n in store.db.execute("SELECT lane, status, COUNT(*) FROM urls GROUP BY lane, status"):
        counts.setdefault(lane, Counter())[status] = n
    return counts


def downloaded_since(store: Store, since: str) -> Counter:
    """Documents saved since a moment, by source, each counted once however many links lead to it."""
    return Counter(dict(store.db.execute("SELECT lane, COUNT(DISTINCT doc_key) FROM urls WHERE status IN ('ok', 'thin')"
                                         " AND updated_at >= ? GROUP BY lane", (since,))))


async def _report(store: Store) -> None:
    loop = asyncio.get_running_loop()
    since, start, last_total = now_iso(), loop.time(), 0
    while True:
        await asyncio.sleep(REPORT_EVERY)
        got = downloaded_since(store, since)
        total = sum(got.values())
        so_far = ", ".join(f"{lane} {got[lane]}" for lane in LANES if got[lane]) or "nothing yet"
        log.info("downloaded this run: %s | %.1f/s, %.0fs elapsed", so_far, (total - last_total) / REPORT_EVERY,
                 loop.time() - start)
        last_total = total


def stop_on_signals() -> None:
    """Ctrl-C/kill cancel the current task as a whole. Left to Python, SIGINT raises
    KeyboardInterrupt in whatever callback happens to be running, and uvloop's SSL callbacks
    swallow it. A second Ctrl-C falls back to the default and quits on the spot."""
    loop, main = asyncio.get_running_loop(), asyncio.current_task()

    def interrupt() -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
            signal.signal(sig, signal.SIG_DFL)
        main.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, interrupt)


async def _run_lanes(store: Store, args: argparse.Namespace, lanes: list[str],
                     refresh: dict[str, list[tuple[str, str, int]]]) -> None:
    browser_cookies = cookies.load(args.cookies) if args.cookies else []
    if args.cookies:
        log.info("using %d browser cookies for %d sites", len(browser_cookies),
                 len({c.domain.lstrip(".") for c in browser_cookies}))
    pool = extract.WorkerPool(args.workers, args.worker_mem_mb)
    reporter = asyncio.create_task(_report(store))
    stop_on_signals()
    jobs = {
        "twitter": lambda: run_twitter(store, use_scry=not args.no_scry_tweets),
        "arxiv": lambda: run_arxiv(store, pool, args.keep_raw_pdfs, full_text=not args.no_arxiv_fulltext,
                                   refetch_pdfs=args.refetch_arxiv_pdfs,
                                   scry_dollars=args.scry_arxiv_max_dollars if args.scry_arxiv else None),
        "stackexchange": lambda: run_stackexchange(store, refetch=args.refetch_stackexchange),
        "reddit": lambda: run_reddit(store),
        "hackernews": lambda: run_hackernews(store, refetch=args.refetch_comments, older_than_days=args.refetch_comments_days),
        "lesswrong": lambda: run_lesswrong(store, refetch=args.refetch_comments, older_than_days=args.refetch_comments_days),
        "github": lambda: run_github(store),
        "youtube": lambda: run_youtube(store, args.youtube_delay, args.scry_youtube_max_dollars if args.scry_youtube else None),
        "localfile": lambda: run_localfile(store, pool, args.workers,
                                           refresh=refresh.get("localfile", ())),
        "generic": lambda: run_generic(store, pool, args.workers, args.concurrency, args.per_host,
                                       args.contact, impersonate=not args.no_impersonate,
                                       keep_raw=args.keep_raw_pdfs, wayback=not args.no_wayback,
                                       crawl=args.scry_crawl, crawl_dollars=args.scry_crawl_max_dollars,
                                       doi=not args.no_doi, rules=host_rules(args.host_limit),
                                       cookies=browser_cookies, browser_ua=args.user_agent, render=args.render,
                                       render_concurrency=args.render_concurrency,
                                       chrome_profile=args.chrome_profile, challenge_wait=args.challenge_wait,
                                       refresh=refresh.get("generic", ()), pdf_at_once=args.pdf_at_once),
    }
    def report_crash(lane: str, task: asyncio.Task) -> None:  # at once: other lanes can run for hours
        if not task.cancelled() and task.exception():
            log.error("%s lane crashed; its documents stay pending", lane, exc_info=task.exception())

    tasks = {lane: asyncio.create_task(jobs[lane]()) for lane in lanes}
    for lane, task in tasks.items():
        task.add_done_callback(partial(report_crash, lane))

    ocr_lanes = [lane for lane in OCR_LANES if lane in tasks]
    ocr = bool(ocr_lanes) and not args.no_ocr
    if ocr and (problem := ocr_unavailable(args.ocr_lang)):
        log.warning("no OCR this run: %s. PDFs with no text wait for a later run", problem)
        ocr = False

    async def after_reading() -> None:
        # after the lanes that find these PDFs, so a long scan holds up nothing; not after YouTube,
        # which can take days
        read = await asyncio.gather(*(tasks[lane] for lane in ocr_lanes), return_exceptions=True)
        if ocr:
            await run_ocr(store, args.ocr_lang, args.workers, args.worker_mem_mb, lanes=ocr_lanes)
        for lane, result in zip(ocr_lanes, read):
            if lane in refresh and not isinstance(result, BaseException):
                store.finish_refresh(lane)

    if ocr_lanes:
        tasks["ocr"] = asyncio.create_task(after_reading())
        tasks["ocr"].add_done_callback(partial(report_crash, "ocr"))

    async def note_youtube_alone() -> None:
        others = [task for lane, task in tasks.items() if lane != "youtube"]
        await asyncio.gather(*others, return_exceptions=True)
        if "youtube" in tasks and not tasks["youtube"].done():
            log.info("only the YouTube lane is left; Ctrl-C is safe, and a re-run picks up where it stopped")

    notice = asyncio.create_task(note_youtube_alone())
    try:
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    finally:
        notice.cancel()
        reporter.cancel()
        store.commit()
        pool.shutdown()


def host_rules(specs: list[str]) -> tuple[HostRule, ...]:
    """--host-limit rules, plus the defaults for patterns they don't override."""
    rules = [HostRule.parse(spec) for spec in specs]
    given = {r.pattern for r in rules}
    return (*rules, *(r for r in DEFAULT_HOST_RULES if r.pattern not in given))


def refuse_real_chrome_profile(path: str) -> None:
    """Chrome rewrites the profile it runs on, and this guard can't see other programs."""
    home = Path.home()
    real = (home / ".config/google-chrome", home / ".config/chromium", home / ".config/BraveSoftware",
            home / "Library/Application Support/Google/Chrome", home / "AppData/Local/Google/Chrome")
    given = Path(path).expanduser().resolve()
    if any(given == r or r in given.parents for r in real):
        sys.exit(f"--chrome-profile needs a copy of your profile, not your real one: {given}")


def cmd_run(args: argparse.Namespace) -> None:
    import uvloop

    if args.chrome_profile:
        refuse_real_chrome_profile(args.chrome_profile)
    tuning.apply_arguments(args)
    lanes = [lane for lane in (args.only or LANES) if lane not in args.skip]
    refresh_lanes = [lane for lane in REFRESH_LANES.get(args.refresh_pdfs, ()) if lane in lanes]
    if args.refresh_pdfs and not refresh_lanes:
        sys.exit(f"--refresh-pdfs {args.refresh_pdfs} needs the {' and '.join(REFRESH_LANES[args.refresh_pdfs])} "
                 "lane in this run, and --only or --skip leaves it out")

    store = Store(args.out)
    for stale in store.tmp_dir.glob("*.pdf"):
        if TEMP_DOWNLOAD.fullmatch(stale.name):
            stale.unlink()
    if args.fixes:
        try:
            uvloop.run(_run_fixes(store, args))
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.warning("interrupted: the next garbage check puts back what a fix didn't finish")
        finally:
            store.commit()
            store.close()
        return

    routes = []
    if Path(args.urlfile) == DEFAULT_URLFILE and not DEFAULT_URLFILE.exists():
        log.info("no %s, so no new URLs: working on what's already in the manifest", DEFAULT_URLFILE)
    else:
        with open(args.urlfile, encoding="utf-8", errors="replace") as f:
            for line in f:
                r = route(line)
                if r is not None:
                    routes.append(r)
    added, known = store.add(routes)
    if args.mark_inputs:  # only now: a file marked before its URLs were saved would never be read again
        with open(args.mark_inputs, encoding="utf-8") as f:
            store.mark_inputs_read(line.rstrip("\n").split("\t", 1) for line in f if line.strip())
    by_lane = Counter(r.lane for r in routes)
    log.info("%d URLs read from %s (%s)", len(routes), args.urlfile,
             ", ".join(f"{lane} {n}" for lane, n in by_lane.most_common()))
    log.info("%d new URLs queued; %d already in the manifest (same dedup key as extract_urls.py)", added, known)

    rerouted = store.reroute(route)
    if rerouted:
        log.info("%d unfinished URLs re-routed by the current rules", rerouted)
    if args.retry_failed or args.retry_host:
        log.info("re-queued %d failed URLs%s", store.requeue(lanes, hosts=args.retry_host),
                 f" on {', '.join(args.retry_host)}" if args.retry_host else "")
    refresh = {lane: store.start_refresh(lane) for lane in refresh_lanes}
    for lane, todo in refresh.items():
        if not todo:  # said in so many words, so that the page stops counting on this refresh
            log.info("%s: %s: no PDFs left to read again; a refresh that was stopped earlier read them all",
                     lane, REFRESH_FINISHED)

    try:
        uvloop.run(_run_lanes(store, args, lanes, refresh))
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("interrupted: progress is saved, re-run the same command to resume")
    finally:
        if ScryClient.spent_nanodollars:
            log.info("scry: $%.4f spent from the wallet this run", ScryClient.spent_nanodollars / 1e9)
        store.commit()
        print_stats(store)
        store.close()


def print_stats(store: Store, failed: bool = False, thin: bool = False) -> None:
    counts = lane_counts(store)
    print(f"\n{'lane':<14}{'total':>8}" + "".join(f"{s:>9}" for s in STATUSES))
    for lane in (*LANES, "singlefile", "skip"):
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


def cmd_import(args: argparse.Namespace) -> None:
    import uvloop

    paths = [Path(p) for p in args.paths]
    if missing := [p for p in paths if not p.exists()]:
        sys.exit(f"not found: {', '.join(map(str, missing))}")

    async def main() -> None:
        stop_on_signals()
        pool = extract.WorkerPool(args.workers, args.worker_mem_mb)
        try:
            await run_import(store, paths, pool, args.workers)
        finally:
            pool.shutdown()

    store = Store(args.out)
    try:
        uvloop.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("interrupted: progress is saved, re-run the same command to resume")
    finally:
        store.commit()
        store.close()


def cmd_ocr(args: argparse.Namespace) -> None:
    import uvloop

    if problem := ocr_unavailable(args.lang):
        sys.exit(problem)

    async def main() -> None:
        stop_on_signals()
        await run_ocr(store, args.lang, args.workers, args.worker_mem_mb, args.retry, args.limit)

    store = Store(args.out)
    try:
        uvloop.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("interrupted: progress is saved, re-run the same command to resume")
    finally:
        store.commit()
        print_stats(store)
        store.close()


BILLED_KEY = "TWITTERAPI_IO_KEY"


async def _run_fixes(store: Store, args: argparse.Namespace) -> None:
    """The fixes queued on the Documents tab, each for its own documents only, with this run's settings."""
    db = review.connect(args.out)
    billed = os.environ.pop(BILLED_KEY, None)  # tweets cost money only through the fix that says so
    # these widen a run to documents nobody queued
    args.refetch_arxiv_pdfs = args.refetch_comments = args.refetch_stackexchange = False
    try:
        ocr_langs: dict[str, list[str]] = {}
        wanted = []
        for key, lane, decision, method in review.queued_fixes(db):
            if decision == "ocr":
                ocr_langs.setdefault(method or args.ocr_lang, []).append(key)
            else:
                wanted.append((key, lane, method or "again"))
        groups: dict[str, list[tuple[str, str]]] = {}
        for key, lane, method in review.prepare_refetch(store, db, wanted, fixes.FETCHED_LIKE_NEW):
            groups.setdefault(method, []).append((key, lane))
        if not groups and not ocr_langs:
            log.info("fixes: nothing queued")
        for method, docs in groups.items():
            keys = sorted({key for key, _ in docs})
            log.info("fixes: %d to fetch again (%s)", len(keys), method)
            store.only = set(keys)
            try:
                await _fix(store, args, method, docs, billed)
            finally:
                store.only = None
                store.commit()
        for lang, keys in ocr_langs.items():
            if problem := ocr_unavailable(lang):
                log.warning("fixes: %d PDFs not read with OCR: %s", len(keys), problem)
                continue
            review.keep_before_ocr(store, db, keys)
            await run_ocr(store, lang, args.workers, args.worker_mem_mb, forced=keys)
            review.mark_ocr_applied(db, keys)
    finally:
        db.close()


async def _fix(store: Store, args: argparse.Namespace, method: str, docs: list[tuple[str, str]],
               billed: str | None) -> None:
    keys = sorted({key for key, _ in docs})
    if method == "again":  # a normal run, seeing only these documents
        await _run_lanes(store, args, sorted({lane for _, lane in docs}), {})
        return
    if method == "twitterapi":
        if not billed:
            log.warning("fixes: %s isn't set, so %d tweets weren't looked up on twitterapi.io", BILLED_KEY, len(keys))
            return
        os.environ[BILLED_KEY] = billed
        try:
            await run_twitter(store, use_scry=False)
        finally:
            os.environ.pop(BILLED_KEY, None)
        return
    if method == "crawl":
        await fixes.from_crawl(store, keys)
        return
    if method == "doi":
        await run_doi_pass(store, args.contact, [(key, -1, doi) for key in keys if (doi := doi_of(key))])
        return
    if method == "scry":
        by_lane: dict[str, list[str]] = {}
        for key, lane in docs:
            by_lane.setdefault(lane, []).append(key)
        if by_lane.get("arxiv"):
            await arxiv.fill_from_scry(store, by_lane["arxiv"], args.scry_arxiv_max_dollars, replace=True)
        if by_lane.get("youtube"):
            videos = [(key, store.db.execute("SELECT MIN(url) FROM urls WHERE doc_key = ?", (key,)).fetchone()[0])
                      for key in by_lane["youtube"]]
            await youtube.fill_from_scry(store, videos, args.scry_youtube_max_dollars)
        if by_lane.get("lesswrong"):
            await lesswrong_from_scry(store, by_lane["lesswrong"])
        return
    pool = extract.WorkerPool(args.workers, args.worker_mem_mb)
    try:
        if method == "html":  # arXiv's full text again, its web version first
            store.db.executemany("UPDATE urls SET tried = replace(tried, ?, '') WHERE doc_key = ?",
                                 ((f",{arxiv.FULLTEXT}", key) for key in keys))
            await arxiv.add_full_text(store, pool, args.keep_raw_pdfs)
        else:  # impersonate, render or wayback first; then the other ways, and OCR, as in a run
            await run_generic(store, pool, args.workers, args.concurrency, args.per_host, args.contact,
                              impersonate=not args.no_impersonate, keep_raw=args.keep_raw_pdfs,
                              wayback=not args.no_wayback, doi=not args.no_doi,
                              crawl=args.scry_crawl, crawl_dollars=args.scry_crawl_max_dollars,
                              rules=host_rules(args.host_limit),
                              cookies=cookies.load(args.cookies) if args.cookies else [],
                              browser_ua=args.user_agent, render=args.render,
                              render_concurrency=args.render_concurrency,
                              chrome_profile=args.chrome_profile, challenge_wait=args.challenge_wait, via=method)
            if not args.no_ocr and not (problem := ocr_unavailable(args.ocr_lang)):
                await run_ocr(store, args.ocr_lang, args.workers, args.worker_mem_mb, lanes=["generic"])
            elif not args.no_ocr:
                log.warning("fixes: PDFs with no text weren't read with OCR: %s", problem)
    finally:
        pool.shutdown()


def cmd_review(args: argparse.Namespace) -> None:
    store = Store(args.out)
    try:
        for name, n in review.scan(store, args.out).items():
            print(f"{n:>8}  {name.replace('_', ' ')}")
    finally:
        store.close()


def cmd_clean(args: argparse.Namespace) -> None:
    from . import cleanup

    store = Store(args.out)
    try:
        if args.undo:
            restored, skipped = cleanup.undo(store)
            print(f"put back {restored} original document{'' if restored == 1 else 's'}"
                  + (f"; left {skipped} alone, since they've changed since they were cleaned" if skipped else ""))
            return
        totals = cleanup.clean_archive(store, dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.warning("interrupted: every finished document is saved, and running it again carries on")
        sys.exit(130)
    finally:
        store.close()
    print(f"\n{totals.checked} documents checked, {totals.changed} "
          + ("would change" if args.dry_run else "changed")
          + (f"; finished {totals.finished} an interrupted run had cleaned" if totals.finished else ""))
    for rule, n in sorted(totals.rules.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>8}  {rule}")
    print(f"\nevery change is listed in {Path(args.out) / cleanup.REPORT_FILE}"
          + ("" if args.dry_run else "; `osiris clean --undo` puts the originals back"))


def cmd_ui(args: argparse.Namespace) -> None:
    from .ui import serve

    serve(args.out, args.port, args.search_index)


def refresh_search_index(out: str | Path) -> dict[str, int]:
    from . import search

    last = [time.monotonic()]

    def progress(done: int, total: int) -> None:
        if time.monotonic() - last[0] > 5 and done < total:
            last[0] = time.monotonic()
            log.info("search index: %d of %d documents checked", done, total)

    result = search.refresh(out, progress)
    if result["indexed"] or result["removed"]:
        log.info("search index: %d documents added or updated, %d removed; %d in all",
                 result["indexed"], result["removed"], result["documents"])
    return result


def cmd_search(args: argparse.Namespace) -> None:
    from . import search

    refresh_search_index(args.out)
    found = search.search(args.out, " ".join(args.query), lane=args.lane or "", limit=args.limit)
    for item in found["items"]:
        text = "".join(piece for piece, _ in item.pop("snippet") or ())
        if args.json:
            print(json.dumps({**item, "snippet": text}, ensure_ascii=False))
        else:
            print(f"\n{item['title'] or item['url']}\n  {item['url']}  [{item['lane']}, {item['chars'] or 0:,} chars]"
                  f"\n  {text}")
    if not args.json:
        shown = len(found["items"])
        print(f"\n{found['total']:,} matching documents" + (f", best {shown} shown" if shown < found["total"] else ""))


def cmd_stats(args: argparse.Namespace) -> None:
    store = Store(args.out)
    print_stats(store, failed=args.failed, thin=args.thin)
    store.close()


def refuse_to_overwrite(path: Path) -> None:
    """Replacing an earlier export is fine. Anything else at that path may be yours."""
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline()
        ours = path.is_file() and (not first or "text" in json.loads(first))
    except (OSError, ValueError, TypeError):
        ours = False
    if not ours:
        sys.exit(f"{path} already exists and isn't an earlier export, so it won't be overwritten. Choose another path.")


def cmd_export(args: argparse.Namespace) -> None:
    store = Store(args.out)
    out = Path(args.output) if args.output else store.out / "corpus.jsonl"
    refuse_to_overwrite(out)
    tmp = out.with_suffix(out.suffix + ".tmp")
    n = left_out = 0
    excluded = review.excluded(store.out)
    with open(tmp, "w", encoding="utf-8") as f:
        for did, lane, urls in store.done_docs():
            doc = store.read_doc(did)
            if doc is None:
                continue
            meta, body = doc
            if did in excluded and excluded[did] == review.text_hash(body):
                left_out += 1
                continue
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
    print(f"wrote {n} documents to {out}" + (f", leaving out {left_out} marked bad in review" if left_out else ""))


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(prog="osiris", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="fetch and extract every pending URL (resumable)")
    run.add_argument("urlfile", nargs="?", default=DEFAULT_URLFILE,
                     help=f"text file with one URL per line (default: {DEFAULT_URLFILE}, from extract_urls.py)")
    run.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    run.add_argument("--fixes", action="store_true",
                     help="instead of the links waiting, carry out the fixes queued on the Documents tab, for those "
                          "documents only, with this run's other options")
    run.add_argument("--mark-inputs", metavar="FILE",
                     help="once the URLs are queued, record the backup files listed here (from extract_urls.py "
                          "--hashes-out) as read, so that later runs can skip them if they haven't changed")
    run.add_argument("--concurrency", type=int, default=tuning.FLAG_DEFAULTS["concurrency"],
                     help="max requests in flight (default: %(default)s)")
    run.add_argument("--per-host", type=int, default=tuning.FLAG_DEFAULTS["per_host"],
                     help="max requests in flight per host (default: %(default)s)")
    run.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="extraction processes (default: CPUs)")
    run.add_argument("--worker-mem-mb", type=int, default=tuning.FLAG_DEFAULTS["worker_mem_mb"],
                     help="memory cap per extraction process; a document past it fails instead of swapping "
                          "the machine (default %(default)s, 0 = none)")
    run.add_argument("--only", choices=LANES, action="append", help="run only this lane (repeatable)")
    run.add_argument("--skip", choices=LANES, action="append", default=[],
                     help="don't run this lane; its URLs stay pending (repeatable), e.g. --skip twitter")
    run.add_argument("--youtube-delay", type=parse_delay, default=YOUTUBE_DELAY, metavar="MIN-MAX",
                     help=f"seconds between YouTube requests, at random in this range (default {YOUTUBE_DELAY[0]:g}-"
                          f"{YOUTUBE_DELAY[1]:g}); it's the lane most likely to get your IP blocked")
    run.add_argument("--refetch-stackexchange", action="store_true",
                     help="update archived Stack Exchange threads to include comments; resumable, and a question "
                          "deleted since keeps its document")
    run.add_argument("--refetch-comments", action="store_true",
                     help="fetch every archived Hacker News and LessWrong thread again with all its comments, including "
                          "pages saved before those lanes existed. A document changes only if the new text is at least "
                          "as long. Repeat it any time to pick up new comments")
    run.add_argument("--refetch-comments-days", type=float, default=0, metavar="DAYS",
                     help="with --refetch-comments, leave out threads these lanes fetched in the last DAYS days. "
                          "Pages saved before those lanes existed are always included (default 0: every thread)")
    run.add_argument("--no-ocr", action="store_true",
                     help="don't read PDFs with no text layer with tesseract once the generic and local-file lanes "
                          "are done; they wait for a later run or `osiris ocr`")
    run.add_argument("--ocr-lang", default=OCR_LANG,
                     help=f"tesseract languages for those PDFs, e.g. eng+tur (default {OCR_LANG})")
    run.add_argument("--refresh-pdfs", choices=REFRESH_LANES,
                     help="read every PDF again, failed or not: web ones, local ones or both. A PDF keeps its text "
                          "unless the new copy has more. An interrupted refresh resumes with the same command")
    run.add_argument("--retry-failed", action="store_true", help="re-queue previously failed URLs")
    run.add_argument("--retry-host", action="append", default=[], metavar="PATTERN",
                     help="re-queue only failed URLs on hosts matching the glob (repeatable), e.g. "
                          "--retry-host 'scholar.google.*' after adding --cookies")
    run.add_argument("--no-impersonate", action="store_true", help="skip the curl_cffi retry pass")
    run.add_argument("--no-doi", action="store_true", help="skip the DOI (OpenAlex abstract) fallback pass")
    run.add_argument("--no-wayback", action="store_true", help="skip the Wayback Machine fallback pass")
    run.add_argument("--render", action="store_true",
                     help="headless-browser fallback pass for blocked/thin pages (needs: pip install -e .[render] "
                          "&& playwright install chromium; a few hundred MB of RAM per page)")
    run.add_argument("--render-concurrency", type=int, default=tuning.FLAG_DEFAULTS["render_concurrency"],
                     help="pages open at once in --render (default %(default)s)")
    run.add_argument("--chrome-profile", metavar="DIR",
                     help="--render with a COPY of your Chrome profile directory (uses installed Chrome) so its "
                          "logged-in sessions apply; Chrome must not have that copy open")
    run.add_argument("--challenge-wait", type=float, default=tuning.FLAG_DEFAULTS["challenge_wait"],
                     help="seconds --render waits for bot-challenge pages to clear (default %(default)s)")
    run.add_argument("--host-limit", action="append", default=[], metavar="PATTERN=N[:SECONDS]",
                     help="hosts matching the glob share one queue: N requests in flight, SECONDS between "
                          "starts (repeatable; default scholar.google.*=1:20)")
    run.add_argument("--keep-raw-pdfs", action="store_true",
                     help="also keep originals under <out>/raw/, named after their documents: PDFs from the web "
                          "(~2 MB each), and arXiv papers' HTML, or their PDF if they have no HTML. Web pages and "
                          "your local files aren't kept")
    run.add_argument("--refetch-arxiv-pdfs", action="store_true",
                     help="give arXiv papers whose full text came from a PDF another look at their HTML, once each. "
                          "The HTML replaces the PDF text unless it's less than half as long")
    run.add_argument("--scry-arxiv", action="store_true",
                     help="take an arXiv paper's full text from Scry when it has one, and don't ask arXiv for that paper. "
                          "Much faster, but Scry's copy drops formulas. Sends your paper ids to Scry")
    run.add_argument("--scry-arxiv-max-dollars", type=float, default=5.0, metavar="N",
                     help="stop taking papers from Scry after spending this much in a run; the rest wait (default 5)")
    run.add_argument("--scry-crawl", action="store_true",
                     help="after every other fallback, take Scry's crawl copy of web pages still failed or thin; this "
                          "sends their addresses to Scry")
    run.add_argument("--scry-crawl-max-dollars", type=float, default=1.0, metavar="N",
                     help="with --scry-crawl, stop once a run has spent this much on it (default %(default)s)")
    run.add_argument("--scry-youtube", action="store_true",
                     help="take a video's transcript from Scry when it has one, so that video never touches YouTube. "
                          "Sends your video ids to Scry")
    run.add_argument("--scry-youtube-max-dollars", type=float, default=1.0, metavar="N",
                     help="stop taking transcripts from Scry after spending this much in a run; the rest come from YouTube (default 1)")
    run.add_argument("--pdf-at-once", type=int, metavar="N",
                     help="web PDFs being downloaded or read at once (default: twice --workers, at least 3)")
    run.add_argument("--no-arxiv-fulltext", action="store_true",
                     help="arXiv papers keep only their abstracts this run (the full text takes 15 seconds a paper)")
    run.add_argument("--cookies", metavar="FILE",
                     help="your browser's cookies (Netscape cookies.txt or JSON export), each sent only to its "
                          "own site: for Scholar, Medium, logged-in and bot-walled pages")
    run.add_argument("--user-agent", help="browser User-Agent to send; match the browser the cookies came from")
    run.add_argument("--contact", help="email/URL appended to the User-Agent sent to Wikimedia sites")
    run.add_argument("--no-scry-tweets", action="store_true",
                     help="don't look tweets up in Scry: only twitterapi.io, if keyed, and otherwise they stay pending")
    tuning.add_arguments(run)
    run.set_defaults(func=cmd_run)

    imp = sub.add_parser("import", help="store HTML pages saved with SingleFile as markdown (resumable)")
    imp.add_argument("paths", nargs="+", help="directories (searched recursively for .html/.htm) or files")
    imp.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    imp.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="extraction processes (default: CPUs)")
    imp.add_argument("--worker-mem-mb", type=int, default=1536, help="memory cap per extraction process (0 = none)")
    imp.set_defaults(func=cmd_import)

    ocr = sub.add_parser("ocr", help="read PDFs with no text layer (scans) with tesseract; slow, resumable")
    ocr.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    ocr.add_argument("--lang", default=OCR_LANG, help=f"tesseract languages, e.g. eng+tur (default {OCR_LANG})")
    ocr.add_argument("--workers", type=int, default=1, help="pages at a time, one CPU core each (default 1)")
    ocr.add_argument("--worker-mem-mb", type=int, default=1536, help="memory cap per OCR process (0 = none)")
    ocr.add_argument("--retry", action="store_true", help="also try PDFs OCR has already tried, e.g. in a new language")
    ocr.add_argument("--limit", type=int, help="stop after this many PDFs")
    ocr.set_defaults(func=cmd_ocr)

    ui = sub.add_parser("ui", help="open the web interface")
    ui.add_argument("--out", default=DEFAULT_OUT)
    ui.add_argument("--port", type=int, default=8321, help="local port (default 8321)")
    ui.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # the page never opens by itself now
    ui.add_argument("--search-index", action="store_true",
                    help="keep a full-text index (data/search.sqlite) up to date in the background, for ranked "
                         "search of titles, URLs and text; without it, text search scans the documents")
    ui.set_defaults(func=cmd_ui)

    aud = sub.add_parser("review", help="score documents for probable garbage and settle queued fixes (scan); "
                                        "`osiris run --fixes` carries the fixes out")
    aud.add_argument("action", choices=("scan",))
    aud.add_argument("--out", default=DEFAULT_OUT)
    aud.set_defaults(func=cmd_review)

    find = sub.add_parser("search", help="full-text search of the archived documents, best matches first")
    find.add_argument("query", nargs="+",
                      help='words that must all appear; "quoted words" together; -word must not appear, '
                           "so quote a query that has one: osiris search 'transformer -attention'")
    find.add_argument("--out", default=DEFAULT_OUT)
    find.add_argument("--lane", choices=(*LANES, "singlefile"), help="only documents from this lane")
    find.add_argument("--limit", type=int, default=20, help="matches to show (default 20)")
    find.add_argument("--json", action="store_true", help="one JSON object per match, for other tools")
    find.set_defaults(func=cmd_search)

    cln = sub.add_parser("clean", help="repair characters and line-end hyphens that conversion broke, in "
                                       "documents already archived (only changed files are rewritten)")
    cln.add_argument("--out", default=DEFAULT_OUT)
    cln.add_argument("--dry-run", action="store_true",
                     help="change nothing: only list what would change in <out>/clean-report.tsv")
    cln.add_argument("--undo", action="store_true",
                     help="put back the originals of documents cleaned before, unless they've changed since")
    cln.set_defaults(func=cmd_clean)

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
    # everything below may run third-party parsers on your files
    safety.install(args.out, *([Path(args.output).resolve().parent] if getattr(args, "output", None) else []))
    # for the extraction workers, which repair line-end hyphens with the archive's own vocabulary
    os.environ[clean.LEXICON_ENV] = str(Path(args.out).resolve() / clean.LEXICON_FILE)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    args.func(args)


if __name__ == "__main__":
    main()
