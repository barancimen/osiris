# archiver

Archives saved URLs and local files as clean markdown for lexical and semantic indexing. Only text is kept: images and video are never downloaded.

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/archiver ui
```

This opens the [web interface](#web-interface), which covers the whole process. The same work from the command line:

```sh
python3 extract_urls.py    # ../tab-backups -> all_urls.md
.venv/bin/archiver run     # all_urls.md -> data/ (resumable, Ctrl-C safe)
.venv/bin/archiver stats --failed --thin
.venv/bin/archiver export  # data/corpus.jsonl, one document per line
```

Optional system and Python extras:

| For | Install |
|---|---|
| OCR of scanned PDFs | `apt install tesseract-ocr` (plus `tesseract-ocr-tur` etc. for other languages), or `brew install tesseract` |
| `--render` | `.venv/bin/pip install -e .[render]`, then `playwright install chromium` |

## Web interface

`archiver ui` opens one page with three tabs. Choices are saved in `data/ui.json`, and a job keeps running if you close or reload the page.

- **Archive:** add folders of tab backups, local files and saved pages with a folder picker, choose options, and press **Start archiving**. That extracts URLs, imports saved pages, archives everything and checks for garbage, showing progress and a live log. **Stop** saves progress, and starting again resumes.
- **Review:** documents that look like garbage, worst first. See [Finding garbage](#finding-garbage).
- **Documents:** search titles, URLs or the text inside documents, filter by source and status, and see why something failed. Each document shows its saved text and, where there's one on this machine, its original: your local file, a saved page or a `--keep-raw` copy. Web documents link to the live page.

**Keyboard in Review:** `j`/`k` move, `f` fine, `b` bad, `o` OCR it, `r` fetch again, `x` can't fix, `u` undo, `n` write a note.

**Over ssh:** the page only listens on this machine, so forward the port from your own machine, then open the printed address:

```sh
ssh -N -L 8321:127.0.0.1:8321 <server>
```

**Things to know:**

- **Access:** every request needs the token in the printed address, stored in `data/ui-token`. Don't share that address, since the page can read your files and start jobs. Originals are served only for files already in the manifest, and saved web pages open sandboxed so their scripts can't run.
- **One job at a time:** don't run `archiver run` in a terminal while the page is running a job.
- **YouTube:** the garbage check runs after the archive step, and YouTube can take days. To check sooner, press **Stop**, then **Check for garbage**, or turn YouTube off for that run.
- **twitterapi.io:** runs from the page ignore `TWITTERAPI_IO_KEY` unless you turn on the billed option under Advanced. Tweets still come from Scry.
- **Older imports:** pages imported before this version only recorded a file name, so they have no Original view.
- **Quitting:** Ctrl-C in the terminal stops the page and any running job, with progress saved.

## Finding garbage

**Check for garbage** scores every document from cheap text signals, then lists anything scoring 0.35 or higher, worst first. Unchanged documents aren't scored again. It's a ranking for you to read, not a verdict, so expect some false alarms, such as tables, code and languages other than English and Turkish.

| Signal | What it catches |
|---|---|
| `encoding` | `�`, `Ã`, `â€`, `(cid:12)` and control characters: wrong encoding and failed PDF extraction |
| `few common words` | Text from broken PDF font maps, random characters |
| `odd words` | Words with no vowels or with digits inside, usually from bad OCR |
| `symbols` | Leftover code or markup |
| `no spaces` | PDFs that lost their word spacing |
| `repeated lines` | Repeating headers and navigation |
| `block page` | Short pages saying "enable JavaScript", "unusual traffic", "access denied" and the like |

| Decision | What happens |
|---|---|
| **Fine** | Stays off the list unless its text changes |
| **Bad** | Left out of `export` and listed in `data/audit-exclude.txt`. It stays under **Bad** until you decide otherwise |
| **OCR it** | PDFs only. Reads the PDF with OCR, and replaces the text if the new text scores better, even if it's shorter |
| **Fetch again** | Fetches or reads it from scratch, replacing what's there. If that fails, the document is no longer exported |
| **Can't fix** | Like Bad, but no longer counted as something to come back to |

**Your documents aren't changed when you decide.** OCR and Fetch again are queued, and **Run fixes** carries them out. A decision only holds for the exact text it was about: if the text changes, the document is scored afresh. A fix that changed nothing comes back as Bad, with a note saying so.

If your indexer reads `data/md` directly, `data/audit-exclude.txt` lists the paths to leave out. The file is rewritten by each check.

From the command line: `archiver audit scan`, then `archiver audit apply`, which also prints the lanes to run for documents queued to fetch again.

## Commands

| Command | What it does |
|---|---|
| `archiver ui` | Opens the web interface |
| `archiver audit {scan,apply}` | Finds probable garbage, or carries out queued fixes |
| `archiver run [FILE]` | Fetches and extracts every pending URL. `FILE` defaults to `all_urls.md`, and `/dev/stdin` works |
| `archiver import DIR [DIR\|FILE ...]` | Stores pages saved with SingleFile |
| `archiver ocr` | Runs only OCR, with `--retry` and `--limit` |
| `archiver stats` | Counts by lane and status. `--failed` and `--thin` add the top hosts |
| `archiver export` | Writes `data/corpus.jsonl`, leaving out documents marked Bad or Can't fix |

Common `archiver run` options:

| Option | Effect |
|---|---|
| `--only LANE`, `--skip LANE` | Run only this lane, or leave its URLs pending. Repeatable |
| `--retry-failed` | Re-queue all failed URLs |
| `--retry-host 'scholar.google.*'` | Re-queue failed URLs on matching hosts only. Repeatable |
| `--refresh-pdfs {web,local,both}` | Read every PDF again, failed or not. See [Refreshing PDFs](#refreshing-pdfs) |
| `--no-ocr`, `--ocr-lang eng+tur` | Turn off automatic OCR, or set its languages |
| `--keep-raw` | Also keep fetched pages and PDFs under `data/raw/` |
| `--cookies FILE`, `--user-agent UA` | Use your browser session. See [Browser session](#browser-session) |
| `--render` | Add the headless browser pass |
| `--no-impersonate`, `--no-doi`, `--no-wayback` | Skip a fallback pass |
| `--workers N`, `--worker-mem-mb N` | Extraction and OCR processes, and the memory cap for each |

`archiver run --help` lists the rest.

## Adding more to the archive

`archiver run` only queues URLs whose dedup key isn't already in the manifest, so it's safe to repeat. The key comes from `archiver/dedup.py`, which `extract_urls.py` shares. It removes tracking parameters and treats `twitter.com` as `x.com`, `www.`, `m.` and `old.` hosts as the same site, and arXiv abs, pdf and html links as one paper.

To archive tab backups and a folder of local files in one run:

```sh
python3 extract_urls.py --input-dir ~/tab_backups
find ~/local_files -type f -print0 | python3 -c 'import sys,pathlib; [print(pathlib.Path(p).resolve().as_uri()) for p in sys.stdin.read().split("\0") if p]' >> all_urls.md
.venv/bin/archiver run
```

`extract_urls.py` rewrites `all_urls.md` each time, so run it first. It keeps web pages and `file://` links, and drops Google except Scholar, Gmail, local servers and browser-internal links such as `chrome://` and `about:`.

