# archiver

Archives a list of saved URLs and local files (bookmarks, Session Buddy tabs) as clean markdown
for lexical and semantic indexing. Only text is kept. Images and video are never downloaded.

## Pipeline

```sh
python3 -m venv .venv && .venv/bin/pip install -e .          # add [render] for --render, then: playwright install chromium

python3 extract_urls.py                 # ../tab-backups/{bookmarks_*.html,session-buddy-*.json} -> ./all_urls.md
.venv/bin/archiver run                  # ./all_urls.md -> ./data  (resumable, Ctrl-C safe)
.venv/bin/archiver stats --failed --thin
.venv/bin/archiver import ~/saved-pages # HTML pages you saved with SingleFile -> ./data
.venv/bin/archiver ocr                  # PDFs with no text layer, with tesseract (slow; apt install tesseract-ocr)
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
| `GITHUB_TOKEN` (or `~/.config/archiver/env`) | the GitHub lane: 5,000 API calls/hour instead of 60. Unset: its URLs stay pending. A token with no scopes is enough for public repos. Keep the file private |
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
| `github` | `github.com/<owner>/<repo>/…`, gists | REST API: repo metadata and README, or a gist's files (Jupyter notebooks reduced to their markdown and code). All links into one repo share its document, so a file or issue link stores the repo's README, not that file or issue |
| `youtube` | videos, playlists, channels | transcripts, or a list's title, description and video titles (up to 2,000, without their transcripts). See below |
| `localfile` | `file://` links to PDFs, ebooks (`.epub`, `.mobi`, `.azw`, `.azw3`), `.docx`, web pages and text files | read in place, never changed. See below |
| `generic` | everything else | async fetch, then trafilatura main-content markdown, or pypdfium2 for PDFs, then the fallback passes below |
| `skip` | GitHub user and organization pages and GitHub's own pages, other YouTube pages (search, feeds, your Watch Later and Liked lists), other local files (images, no extension), private chat links (ChatGPT/Claude/Grok), X profiles, media files, local hosts | recorded, not fetched |

`--skip LANE` leaves a lane's URLs pending (e.g. `--skip youtube`); `--only LANE` runs just one.

`extract_urls.py` keeps web pages and `file://` links. It drops Google except Scholar, Gmail, local servers and browser-internal links (`chrome://`, `chrome-extension://`, `about:`, …).

### YouTube lane

- Transcripts come from `youtube_transcript_api`: English first, then the video's own language, with human captions ahead of automatic ones. Playlists and channels are read with `yt-dlp`. A video with no captions is stored as `thin`, with just its title and channel.
- YouTube blocks most datacenter IPs and throttles bursts from any IP, so run this lane from a home connection. Requests go out one at a time, `--youtube-delay` (5) seconds apart.
- The lane has its own worker thread and shares no limits with the others, so a block here can't hold up another document. A blocked item stays `pending`. The lane pauses for 5, 10, 20, then 40 minutes, and after 5 blocks in a row stops until the next run. Once only YouTube is left, the log says so, and Ctrl-C is safe.

### Local files

- `all_urls.md` holds them as `file:///path/to/My%20Paper.pdf`, one per line, and they run with `archiver run` like any URL. `file:///C:/…` is also looked up under `/mnt/c/…` for WSL.
- To add a whole folder, write its files to a list of your own and run that. Files in formats the lane can't read are recorded as `skipped` with `local_other`, and are picked up automatically if a later version supports them:

  ```sh
  find ~/my-files -type f -print0 | python3 -c '
  import sys, pathlib
  for p in sys.stdin.buffer.read().split(b"\0"):
      if p:
          print(pathlib.Path(p.decode()).resolve().as_uri())
  ' > local_files.txt
  .venv/bin/archiver run local_files.txt --only localfile
  ```

