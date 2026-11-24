#!/usr/bin/env python3
"""
Deprecated. osiris now takes an account's tweets itself: put usernames in a .txt file,
one per line, in a folder listed under Tweets on its page, or pass the file to
`osiris run --twitter-accounts`. This script is no longer kept up, and osiris still
imports the databases it wrote.

Fetch all tweets (including replies) from a Twitter account via twitterapi.io.
Stores in SQLite inside a directory named after the username.

Subsequent runs auto-detect state:
  - If backfill is complete: fetches newest tweets first, stops at known data.
  - If backfill was interrupted (--max-pages): catches up on newest tweets,
    then resumes the backfill from the saved cursor.

Usage:
    python fetch_tweets.py <username>                # fetch/update all tweets
    python fetch_tweets.py <username> --max-pages 3  # test: only N pages
    python fetch_tweets.py <username> --stats        # show DB stats, no fetch
    python fetch_tweets.py <username> --db my.db     # custom DB path (overrides dir)

Pricing: $0.15 per 1,000 tweets returned via this endpoint.
"""

import argparse
import os
import sqlite3
import sys
import time
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _api_key() -> str:
    """TWITTERAPI_IO_KEY from the environment, or else from the .env file beside this script."""
    key = os.environ.get("TWITTERAPI_IO_KEY", "")
    if key:
        return key
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8") as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name.strip() == "TWITTERAPI_IO_KEY":
                    key = value.strip().strip("'\"")
    except OSError:
        pass
    return key


API_KEY = _api_key()
BASE_URL = "https://api.twitterapi.io"

# How long to sleep between paginated requests (seconds).
PAGE_DELAY = 0.5

# Retry config for transient failures.
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0  # multiplicative backoff base (seconds)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _user_dir(username: str) -> str:
    """Return the directory for a given username (creates if needed)."""
    d = username
    os.makedirs(d, exist_ok=True)
    return d


def _default_db_path(username: str) -> str:
    return os.path.join(_user_dir(username), "tweets.db")