- **Failed URLs:** each gets every fallback pass once and is then left alone until you use `--retry-failed` or `--retry-host`.
- **Routing changes:** every run applies the current routing rules to URLs that aren't archived yet.
- **Moving to another machine:** copy `data/` with the repo, but not `.venv`, after the run has stopped.

## API keys

All are optional.

| Variable | Used for | If unset |
|---|---|---|
| `SCRY_API_KEY` (or `~/.config/scry/env`, `~/.scry/.env`) | Tweets, Reddit threads and DOI abstracts from [Scry](https://scry.io)'s archives | Those sources are skipped |
| `GITHUB_TOKEN` (or `~/.config/archiver/env`) | GitHub lane, 5,000 API calls an hour instead of 60. A token with no scopes is enough for public repos. Keep the file private | GitHub URLs stay pending |
| `TWITTERAPI_IO_KEY` | Tweets Scry lacks, from twitterapi.io, billed per tweet | They're marked `not_in_scry` |
| `STACKEXCHANGE_KEY` | 10,000 Stack Exchange API calls a day instead of 300 | The lower quota applies |
| `OPENALEX_API_KEY` | Your own OpenAlex budget | The shared per-IP budget applies |

## Lanes

Each URL goes to the cheapest source that actually returns its text.

| Lane | URLs | How |
|---|---|---|
| `twitter` | `x.com/…/status/<id>` | Scry's archive, which also holds some deleted tweets, then twitterapi.io if keyed |
| `arxiv` | `arxiv.org/{abs,pdf,html}/<id>` | Export API, 100 IDs per call. Title, authors and abstract only |
| `stackexchange` | `*/questions/<id>` on SE sites | API, 100 IDs per call. Question, all answers and comments as markdown |
| `reddit` | `reddit.com/…/comments/<id>`, `redd.it/<id>` | Post and comment tree from Scry, then Arctic Shift for what Scry lacks |
| `github` | `github.com/<owner>/<repo>/…`, gists | REST API: repo metadata and README, or a gist's files with notebooks reduced to markdown and code |
| `youtube` | Videos, playlists, channels | Transcripts, or a list's title, description and video titles |
| `localfile` | `file://` links | Read in place, never changed |
| `generic` | Everything else | Async fetch, then trafilatura for pages and pypdfium2 for PDFs, then the fallback passes |
| `skip` | See below | Recorded, not fetched |

`skip` covers GitHub user, organization and site pages, other YouTube pages (search, feeds, Watch Later, Liked), unreadable local files, private chat links (ChatGPT, Claude, Grok), X profiles, media files and local hosts.

All links into one GitHub repo share one document, so a link to a file or issue stores the repo's README, not that file or issue.

### Stack Exchange

Threads archived before comments were collected gain them with this command, which updates each thread once:

```sh
.venv/bin/archiver run --only stackexchange --refetch-stackexchange
```

Without `STACKEXCHANGE_KEY` the API allows 300 calls a day. If that runs out, the same command carries on the next day. A question deleted since it was archived keeps its document.

### YouTube

Run this lane from a home connection, because YouTube blocks most datacenter IPs.

- **Transcripts:** these come from `youtube_transcript_api`, English first and then the video's own language, with human captions ahead of automatic ones. A video with no captions is stored as `thin`, with its title and channel.
- **Playlists and channels:** these are read with `yt-dlp`, up to 2,000 video titles without their transcripts.
- **Rate limits:** requests go out one at a time, `--youtube-delay` (5) seconds apart. The lane has its own worker thread, so a block here can't hold up other documents.
- **Blocks:** a blocked item stays `pending`. The lane pauses for 5, 10, 20, then 40 minutes, and after 5 blocks in a row stops until the next run.
- **Finishing early:** once only YouTube is left, the log says so. Ctrl-C is safe, and the next run resumes.

### Local files

**Your files are only ever read.** Nothing moves, changes or deletes them, and `--keep-raw` stores a copy.

| Type | Extensions | How |
|---|---|---|
| PDF | `.pdf` | pypdfium2, then OCR if there's no text |
| Ebook | `.epub`, `.mobi`, `.azw`, `.azw3` | EPUB in spine order. Kindle files are unpacked with KindleUnpack first |
| Word | `.docx` | mammoth, then markdownify |
| Web page | `.html`, `.htm`, `.xhtml` | Same reader as saved pages |
| Text | `.txt`, `.md`, `.markdown`, `.rst`, `.tex`, `.org` | As is |

- **Paths:** the list holds `file:///path/to/My%20Paper.pdf`, one per line. `file:///C:/…` is also looked up under `/mnt/c/…` for WSL.
- **Other formats:** these are `skipped` with `local_other`, and are picked up automatically if a later version supports them. Newer `.kfx` files and old binary `.doc` files aren't supported.
- **DRM:** books under DRM are `failed` with `drm_protected` and aren't worked around.
- **Missing files:** a file that isn't there is `failed` with `file_missing`. Run on the machine that has it, or use `--retry-failed` once it's back.

Ebooks and Word files follow [MarkItDown](https://github.com/microsoft/markitdown)'s recipe, which keeps all the text rather than guessing at a main content block.

### Generic lane

**First pass**

- Hosts are served round-robin, with at most `--per-host` (4) requests in flight per host and `--concurrency` (64) overall, and cooldowns on 429 and 5xx responses. `Retry-After` is honored.
- `--host-limit PATTERN=N[:SECONDS]` groups matching hosts into one slower queue. The default is `scholar.google.*=1:8`, since Scholar shows CAPTCHAs after bursts.
- The `Content-Type` is checked before the body is read, so non-text responses are dropped unread.
- A host is skipped after 5 straight refusals (403, or 429 after retries) or 3 connection failures.

**Fallback passes**

These run over what's still failed or thin, cheapest first. Each keeps a result only if it beats the previous one, and each URL gets each pass once, as recorded in the manifest's `tried` column.

1. **impersonate:** curl_cffi with Chrome's TLS fingerprint, for 403s, resets and thin pages.
2. **render** (`--render`, opt-in): headless Chromium, for pages that need JavaScript and for bot-challenge pages, which it waits out for `--challenge-wait` seconds. It costs a few hundred MB of RAM per page, so `--render-concurrency` defaults to 2.
3. **doi:** failed paper links are matched by DOI, covering dead sci-hub mirrors, publisher `/doi/` pages, and nature.com and JSTOR IDs. Title, authors and abstract come from Scry's OpenAlex snapshot, then the OpenAlex API.
4. **wayback:** the newest successful Wayback Machine capture, 4 requests at a time.

### Browser session

This helps with Scholar, Medium and other logged-in or bot-walled sites:

```sh
.venv/bin/archiver run --cookies ~/cookies.txt --user-agent "<your browser's UA>" --render
```

- `--cookies FILE` takes a Netscape `cookies.txt` or a JSON cookie export. Each cookie is sent only to its own site, including across redirects.
- `--user-agent` should match the browser the cookies came from, because Cloudflare ties `cf_clearance` to it.
- `--chrome-profile DIR` makes `--render` use a copy of your Chrome profile directory.

The cookie file holds live sessions, so keep it private.

## PDFs

Only the first 400 pages of any PDF are read.

### OCR

`archiver run` reads PDFs with no usable text with [tesseract](https://github.com/tesseract-ocr/tesseract), both web and local. It starts once the generic and local-file lanes finish, so a long scan holds up nothing, and it doesn't wait for YouTube.

- **Scope:** it covers only the lanes in that run. `--only localfile` leaves web PDFs alone, and `--only youtube` does no OCR.
- **Earlier PDFs:** it also works through PDFs left from earlier runs, so the first run after installing tesseract can take a while.
- **Files:** local files are read in place and never changed. Web PDFs are downloaded again to a temporary file, which is then deleted.
- **Tries:** each PDF is tried once. Ctrl-C stops at once, and the PDF in progress is tried again next run.
- **Results:** text is kept only if there's more of it than before, with `source: pdf:ocr` or `file:pdf:ocr`.
- **Languages:** `--ocr-lang eng+tur` sets them, after installing their packs. The default is English. List only what you need, since each extra language is slower and can lower accuracy.
- **Missing tesseract:** if tesseract or a language pack is missing, the run logs a warning and carries on, and those PDFs wait without using up their try.
- **Limitation:** a PDF with a few readable pages and many scanned ones counts as having text, so it isn't picked up.

`archiver ocr` runs OCR on its own. It takes `--lang`, `--workers`, `--limit`, and `--retry` to try PDFs again, for example in another language.

### Refreshing PDFs

```sh
.venv/bin/archiver run --refresh-pdfs web     # web PDFs only
.venv/bin/archiver run --refresh-pdfs local   # local PDFs only
.venv/bin/archiver run --refresh-pdfs both
```

This downloads or reads every PDF again, whatever its status.

| Status before | What happens |
|---|---|
| `failed` | Goes through the whole process again: first pass, fallback passes and OCR. The old failure is replaced by the new result |
| `thin` | Read again, and gets its fallback passes and OCR again. It's only replaced by more text |
| `ok` | Read again. It's only replaced by more text from a PDF |

- **Your text is safe:** a PDF that has text keeps it if the link has died, the file has moved, or the new copy has the same amount of text or less. A PDF that really got shorter therefore keeps its longer old text.
- **Scans:** an `ok` PDF that was already OCRed isn't OCRed again.
- **Resuming:** after an interruption, the same command carries on where it stopped. Once a refresh finishes, the next one starts over.
- **Lanes:** the chosen lanes must be in the run, so `--refresh-pdfs web --only localfile` stops with an error. Other pending URLs still run unless you limit them with `--only`.
- **What counts as a PDF:** a URL recorded as `application/pdf`, or whose path ends in `.pdf`. A failed link with neither can't be recognized.
- **Not included:** arXiv links, which store abstracts only, and imported SingleFile pages.

## Saved pages

`archiver import DIR [DIR|FILE ...]` stores pages saved with the [SingleFile](https://github.com/gildas-lormeau/SingleFile) browser extension like every other document. Subdirectories are searched for `.html` and `.htm` files, and the originals are left alone. These documents appear as the `singlefile` lane, with `source: singlefile`.

- **URL and date:** these come from the comment SingleFile writes at the top of the file, and the date becomes `fetched_at`. A file with no comment uses its `<link rel=canonical>`, and failing that is stored under a `file:` key.
- **Dedup:** URLs go through the same dedup as `archiver run`, and imported URLs are never queued for fetching. A saved copy replaces a failed or pending entry, and replaces an archived document only when it has more text. Complete Stack Exchange and Reddit documents are always kept.
- **Skipped sites:** URLs `archiver run` skips on purpose, such as GitHub, chat apps and X profiles, are stored, since you saved them deliberately.
- **Re-running:** files are tracked by content hash, so only new or changed files are read. Files that failed are tried again.
- **Cleaning:** inlined CSS, scripts and images are cut before parsing, as are elements SingleFile marked hidden. Saved shadow DOM is kept.
- **X pages:** these are read from X's own tweet markup, so a saved conversation keeps every tweet with its author and date, and any X Article.
- **Unsupported:** SingleFile's self-extracting ZIP format is reported as `singlefile_zip`. Use the default HTML format.

## Output

Everything goes in `data/`.

| Path | Contents |
|---|---|
| `manifest.sqlite` | One row per URL: lane, status, reason, HTTP status, final URL, character count, dedup key, fallback passes tried and raw path |
| `md/<id[:2]>/<id>.md` | YAML front matter (url, title, author, date, site, source) and the markdown body |
| `corpus.jsonl` | Written by `archiver export` |
| `audit.sqlite` | Garbage scores and your review decisions |
| `audit-exclude.txt` | Paths of documents marked Bad or Can't fix |
| `ui.json`, `ui-token`, `ui_urls.txt` | The web interface's settings, access token and latest URL list |
| `raw/<id[:2]>/<id>.{html.zst,pdf}` | Only with `--keep-raw`. Budget about 1–1.5 GB for 20,000 pages, plus about 2 MB per PDF |

| Status | Meaning |
|---|---|
| `pending` | Not done yet |
| `ok` | Stored |
| `thin` | Under 200 characters, usually a JavaScript app, paywall or bot wall, or a paper with no abstract. Still saved and exported |
| `failed` | No text. `reason` says why |
| `skipped` | Deliberately not fetched |

`source` records where the text came from, for example `html`, `singlefile`, `pdf:wayback`, `html:render`, `doi:scry`, `reddit:scry+arctic_shift`, `tweet:twitterapi`, `github:api`, `youtube:transcript:auto` or `file:pdf`.

Extraction runs in memory-capped worker processes (`--worker-mem-mb`, default 1536), which are replaced every 500 documents and rebuilt after a crash. Only the document that crashed a worker is lost.
