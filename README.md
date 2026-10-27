# archiver

Archives a list of saved URLs (bookmarks, Session Buddy tabs) as clean markdown for
lexical and semantic indexing. Only text is kept. Images and video are never downloaded.

## Pipeline

```sh
python3 -m venv .venv && .venv/bin/pip install -e .          # add [render] for --render, then: playwright install chromium

python3 extract_urls.py                 # ../tab-backups/{bookmarks_*.html,session-buddy-*.json} -> ./all_urls.md
.venv/bin/archiver run                  # ./all_urls.md -> ./data  (resumable, Ctrl-C safe)
.venv/bin/archiver stats --failed --thin
.venv/bin/archiver export               # ./data/corpus.jsonl, one document per line
```

Typical local run, with your browser's session for Scholar, Medium and logged-in pages:

```sh
.venv/bin/archiver run --cookies ~/cookies.txt --user-agent "<your browser's UA>" --render
```

Re-running `archiver run` on a new or updated list only queues URLs whose dedup key
(`archiver/dedup.py`, shared with `extract_urls.py`: tracking params, `twitter.com`→`x.com`,
`www.`/`m.`/`old.` hosts, arXiv abs/pdf/html, …) isn't already in the manifest. Every run
also applies the current routing rules to URLs not yet archived. Failed URLs get each fallback
pass once and are then left alone: `--retry-failed` re-queues all of them, and
`--retry-host 'scholar.google.*'` (repeatable) only those on matching sites, e.g. after adding
`--cookies`. To move a run to another machine, copy `data/` along with the repo (not `.venv`),
after the run has stopped.

### Keys (all optional)

| variable | used for |
|---|---|
| `SCRY_API_KEY` (or `~/.config/scry/env`, `~/.scry/.env`) | tweets, Reddit threads and DOI abstracts from [Scry](https://scry.io)'s archives; free while Scry has spare capacity |
| `TWITTERAPI_IO_KEY` | tweets Scry lacks, via twitterapi.io (billed per tweet). Unset: those become `not_in_scry` |
| `STACKEXCHANGE_KEY` | raises the Stack Exchange API quota from 300 to 10,000 calls/day |
| `OPENALEX_API_KEY` | your own OpenAlex budget instead of the shared per-IP one |

## Lanes

Each URL is routed to the cheapest source that actually returns its text:

| lane | URLs | how |
|---|---|---|
| `twitter` | `x.com/…/status/<id>` | Scry's Twitter archive first (it also holds some deleted tweets), then twitterapi.io for the rest if keyed. x.com serves an empty JS shell |
| `arxiv` | `arxiv.org/{abs,pdf,html}/<id>` | export API, 100 IDs per call, title/authors/abstract (arxiv.org's robots.txt sets a 15 s crawl delay) |
| `stackexchange` | `*/questions/<id>` on SE sites | api.stackexchange.com, 100 IDs per call, question and all answers as markdown (the sites show Cloudflare challenges to datacenter IPs) |
| `reddit` | `reddit.com/…/comments/<id>`, `redd.it/<id>` | post + comment tree from Scry's Reddit archive, falling back to Arctic Shift for posts or comment trees Scry lacks. reddit.com refuses datacenter IPs and new API apps need approval |
| `generic` | everything else | async fetch, then trafilatura main-content markdown, or pypdfium2 for PDFs, then the fallback passes below |
| `skip` | GitHub (`github.com`, gists, raw files; `*.github.io` sites are kept), private chat links (ChatGPT/Claude/Grok), X profiles, media files, local hosts | recorded, not fetched |

`--skip LANE` leaves a lane's URLs pending (e.g. `--skip twitter`); `--only LANE` runs just one.

### Generic lane

Pass 1:
- Hosts are served round-robin, with at most `--per-host` (4) requests in flight per host, `--concurrency` (64) overall, and cooldowns on 429/5xx (`Retry-After` is honored).
- `--host-limit PATTERN=N[:SECONDS]` groups matching hosts into one slower queue. The default is `scholar.google.*=1:8`: one request at a time, about 8 s apart, since Scholar shows CAPTCHAs after bursts.
- The `Content-Type` is checked before the body is read, so non-text responses are dropped unread.
- A host is skipped after 5 straight refusals (403, or 429 after retries) or 3 connection failures.

Then fallback passes over what's still failed or thin, cheapest first. Each pass keeps a result only if it beats the previous one, and each URL gets each pass once (the manifest's `tried` column):

1. **impersonate**: curl_cffi with Chrome's TLS fingerprint, for 403s, resets and thin pages.
2. **render** (`--render`, opt-in): headless Chromium, for pages that only exist after JavaScript runs and for bot-challenge interstitials, which it waits out for `--challenge-wait` seconds. It costs a few hundred MB of RAM per page, so `--render-concurrency` defaults to 2.
3. **doi** (`--no-doi` to skip): paper links that failed are matched by DOI. That covers dead sci-hub mirrors, publisher `/doi/` pages, and nature.com and JSTOR IDs. The pass gets title, authors and abstract from Scry's OpenAlex snapshot, then the OpenAlex API.
4. **wayback** (`--no-wayback` to skip): the newest successful Wayback Machine capture, 4 requests at a time.

Your browser session, the way ArchiveBox and Karakeep handle logged-in and bot-walled sites:
- `--cookies FILE` takes a Netscape `cookies.txt` (as written by the "Get cookies.txt LOCALLY" extension, the same format yt-dlp and wget read) or a JSON cookie export. Each cookie is only sent to its own site, including across redirects.
- `--user-agent` should match the browser the cookies came from. Cloudflare ties `cf_clearance` to it.
- `--chrome-profile DIR` makes `--render` use a copy of your Chrome profile directory.
- The cookie file holds live sessions, so keep it private.

## Output (`data/`)

- `manifest.sqlite`: one row per input URL with lane, status (`pending|ok|thin|failed|skipped`), reason, HTTP status, final URL, character count, dedup key, fallback passes tried and raw path.
- `md/<id[:2]>/<id>.md`: YAML front matter (url, title, author, date, site, source) plus the markdown body. `source` records where the text came from, e.g. `html`, `pdf:wayback`, `html:render`, `doi:scry`, `reddit:scry+arctic_shift`, `tweet:twitterapi`.
- `corpus.jsonl`: written by `archiver export`.
- `raw/<id[:2]>/<id>.{html.zst,pdf}`: only with `--keep-raw`. HTML is zstd-compressed and PDFs are kept as-is. Budget about 1–1.5 GB for 20k pages plus about 2 MB per PDF.

`thin` means under 200 characters were extracted. These are usually JS-only apps, paywalls or bot walls, or papers with no abstract. They are still saved and exported.

Extraction runs in worker processes that are memory-capped (`--worker-mem-mb`, default 1536), replaced every 500 documents, and rebuilt if one crashes. Only the document that crashes a worker is lost.