- Readable formats: `.pdf`; `.epub`; `.mobi`, `.azw`, `.azw3`; `.docx`; `.html`, `.htm`, `.xhtml`; `.txt`, `.md`, `.markdown`, `.rst`, `.tex`, `.org`.
- Ebooks and Word files follow [MarkItDown](https://github.com/microsoft/markitdown)'s recipe: an EPUB is read in spine order, a DOCX goes through mammoth, and the HTML becomes markdown with markdownify, keeping all the text rather than guessing at a main content block. Kindle files are unpacked with KindleUnpack (the `mobi` package) first.
- Books under DRM are `failed` with `drm_protected`, and aren't worked around. Newer `.kfx` files and old binary `.doc` files aren't supported.
- **Your files are only ever read.** Nothing moves, changes or deletes them, and `--keep-raw` stores a copy under `raw/`.
- A file that isn't there is `failed` with `file_missing`: run on the machine that has the files, or use `--retry-failed` once they're back. A scanned PDF with no text layer is `failed` with `no_text` until you run `archiver ocr`.
- Only the first 400 pages of any PDF are read, here and in the generic lane.

### OCR (`archiver ocr`)

Reads PDFs recorded with no usable text, local or from the web, with [tesseract](https://github.com/tesseract-ocr/tesseract). It's a separate command because it takes seconds per page, so a long scan can't hold up anything else. Local files are read in place and never changed, and web PDFs are downloaded again to a temporary file. Text is kept only if there's more of it than before, with `source: pdf:ocr` or `file:pdf:ocr`.

- `--lang eng+tur` sets the languages, after installing their packs (`apt install tesseract-ocr-tur`). The default is English. List only what you need, since each extra language is slower and can lower accuracy.
- `--workers N` runs N PDFs at once. Each PDF is tried once, and `--retry` tries them again, e.g. with another language. Only the first 400 pages are read, like other PDFs.
- A PDF with a few readable pages and many scanned ones counts as having text, so it isn't picked up.

### Saved pages (`archiver import`)

`archiver import DIR [DIR|FILE ...]` stores pages saved with the [SingleFile](https://github.com/gildas-lormeau/SingleFile) browser extension as markdown like every other document. Subdirectories are searched for `.html` and `.htm` files, and the originals are left alone.

- The page's URL and save date come from the comment SingleFile writes at the top of the file. The date becomes `fetched_at`. A file with no such comment uses its `<link rel=canonical>`, and failing that is stored under a `file:` key.
- URLs go through the same dedup as `archiver run`, and imported URLs are never queued for fetching. Saved copies replace failed or pending entries, and an archived document only when they have more text. Complete Stack Exchange and Reddit documents are always kept.
- URLs `archiver run` skips on purpose (GitHub, chat apps, X profiles) are stored, since you saved them deliberately.
- Files are tracked by content hash, so re-running on a growing directory only reads new or changed files. Files that failed are tried again.
- Inlined CSS, scripts and images are cut before parsing, as are elements SingleFile marked hidden (`sf-hidden`). Saved shadow DOM is kept. X pages are read from X's own tweet markup, so a saved conversation keeps every tweet in it, each with its author and date, and any X Article. SingleFile's self-extracting ZIP format isn't supported and is reported as `singlefile_zip`; use the default HTML format.

Imported documents appear as the `singlefile` lane in `stats` and `export`, with `source: singlefile`.

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
- `md/<id[:2]>/<id>.md`: YAML front matter (url, title, author, date, site, source) plus the markdown body. `source` records where the text came from, e.g. `html`, `singlefile`, `pdf:wayback`, `html:render`, `doi:scry`, `reddit:scry+arctic_shift`, `tweet:twitterapi`, `github:api`, `youtube:transcript:auto`, `file:pdf`.
- `corpus.jsonl`: written by `archiver export`.
- `raw/<id[:2]>/<id>.{html.zst,pdf}`: only with `--keep-raw`. HTML is zstd-compressed and PDFs are kept as-is. Budget about 1–1.5 GB for 20k pages plus about 2 MB per PDF.

`thin` means under 200 characters were extracted. These are usually JS-only apps, paywalls or bot walls, or papers with no abstract. They are still saved and exported.

Extraction runs in worker processes that are memory-capped (`--worker-mem-mb`, default 1536), replaced every 500 documents, and rebuilt if one crashes. Only the document that crashes a worker is lost.
