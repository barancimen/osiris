# archiver

Archives a list of saved URLs (bookmarks, Session Buddy tabs) as clean markdown for
lexical and semantic indexing. Only text is kept. Images and video are never downloaded.

## Pipeline

```sh
python3 -m venv .venv && .venv/bin/pip install -e .

python3 extract_urls.py                 # ../tab-backups/{bookmarks_*.html,session-buddy-*.json} -> ./all_urls.md
export TWITTERAPI_IO_KEY=...            # twitterapi.io key, for tweets
.venv/bin/archiver run                  # ./all_urls.md -> ./data  (resumable, Ctrl-C safe)
.venv/bin/archiver stats --failed --thin
.venv/bin/archiver export               # ./data/corpus.jsonl, one document per line
```

Re-running `archiver run` on a new or updated list only queues URLs whose dedup key
(`archiver/dedup.py`, shared with `extract_urls.py`: tracking params, `twitter.com`→`x.com`,
`www.`/`m.`/`old.` hosts, arXiv abs/pdf/html, …) isn't already in the manifest.
`--retry-failed` re-queues earlier failures.

## Lanes

Each URL is routed to the cheapest source that actually returns its text:

| lane | URLs | how |
|---|---|---|
| `twitter` | `x.com/…/status/<id>` | twitterapi.io batch lookup, 50 IDs per call (x.com serves an empty JS shell) |
| `arxiv` | `arxiv.org/{abs,pdf,html}/<id>` | export API, 100 IDs per call, title/authors/abstract (arxiv.org's robots.txt sets a 15 s crawl delay) |
| `stackexchange` | `*/questions/<id>` on SE sites | api.stackexchange.com, 100 IDs per call, question and all answers as markdown (the sites show Cloudflare challenges to datacenter IPs). 300 calls/day without `STACKEXCHANGE_KEY` |
| `generic` | everything else | async fetch, then trafilatura main-content markdown, or pypdfium2 for PDFs |
| `skip` | private chat links (ChatGPT/Claude/Grok), X profiles, media files, local hosts | recorded, not fetched |

Generic-lane details:
- Hosts are served round-robin, with at most `--per-host` (4) requests in flight per host, `--concurrency` (64) overall, and cooldowns on 429/5xx (`Retry-After` is honored).
- The `Content-Type` is checked before the body is read, so non-text responses are dropped unread.
- Pages blocked by 403s/resets, and pages with thin text, get a second try with a Chrome TLS fingerprint (curl_cffi).
- A host is skipped after 5 straight 403s (Google Scholar, Reddit) or 3 connection failures.

## Output (`data/`)

- `manifest.sqlite`: one row per input URL with lane, status (`pending|ok|thin|failed|skipped`), reason, HTTP status, final URL, character count, dedup key and raw path.
- `md/<id[:2]>/<id>.md`: YAML front matter (url, title, author, date, site, source) plus the markdown body.
- `corpus.jsonl`: written by `archiver export`.
- `raw/<id[:2]>/<id>.{html.zst,pdf}`: only with `--keep-raw`. HTML is zstd-compressed and PDFs are kept as-is. Budget about 1–1.5 GB for 20k pages plus about 2 MB per PDF.

`thin` means under 200 characters were extracted. These are usually JS-only apps, paywalls or bot walls. They are still saved and exported.