def _cursor_file_path(username: str) -> str:
    return os.path.join(_user_dir(username), f"{username}.cursor")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tweets (
            id              TEXT PRIMARY KEY,
            text            TEXT,
            created_at      TEXT,
            like_count      INTEGER DEFAULT 0,
            reply_count     INTEGER DEFAULT 0,
            retweet_count   INTEGER DEFAULT 0,
            quote_count     INTEGER DEFAULT 0,
            view_count      INTEGER DEFAULT 0,
            bookmark_count  INTEGER DEFAULT 0,
            url             TEXT,
            is_reply        INTEGER DEFAULT 0,
            in_reply_to_id  TEXT,
            conversation_id TEXT,
            lang            TEXT,
            source          TEXT,
            author_id       TEXT,
            author_name     TEXT,
            author_username TEXT,
            retweeted_text  TEXT,
            fetched_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_tweets_created
            ON tweets(created_at);

        CREATE TABLE IF NOT EXISTS fetch_state (
            username    TEXT PRIMARY KEY,
            last_cursor TEXT,
            total_fetched INTEGER DEFAULT 0,
            backfill_complete INTEGER DEFAULT 0,
            updated_at  TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # Migrations for pre-existing DBs
    tweet_cols = {r[1] for r in conn.execute("PRAGMA table_info(tweets)")}
    if "retweeted_text" not in tweet_cols:
        conn.execute("ALTER TABLE tweets ADD COLUMN retweeted_text TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fetch_state)")}
    if "backfill_complete" not in cols:
        conn.execute("ALTER TABLE fetch_state ADD COLUMN backfill_complete INTEGER DEFAULT 0")
    conn.commit()


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT id FROM tweets")}


def insert_tweets(conn: sqlite3.Connection, tweets: list[dict]) -> int:
    """Insert tweets. Returns count of actually new rows inserted."""
    sql = """
        INSERT OR IGNORE INTO tweets
            (id, text, created_at, like_count, reply_count, retweet_count,
             quote_count, view_count, bookmark_count, url, is_reply,
             in_reply_to_id, conversation_id, lang, source,
             author_id, author_name, author_username, retweeted_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    count = 0
    for t in tweets:
        author = t.get("author") or {}
        cursor = conn.execute(
            sql,
            (
                t["id"],
                t.get("text"),
                t.get("createdAt"),
                t.get("likeCount", 0),
                t.get("replyCount", 0),
                t.get("retweetCount", 0),
                t.get("quoteCount", 0),
                t.get("viewCount", 0),
                t.get("bookmarkCount", 0),
                t.get("url"),
                1 if t.get("isReply") else 0,
                t.get("inReplyToId"),
                t.get("conversationId"),
                t.get("lang"),
                t.get("source"),
                author.get("id"),
                author.get("name"),
                author.get("userName"),
                (t.get("retweeted_tweet") or {}).get("text"),
            ),
        )
        if cursor.rowcount > 0:
            count += 1
    conn.commit()
    return count


def write_cursor_file(username: str, cursor: str) -> None:
    """Atomically write the resume cursor to a file."""
    tmp = _cursor_file_path(username) + ".tmp"
    with open(tmp, "w") as f:
        f.write(cursor)
    os.replace(tmp, _cursor_file_path(username))


def delete_cursor_file(username: str) -> None:
    """Remove the cursor file (backfill complete)."""
    cf = _cursor_file_path(username)
    if os.path.exists(cf):
        os.remove(cf)


def save_cursor(conn: sqlite3.Connection, username: str, cursor: str) -> None:
    """Save progress cursor (after tweets for that page are committed)."""
    conn.execute(
        """
        INSERT INTO fetch_state (username, last_cursor, total_fetched, updated_at)
        VALUES (?, ?, 0, datetime('now'))
        ON CONFLICT(username) DO UPDATE SET
            last_cursor = excluded.last_cursor,
            updated_at = excluded.updated_at
        """,
        (username, cursor),
    )
    conn.commit()
    write_cursor_file(username, cursor)


def save_final_state(
    conn: sqlite3.Connection, username: str, cursor: str,
    total_new: int, backfill_complete: bool = False,
) -> None:
    """Save final state after a run finishes."""
    conn.execute(
        """
        INSERT INTO fetch_state (username, last_cursor, total_fetched, backfill_complete, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(username) DO UPDATE SET
            last_cursor = excluded.last_cursor,
            total_fetched = total_fetched + excluded.total_fetched,
            backfill_complete = excluded.backfill_complete,
            updated_at = excluded.updated_at
        """,
        (username, cursor, total_new, 1 if backfill_complete else 0),
    )
    conn.commit()
    if backfill_complete:
        delete_cursor_file(username)
    else:
        write_cursor_file(username, cursor)


def load_cursor_from_db(conn: sqlite3.Connection, username: str) -> str | None:
    row = conn.execute(
        "SELECT last_cursor FROM fetch_state WHERE username = ?", (username,)
    ).fetchone()
    return row[0] if row else None


def is_backfill_complete(conn: sqlite3.Connection, username: str) -> bool:
    row = conn.execute(
        "SELECT backfill_complete FROM fetch_state WHERE username = ?", (username,)
    ).fetchone()
    return bool(row and row[0])


def show_stats(conn: sqlite3.Connection, username: str | None = None) -> None:
    total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
    if total == 0:
        print("Database is empty.")
        return

    newest = conn.execute(
        "SELECT created_at FROM tweets ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    oldest = conn.execute(
        "SELECT created_at FROM tweets ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    replies = conn.execute(
        "SELECT COUNT(*) FROM tweets WHERE is_reply = 1"
    ).fetchone()[0]

    print(f"Total tweets: {total}")
    print(f"  Replies:    {replies}")
    print(f"  Original:   {total - replies}")
    if newest and oldest:
        print(f"  Newest:     {newest[0]}")
        print(f"  Oldest:     {oldest[0]}")

    if username:
        state = conn.execute(
            "SELECT total_fetched, updated_at, backfill_complete FROM fetch_state WHERE username = ?",
            (username,),
        ).fetchone()
        if state:
            bc = "yes" if state[2] else "no"
            print(f"  Last fetch:      {state[1]} ({state[0]} new tweets that run)")
            print(f"  Backfill done:   {bc}")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _api_request(url: str, params: dict) -> dict:
    """Single API call with retries."""
    headers = {"x-api-key": API_KEY}
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429 or status >= 500:
                last_exc = e
            else:
                raise
        except requests.RequestException as e:
            last_exc = e

        if attempt < MAX_RETRIES:
            delay = RETRY_BACKOFF ** attempt
            print(
                f"  [retry {attempt}/{MAX_RETRIES} in {delay:.1f}s: {last_exc}]",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise last_exc  # type: ignore[misc]


def fetch_page(username: str, cursor: str) -> dict:
    """Fetch one page of tweets from /twitter/user/last_tweets."""
    params: dict[str, str] = {
        "userName": username,
        "includeReplies": "true",
    }
    if cursor:
        params["cursor"] = cursor
    return _api_request(f"{BASE_URL}/twitter/user/last_tweets", params)


# ---------------------------------------------------------------------------
# Single-page fetch + insert helper
# ---------------------------------------------------------------------------


def _fetch_and_insert(
    username: str,
    cursor: str,
    existing: set[str],
    conn: sqlite3.Connection,
) -> tuple[int, int, bool, str, list[dict]]:
    """
    Fetch one page, insert new tweets.
    Returns (new_count, known_on_page, has_next, next_cursor, tweets).
    Exits on API error.
    """
    try:
        data = fetch_page(username, cursor)
    except requests.HTTPError as exc:
        if (exc.response is not None
                and 400 <= exc.response.status_code < 500
                and exc.response.status_code != 429):
            print(f"\nERROR: HTTP {exc.response.status_code}", file=sys.stderr)
            try:
                print(exc.response.json(), file=sys.stderr)
            except Exception:
                print(exc.response.text[:500], file=sys.stderr)
            sys.exit(1)
        raise

    inner = data.get("data") or {}
    if data.get("status") == "error":
        msg = data.get("msg") or data.get("message") or "unknown error"
        print(f"\nAPI error: {msg}", file=sys.stderr)
        sys.exit(1)

    tweets: list[dict] = inner.get("tweets", [])
    has_next: bool = data.get("has_next_page", False)
    next_cursor: str = data.get("next_cursor", "")

    new_ids = {t["id"] for t in tweets} - existing
    known_on_page = len(tweets) - len(new_ids)
    new_count = insert_tweets(conn, [t for t in tweets if t["id"] in new_ids])
    existing |= new_ids

    ts_range = ""
    if tweets:
        newest_ts = tweets[0].get("createdAt", "?")
        oldest_ts = tweets[-1].get("createdAt", "?")
        ts_range = f"  [{oldest_ts} ... {newest_ts}]"
    print(f"{len(tweets)} tweets  new={new_count}  known={known_on_page}{ts_range}")

    return new_count, known_on_page, has_next, next_cursor, tweets


# ---------------------------------------------------------------------------
# Core pagination loop (single linear sweep)
# ---------------------------------------------------------------------------


def _paginate(
    username: str,
    conn: sqlite3.Connection,
    existing: set[str],
    start_cursor: str,
    max_pages: int | None,
    page_label: str = "",
    *,
    stop_on_all_known: bool = True,
) -> tuple[int, bool, str, int, bool]:
    """
    Linear pagination starting from *start_cursor*.

    Parameters
    ----------
    stop_on_all_known : bool
        If True, stop when every tweet on a page is already in the DB.

    Returns
    -------
    (total_new, reached_end, last_cursor, pages_done, stopped_by_max_pages)
    """
    cursor = start_cursor
    page = 0
    total_new = 0
    reached_end = False
    seen_cursors: set[str] = set()
    prev_tweet_ids: frozenset[str] | None = None

    while True:
        if max_pages is not None and page >= max_pages:
            print(f"Reached --max-pages={max_pages}. Stopping.")
            return total_new, False, cursor, page, True

        page += 1
        label = f"{page_label}Page {page}" if page_label else f"Page {page}"
        print(f"{label}  ", end="", flush=True)

        new_count, known, has_next, next_cursor, tweets = _fetch_and_insert(
            username, cursor, existing, conn
        )
        total_new += new_count

        if not tweets:
            print("0 tweets (empty page). Done.")
            reached_end = True
            return total_new, True, cursor, page, False

        if stop_on_all_known and known == len(tweets):
            print("All tweets on this page already stored — caught up. Stopping.")
            return total_new, False, cursor, page, False

        # Safety: detect cursor loops (same cursor string)
        if cursor and cursor in seen_cursors:
            print("Cursor loop detected (same cursor returned again). Stopping.")
            return total_new, True, cursor, page, False
        if cursor:
            seen_cursors.add(cursor)

        # Safety: detect same tweets returned again (API cursor churn)
        cur_ids = frozenset(t["id"] for t in tweets)
        if prev_tweet_ids is not None and cur_ids == prev_tweet_ids:
            print("Same tweet IDs as previous page — cursor is not advancing. Stopping.")
            return total_new, True, cursor, page, False
        prev_tweet_ids = cur_ids

        if not has_next:
            print("API reports no more pages. Done.")
            reached_end = True
            return total_new, True, next_cursor or cursor, page, False

        if not next_cursor:
            print("No next_cursor returned. Done.")
            reached_end = True
            return total_new, True, cursor, page, False

        cursor = next_cursor
        save_cursor(conn, username, cursor)
        time.sleep(PAGE_DELAY)


# ---------------------------------------------------------------------------
# Core fetch orchestrator
# ---------------------------------------------------------------------------


def fetch_all(
    username: str,
    db_path: str,
    max_pages: int | None = None,
    resume_cursor: str | None = None,
) -> int:
    """
    Fetch tweets for *username*, storing in *db_path*.

    Returns total number of newly inserted tweets.
    """
    conn = _connect(db_path)
    init_db(conn)

    existing = known_ids(conn)
    backfill_done_before = is_backfill_complete(conn, username)
    was_empty = len(existing) == 0

    # Determine resume cursor and mode.
    #
    # resume_cursor priority:
    #   1. Explicit --resume-cursor
    #   2. Saved cursor from DB/file (if backfill incomplete, or if
    #      backfill complete but interrupted by --max-pages last time)
    #   3. Empty string (start from page 1)
    #
    # Modes:
    #   "continue" — incomplete backfill + cursor exists → two-phase
    #   "initial"  — empty DB → fresh backfill from page 1
    #   "update"   — backfill complete (maybe with cursor) → catch up
    if resume_cursor is None:
        # Load from DB if we have a reason to resume
        if not backfill_done_before:
            # Backfill incomplete — always try to load cursor
            db_cursor = load_cursor_from_db(conn, username)
            if db_cursor:
                resume_cursor = db_cursor
                print(f"Resuming from saved cursor: {resume_cursor[:60]}...")
            else:
                resume_cursor = ""
        else:
            # Backfill complete — load cursor only if it exists (means
            # previous update was interrupted by --max-pages).
            cf = _cursor_file_path(username)
            if os.path.exists(cf):
                with open(cf) as f:
                    saved = f.read().strip()
                if saved:
                    resume_cursor = saved
                    print(f"Resuming update from saved cursor: {resume_cursor[:60]}...")
                else:
                    resume_cursor = ""
            else:
                resume_cursor = ""

    cursor = resume_cursor or ""

    print(f"User: @{username}")
    print(f"DB:   {os.path.abspath(db_path)}")
    print(f"Existing tweets in DB: {len(existing)}")

    # Determine mode
    if was_empty:
        print("Mode: initial backfill")
        mode = "initial"
    elif not backfill_done_before and cursor:
        print("Mode: backfill continuation (previous run interrupted)")
        mode = "continue"
    elif backfill_done_before and cursor:
        print("Mode: update continuation (resuming from cursor)")
        mode = "update"
    else:
        print("Mode: update (backfill already complete)")
        mode = "update"

    print("=" * 50)

    total_new = 0
    pages_total = 0
    reached_end = False
    interrupted = False  # True if --max-pages stopped us

    # ---------------------------------------------------------------
    # Mode: initial backfill or update
    # ---------------------------------------------------------------
    if mode in ("initial", "update"):
        total_new, reached_end, last_cursor, pages, interrupted = _paginate(
            username,
            conn,
            existing,
            start_cursor=cursor,
            max_pages=max_pages,
            stop_on_all_known=(mode == "update"),
        )
        pages_total = pages
        cursor = last_cursor

    # ---------------------------------------------------------------
    # Mode: backfill continuation (two-phase)
    # ---------------------------------------------------------------
    elif mode == "continue":
        # --- Phase 1: catch up on newest tweets (start from page 1) ---
        # Only do this if we have a cursor to resume FROM (meaning some
        # tweets are already stored).  Fetch newest tweets, stop when
        # we hit already-known data.  Deduct these pages from --max-pages.
        print("--- Phase 1: fetching newest tweets ---")
        ph1_new, ph1_end, ph1_cursor, ph1_pages, ph1_interrupted = _paginate(
            username,
            conn,
            existing,
            start_cursor="",
            max_pages=max_pages,
            page_label="[P1] ",
            stop_on_all_known=True,
        )
        total_new += ph1_new
        pages_total += ph1_pages

        # If --max-pages was hit during Phase 1, save cursor and bail.
        if ph1_interrupted:
            print("--- Phase 2 skipped (--max-pages reached in Phase 1) ---")
            save_final_state(conn, username, ph1_cursor, total_new,
                             backfill_complete=False)
            print("=" * 50)
            print(f"Done. {total_new} new tweets this run.  "
                  f"Total in DB: {len(existing)}")
            conn.close()
            return total_new

        # --- Phase 2: continue backfill from saved cursor ---
        remaining = None
        if max_pages is not None:
            remaining = max_pages - ph1_pages
        if remaining is not None and remaining <= 0:
            print("--- Phase 2 skipped (no pages remaining) ---")
            save_final_state(conn, username, resume_cursor, total_new,
                             backfill_complete=False)
            print("=" * 50)
            print(f"Done. {total_new} new tweets this run.  "
                  f"Total in DB: {len(existing)}")
            conn.close()
            return total_new

        print(f"--- Phase 2: resuming backfill from cursor "
              f"{resume_cursor[:40] if resume_cursor else '(empty)'}... ---")
        ph2_new, ph2_end, ph2_cursor, ph2_pages, ph2_interrupted = _paginate(
            username,
            conn,
            existing,
            start_cursor=resume_cursor,
            max_pages=remaining,
            page_label="[P2] ",
            stop_on_all_known=False,
        )
        total_new += ph2_new
        pages_total += ph2_pages

        if ph2_interrupted:
            # --max-pages hit in Phase 2; save cursor for next run
            save_final_state(conn, username, ph2_cursor, total_new,
                             backfill_complete=False)
            print("=" * 50)
            print(f"Done. {total_new} new tweets this run.  "
                  f"Total in DB: {len(existing)}")
            conn.close()
            return total_new

        reached_end = ph2_end
        cursor = ph2_cursor
        interrupted = False

    # ---------------------------------------------------------------
    # Final state
    # ---------------------------------------------------------------
    if interrupted:
        # --max-pages stopped us.
        # Save cursor.  Keep backfill_complete=True only if we were
        # in update mode (the oldest tweets are already fetched).
        save_final_state(conn, username, cursor, total_new,
                         backfill_complete=(mode == "update"))
    elif reached_end:
        # Natural end — mark backfill complete, clear cursor
        save_final_state(conn, username, "", total_new,
                         backfill_complete=True)
    elif mode == "update":
        # Caught up with known data — nothing to save, still complete
        save_final_state(conn, username, "", total_new,
                         backfill_complete=True)
    else:
        # Unexpected early exit; save what we have
        save_final_state(conn, username, cursor, total_new,
                         backfill_complete=False)

    print("=" * 50)
    print(f"Done. {total_new} new tweets this run.  "
          f"Total in DB: {len(existing)}")
    conn.close()
    return total_new


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deprecated: osiris now does this itself with a list of usernames. "
                    "Fetch all tweets (incl. replies) from a Twitter account "
                    "via twitterapi.io",
    )
    parser.add_argument("username", help="Twitter screen name (without @)")
    parser.add_argument(
        "--db", default=None,
        help="SQLite database path (default: <username>/tweets.db)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Limit to N pages (for testing)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show DB stats and exit (no fetch)",
    )
    parser.add_argument(
        "--resume-cursor", default=None,
        help="Resume from a saved cursor value (overrides auto-detection)",
    )
    args = parser.parse_args()

    db_path = args.db or _default_db_path(args.username)

    if args.stats:
        conn = _connect(db_path)
        init_db(conn)
        show_stats(conn, args.username)
        conn.close()
        return

    if not API_KEY:
        sys.exit("No twitterapi.io key. Set TWITTERAPI_IO_KEY in the environment or in .env beside this script.")

    fetch_all(
        username=args.username,
        db_path=db_path,
        max_pages=args.max_pages,
        resume_cursor=args.resume_cursor,
    )


if __name__ == "__main__":
    main()
