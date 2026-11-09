# osiris

Archives saved URLs and local files as clean markdown for lexical and semantic indexing. Only text is kept: images and video are never downloaded.

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/osiris ui
```

This opens the [web interface](#web-interface), which covers the whole process. The same work from the command line:

```sh
python3 extract_urls.py    # ../tab-backups -> all_urls.md
.venv/bin/osiris run     # all_urls.md -> data/ (resumable, Ctrl-C safe)
.venv/bin/osiris stats --failed --thin
.venv/bin/osiris export  # data/corpus.jsonl, one document per line
```

Optional system and Python extras:

| For | Install |
|---|---|
| OCR of scanned PDFs | `apt install tesseract-ocr` (plus `tesseract-ocr-tur` etc. for other languages), or `brew install tesseract` |
| `--render` | `.venv/bin/pip install -e .[render]`, then `playwright install chromium` |

## Web interface

`osiris ui` opens one page with three tabs. Every choice and location is saved in `data/ui.json` as you make it, so it's still there after a run or a restart. A job keeps running if you close or reload the page. The terminal shows the same log as the page, with the page's address repeated every 20 lines.

- **Archive:** add folders of tab backups, local files and saved pages with a folder picker, choose options, and press **Start archiving**. That extracts URLs, imports saved pages, archives everything and checks for garbage, showing a live log and a progress bar with the time left. That counts only the sources you ticked. Each source gets its own estimate from its schedule and recent pace, and because sources run side by side, the time left is the slowest one's, not their sum. arXiv's abstracts and full text are added together, since they run one after the other. Fallback passes, OCR and the garbage check aren't included. Tab backup files are hashed, and any whose URLs are already in the manifest are skipped unless the file or the rules for choosing URLs have changed. **Stop** saves progress, and starting again resumes.
- **How to archive it:** the common choices are at the top. **Advanced** groups the rest by what they affect: blocked or logged-in pages, what to do when a page fails, speed, time limits, YouTube, tweets, and papers and forums. Number boxes show their defaults as real values, and emptying one brings its default back. Choosing a cookies file fills in the user agent of the browser you have the page open in. Tick **Edit** to type a different one.
- **Why things failed:** counts failed documents by category and by reason, for all time and for the current or latest run. It updates during a run. Choose a row to list its documents, filter them by site, and open any of them.
- **Review:** documents that look like garbage, worst first. See [Finding garbage](#finding-garbage).
- **Documents:** titles are saved as each document is, so they show and can be searched straight away. Search titles and URLs, or the text inside documents, and filter by source and status. With `--search-index`, one box searches all three, best match first (see [Searching](#searching)). Each document shows its saved text with your search words marked and, where there's one on this machine, its original: your local file, a saved page or a `--keep-raw-pdfs` copy. Web documents link to the live page.
- **Failures:** choose **Failed** to sort failures by reason. Each failed document says in plain words what went wrong, which fallbacks were already tried and what might still work. **Retry this site next run** puts the site on a list under **How to archive it**. The next archive run fetches that site's failed URLs again, like `--retry-host`, and then clears the list.

**Explanations:** hover over any button, option or status to see what it does or means. A word with a dotted underline, such as **thin** or a garbage signal, has an explanation too.

**Keyboard in Review:** `j`/`k` move, `f` fine, `b` bad, `o` OCR it, `r` fetch again, `x` can't fix, `u` undo, `n` write a note.

**Keyboard in Documents:** `/` search, `j`/`k` move, `n`/`N` next or previous match in the open document.

**Links:** the address names the open tab and document, such as `#documents/<id>`, so a document can be bookmarked or opened again after a reload.

**Over ssh:** the page only listens on this machine, so forward the port from your own machine, then open the printed address:

```sh
ssh -N -L 8321:127.0.0.1:8321 <server>
```

**Things to know:**

- **Access:** the page only answers on this machine and refuses requests from other websites. It has no password, so anything else running on this computer can use it, and it can read your files and start jobs. Originals are served only for files already in the manifest, and saved web pages open sandboxed so their scripts can't run.
- **One job at a time:** don't run `osiris run` in a terminal while the page is running a job.
- **YouTube:** the garbage check runs after the archive step, and YouTube can take days. To check sooner, press **Stop**, then **Check for garbage**, or turn YouTube off for that run.
- **Tweets:** runs from the page ignore `TWITTERAPI_IO_KEY` unless you tick **Allow twitterapi.io** under Advanced, which is billed per tweet. **Look up tweets in Scry** turns Scry off or on for tweets. With both off, tweets wait.
- **Older imports:** pages imported before this version only recorded a file name, so they have no Original view.
- **Quitting:** Ctrl-C in the terminal, or closing the terminal, stops the page and any running job, with progress saved.

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
| **Bad** | Left out of `export` and listed in `data/review-exclude.txt`. It stays under **Bad** until you decide otherwise |
| **OCR it** | PDFs only. Reads the PDF with OCR, and replaces the text if the new text scores better, even if it's shorter |
| **Fetch again** | Fetches or reads it from scratch. If the text comes back different, the document waits under **Choose version** with both versions kept. If fetching fails, the earlier document is put back exactly as it was |
| **Can't fix** | Like Bad, but no longer counted as something to come back to |

**Your documents aren't changed when you decide.** OCR and Fetch again are queued, and **Run fixes** carries them out. A decision only holds for the exact text it was about: if the text changes, the document is scored afresh. A fix that didn't help comes back as Bad, with a note saying so, and the document as it was before.

**Choose version** lists documents that Fetch again changed. It shows each version's length and garbage score, and lets you read both. **Keep new** or **Keep earlier** deletes the other version and returns the document to undecided. Until you pick, the new text is the one on disk, and the document is left out of `export`. Run fixes reaches this through its last step, the garbage check. From the command line, run `osiris review scan` after `osiris review apply` and the run it prints.

**Fixed** lists documents whose text OCR or Fetch again changed, where there was no earlier version to choose from. They stay there, however well they score now, until you mark them **Fine** or queue another fix. They aren't left out of exports.

If your indexer reads `data/md` directly, `data/review-exclude.txt` lists the paths to leave out. The file is rewritten by each check.

From the command line: `osiris review scan`, then `osiris review apply`, which also prints the lanes to run for documents queued to fetch again.

## Text cleanup

Converting a document to text leaves damage that splits or hides words, so searching for the right word misses the passage. osiris repairs it as each document is archived, and `osiris clean` repairs documents archived before. It only repairs what has one right answer or clear evidence, and leaves anything unsure as it was printed.

| Repair | Where | Example |
|---|---|---|
| Separated accents joined to their letter (NFC) | Everywhere | `e` + `◌́` → `é` |
| A ligature inside a word spelled out | Everywhere | `ﬁnding` → `finding`, but a lone `ﬁ` stays |
| An invisible character inside a word removed | Everywhere, between letters of scripts that put spaces between words | `co​operate` (zero-width space), `infor­mation` (soft hyphen) → `cooperate`, `information` |
| A byte-order mark at the very start removed | Everywhere | |
| A word broken at a line end rejoined, or given back its hyphen | PDFs and OCR only | `infor-⏎mation` → `information`, `well-⏎known` → `well-known` |

**Never touched:** code in markdown (fenced, indented or inline), spaces of any kind, full-width letters, superscripts and fractions, mojibake, and the damage markers the garbage check looks for (`�`, `(cid:12)`, control characters). There's no spelling or OCR correction, and no folding of case or accents.

**Words broken at line ends** are decided by the first rule that knows, in this order:

1. **Soft hyphen** at a line end: joined.
2. **Chrome-printed PDFs:** Chrome hyphenates with `‐` (U+2010), which is joined. A hyphen pdfium marks as printed was in the page itself, so it stays.
3. **The same document:** if it spells the word elsewhere only one way, or one way at least 3 times as often, that way.
4. **A capital after the break**, as in `Anglo-⏎Saxon`: the hyphen stays.
5. **The rest of the archive:** the same count over all your documents, from `data/lexicon.sqlite`. `osiris clean` builds it, and later runs use it.
6. **Otherwise the hyphen stays**, as printed. A wrong join would invent a word, such as `wellknown`.

Digits on either side (`1990-⏎1995`) and page boundaries are left alone. Plain-text files get no line-end repairs, since books such as Project Gutenberg's wrap lines at spaces, so every hyphen at a line end in them is real.

### Cleaning documents already archived

In the web interface, the **Clean up text** card on the Archive tab has four buttons:

- **Preview** changes nothing, and lists what would change.
- **Clean up text** makes the changes.
- **Undo cleanup** puts the originals back.
- **View report** shows the last report, which you can filter by rule. Choose a row to open its document.

From the command line:

```sh
.venv/bin/osiris clean --dry-run   # changes nothing, and lists what would change in data/clean-report.tsv
.venv/bin/osiris clean
.venv/bin/osiris clean --undo      # puts the originals back
```

- **The report** lists every line-end decision with its rule and context, and a few examples of each other repair per document. Read it after a preview to check for anything that shouldn't change. Its first line, starting with `#`, says whether it's from a preview or a cleanup, and when.
- **Only changed files are rewritten.** Their front matter stays as it was, `fetched_at` included, apart from a repaired title. Character counts in the manifest follow the new text.
- **Your review decisions are kept.** A decision holds for the exact text it was made about, so `clean` carries it over to the repaired text and scores the document again. Without that, a document marked Bad would come back into exports.
- **Undo:** each original is kept, compressed, in `data/clean-backup.sqlite` before its file is replaced. `--undo` puts back every one that hasn't changed since, and leaves alone any that were fetched again.
- **Interrupting** is safe, and running it again carries on. Don't run it while an archive run is going.
- **Text files in other encodings:** local text files whose stored text has `�` are read again from the original, if it's still there, with the encoding detected (see [Local files](#local-files)).

## Searching

Without an index, the web page's **Inside documents** search reads the documents one by one, newest first, and stops at 100 matches. For ranked search, keep a full-text index, `data/search.sqlite`. It's off by default, since it takes disk space and some CPU. To turn it on:

```sh
.venv/bin/osiris ui --search-index
```

The web page then keeps the index up to date by itself: when it starts, every few minutes while a job runs, and when a job ends. A document is read again only when its file has changed. The first build reads everything, and a search finds what's indexed so far. Without the flag the page doesn't create, update or read the index, so `data/search.sqlite` and `data/search.lock` can be deleted.

`osiris search` builds or updates the index whenever you run it, with or without the flag:

```sh
.venv/bin/osiris search kuantum bilgisayar
.venv/bin/osiris search '"lazy dog" -fox' --lane generic --limit 50
.venv/bin/osiris search transformer --json > hits.jsonl   # one JSON object per match
```

- **Words:** every word must appear, in the title, the URL or the text. A word of three or more letters also matches longer words that start with it, so `paper` finds `papers`.
- **Phrases and exclusions:** `"quoted words"` must appear together, and `-word` or `-"some words"` leaves out documents containing them. Quote a query with a `-word` in it on the command line.
- **Ranking:** best first by BM25. A match in the title counts most, then the URL, then the text.
- **Accents:** these are ignored, so `cafe` finds `café`. There's no stemming, since the archive mixes languages.
- **Size:** on real text the index took about 60% as much space as `md/`. About half of that is a compressed copy of each document's text, which lets a changed document be taken out of the index again.
- **Failed and waiting documents:** these have no text, so they're not in the index. In the web page, with **Failed** or **Waiting** chosen, the search box filters by URL and title.

These rules apply to the index. The plain **Inside documents** scan matches exactly what you type, ignoring case.

If the web page is updating the index at the time, `osiris search` waits for it to finish.

## Your originals

Nothing in osiris deletes, moves or changes your local files, tab backups or saved pages. They're only opened for reading, and the web interface has no way to delete, rename or upload anything.

- **Guard:** every osiris process refuses, with a `PermissionError`, any attempt by Python code, including third-party parsers, to create, change or delete a file outside the output folder, the system temp folder and `~/.cache` (`osiris/safety.py`). It can't see other programs or native code that makes its own system calls: tesseract only gets page images on stdin, and pdfium only reads.
- **Output paths:** `extract_urls.py --output` won't write into the backup folder, or over anything that isn't a list of URLs. `osiris export -o` won't overwrite anything but an earlier export.
- **`--chrome-profile`:** this needs a copy, because Chrome rewrites the profile it runs on. Standard real profile locations are refused. The option isn't in the web interface.

**What can change:** files in `data/`. **OCR it** replaces a document's saved text when the new text is better, and **Fetch again** when you choose the new version, though neither touches the original. `extract_urls.py` rewrites `all_urls.md` by default, so keep hand-written lists in another file.

## Commands

| Command | What it does |
|---|---|
| `osiris ui` | Opens the web interface. `--search-index` adds [ranked search](#searching) |
| `osiris review {scan,apply}` | Finds probable garbage to review, or carries out the fixes you queued |
| `osiris run [FILE]` | Fetches and extracts every pending URL. `FILE` defaults to `all_urls.md`, and `/dev/stdin` works |
| `osiris import DIR [DIR\|FILE ...]` | Stores pages saved with SingleFile |
| `osiris ocr` | Runs only OCR, with `--retry` and `--limit` |
| `osiris clean` | Repairs text in documents already archived. `--dry-run`, `--undo`. See [Text cleanup](#text-cleanup) |
| `osiris search QUERY` | Full-text search, best match first. `--lane`, `--limit`, `--json`. See [Searching](#searching) |
| `osiris stats` | Counts by lane and status. `--failed` and `--thin` add the top hosts |
| `osiris export` | Writes `data/corpus.jsonl`, leaving out documents marked Bad or Can't fix |

Common `osiris run` options:

| Option | Effect |
|---|---|
| `--only LANE`, `--skip LANE` | Run only this lane, or leave its URLs pending. Repeatable |
| `--retry-failed` | Re-queue all failed URLs |
| `--retry-host 'scholar.google.*'` | Re-queue failed URLs on matching hosts only. Repeatable |
| `--refresh-pdfs {web,local,both}` | Read every PDF again, failed or not. See [Refreshing PDFs](#refreshing-pdfs) |
| `--no-ocr`, `--ocr-lang eng+tur` | Turn off automatic OCR, or set its languages |
| `--keep-raw-pdfs` | Also keep originals under `data/raw/`, each named after its document: PDFs from the web, and arXiv papers' HTML, or their PDF if they have no HTML. Ordinary web pages and your local files aren't kept |
| `--no-arxiv-fulltext` | arXiv papers keep only their abstracts this run |
| `--refetch-arxiv-pdfs` | Look again at arXiv's web version for papers whose full text came from a PDF. It replaces the PDF text unless it's less than half as long |
| `--scry-arxiv`, `--scry-arxiv-max-dollars N` | Take a paper's full text from Scry when it has one, and don't ask arXiv for that paper. Far faster, but formulas are dropped. Papers with only their abstract are affected, and a run stops taking them after spending N dollars (5) |
| `--pdf-at-once N` | Web PDFs being downloaded or read at once. The default is twice `--workers`, at least 3 |
| `--cookies FILE`, `--user-agent UA` | Use your browser session. See [Browser session](#browser-session) |
| `--render` | Add the headless browser pass |
| `--no-impersonate`, `--no-doi`, `--no-wayback` | Skip a fallback pass |
| `--workers N`, `--worker-mem-mb N` | Extraction and OCR processes, and the memory cap for each |
| `--scry-youtube`, `--scry-youtube-max-dollars N` | Take a video's transcript from Scry when it has one, so that video never touches YouTube. Sends your video IDs to Scry. A run stops taking them after spending N dollars (1), and the rest come from YouTube |
| `--no-scry-tweets` | Don't look tweets up in Scry. They then come from twitterapi.io if keyed, and otherwise stay pending |
| `--tries N`, `--max-retry-wait N`, `--site-down-after N`, `--site-refused-after N` | How hard to retry a web page, and when to give up on a site |
| `--page-timeout N`, `--stall-timeout N`, `--read-timeout N`, `--book-timeout N`, `--ocr-page-timeout N`, `--ocr-document-timeout N` | Time limits in seconds. Raise `--page-timeout` (30) if large PDFs time out |
| `--arxiv-delay N`, `--wayback-delay N`, `--stackexchange-delay N` | Seconds between requests to those services |
| `--ocr-dpi N`, `--thin-under N` | How sharply OCR renders pages, and the character count below which a document is thin |

`osiris run --help` lists the rest.

## Adding more to the archive

`osiris run` only queues URLs whose dedup key isn't already in the manifest, so it's safe to repeat. The key comes from `osiris/dedup.py`, which `extract_urls.py` shares. It removes tracking parameters and treats `twitter.com` as `x.com`, `www.`, `m.` and `old.` hosts as the same site, and arXiv abs, pdf and html links as one paper.

To archive tab backups and a folder of local files in one run:

```sh
python3 extract_urls.py --input-dir ~/tab_backups
find ~/local_files -type f -print0 | python3 -c 'import sys,pathlib; [print(pathlib.Path(p).resolve().as_uri()) for p in sys.stdin.read().split("\0") if p]' >> all_urls.md
.venv/bin/osiris run
```

`extract_urls.py` rewrites `all_urls.md` each time, so run it first. It keeps web pages and `file://` links, and drops Google except Scholar, Gmail, local servers and browser-internal links such as `chrome://` and `about:`.

- **Failed URLs:** each gets every fallback pass once and is then left alone until you use `--retry-failed` or `--retry-host`. A pass that couldn't run doesn't count: if the headless browser isn't installed, that pass is skipped with one error and its links wait for a later run.
- **Routing changes:** every run applies the current routing rules to URLs that aren't archived yet.
- **Moving to another machine:** copy `data/` with the repo, but not `.venv`, after the run has stopped.

## API keys

All are optional.

All keys go in one file, `.env` in the osiris folder. Copy `.env.example` to `.env` and fill in what you have. It's ignored by git, and a key in it takes priority over one set in your shell.

| Variable | Used for | If unset |
|---|---|---|
| `SCRY_API_KEY` | Tweets, Reddit threads and DOI abstracts from [Scry](https://scry.io)'s archives | Those sources are skipped |
| `GITHUB_TOKEN` | GitHub lane, 5,000 API calls an hour instead of 60. A token with no scopes is enough for public repos | GitHub URLs stay pending |
| `TWITTERAPI_IO_KEY` | Tweets Scry lacks, from twitterapi.io, billed per tweet | They're marked `not_in_scry` |
| `STACKEXCHANGE_KEY` | 10,000 Stack Exchange API calls a day instead of 300 | The lower quota applies |
| `OPENALEX_API_KEY` | Your own OpenAlex budget | The shared per-IP budget applies |

## Lanes

Each URL goes to the cheapest source that actually returns its text.

| Lane | URLs | How |
|---|---|---|
| `twitter` | `x.com/…/status/<id>` | Scry's archive, which also holds some deleted tweets, then twitterapi.io if keyed |
| `arxiv` | `arxiv.org/{abs,pdf,html}/<id>` | Export API, 100 IDs per call, for title, authors and abstract. Once every abstract is in (if any couldn't be fetched, full text waits for the next run), the full text, whichever form of link you saved: arXiv's HTML, or the PDF if there is none. One request every 15 seconds, as arXiv's robots.txt asks, with papers read in the background so a slow one doesn't hold up the next. A large first run takes hours; it's safe to stop and resumes. If this machine is too busy to read a paper's HTML, the next run tries it again before settling for the PDF |
| `stackexchange` | `*/questions/<id>` on SE sites | API, 100 IDs per call. Question, all answers and comments as markdown |
| `reddit` | `reddit.com/…/comments/<id>`, `redd.it/<id>` | Post and comment tree from Scry, then Arctic Shift for what Scry lacks |
| `hackernews` | `news.ycombinator.com/item?id=<id>` | Algolia's item API: the story, or one comment, with every reply |
| `lesswrong` | Posts on `lesswrong.com`, `alignmentforum.org`, `greaterwrong.com` | GraphQL API: the full post as markdown with every comment A post LessWrong no longer shows to visitors comes from Scry's archive instead, with the source `lesswrong:scry`. |
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
.venv/bin/osiris run --only stackexchange --refetch-stackexchange
```

Without `STACKEXCHANGE_KEY` the API allows 300 calls a day. If that runs out, the same command carries on the next day. A question deleted since it was archived keeps its document.

### Hacker News and LessWrong

New links go to these lanes automatically. This command fetches every thread already archived, including pages the generic lane saved before these lanes existed, and you can repeat it to pick up new comments:

```sh
.venv/bin/osiris run --only hackernews --only lesswrong --refetch-comments
```

Add `--refetch-comments-days 30` to leave out threads these lanes fetched in the last 30 days. Pages saved before the lanes existed are always included.

A document changes only when the API returns at least as much text as it has, so a thread deleted since keeps what was archived. A page moved from the generic lane gets a new document id: its old file is removed once the new one is written, and any Review decision on it is dropped so the new text gets a new look. Pages you saved with SingleFile are left alone.

### YouTube

Run this lane from a home connection, because YouTube blocks most datacenter IPs.

- **Transcripts:** these come from `youtube_transcript_api`, English first and then the video's own language, with human captions ahead of automatic ones. A video with no captions is stored as `thin`, with its title and channel.
- **Playlists and channels:** these are read with `yt-dlp`, up to 2,000 video titles without their transcripts.
- **Rate limits:** videos go one at a time, a random `--youtube-delay MIN-MAX` (10-16) seconds apart, which is about 240 an hour. YouTube publishes no limit. The figure usually quoted is yt-dlp's, about 300 videos an hour without signing in. The lane has its own worker thread, so a block here can't hold up other documents.
- **Blocks:** a blocked item stays `pending`. The lane pauses for 30, 60, 120, then 240 minutes (`--youtube-block-wait`), and after 5 blocks in a row (`--youtube-block-limit`) stops until the next run. A block only stops counting towards the next pause after 25 videos in a row go through.
- **From Scry instead:** with `--scry-youtube`, videos Scry has a transcript for never touch YouTube. That's only part of a list: Scry had usable transcripts for 43% of a sample of 400 YouTube links from Hacker News. It's also expensive for bulk use, because Scry counts YouTube data 20 times over against its free 20 MB a day and the price rises as you take more. The free allowance covers about 55 transcripts a day, $1 buys about 300 and $5 about 700, while YouTube itself is free at about 240 an hour. Human-made captions Scry stored without line breaks are skipped, because their words run together at the breaks. Scry's source is marked `youtube:transcript:auto:scry` or `:human:scry`.
- **One video refused:** YouTube's bot check can refuse one video while others go through. That counts as a block only if the next video is refused too. Otherwise the video is `failed` with reason `bot_check`, and `--retry-failed` tries it again. A 429 or a captcha page is always a block.
- **After a block:** the block is remembered in `youtube_block.json` in the archive folder. A new run waits out what's left of the pause, and for 24 hours the lane goes at half speed. `--youtube-block-wait 0` skips the wait, which helps after changing network. Failing to reach YouTube at all isn't counted as a block.
- **Finishing early:** once only YouTube is left, the log says so. Ctrl-C is safe, and the next run resumes.

### Local files

**Your files are only ever read.** Nothing moves, changes, deletes or copies them.

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
- **Text that isn't UTF-8:** UTF-8 and files with a byte-order mark are read as such. Anything else is tried in each common legacy encoding, such as Windows cp1252 English with curly quotes, cp1254 Turkish or cp1251 Russian. A reading is kept only if it's beyond doubt: it has no control characters, symbols where letters belong or words mixing scripts, and no other reading is as plausible. When readings tie, the archive's vocabulary decides, by a clear margin only. Otherwise undecodable bytes become `�`, which the garbage check flags, rather than wrong letters that look right. Web pages served as plain text are read the same way, after any charset the server declares.

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
.venv/bin/osiris run --cookies ~/cookies.txt --user-agent "<your browser's UA>" --render
```

- `--cookies FILE` takes a Netscape `cookies.txt` or a JSON cookie export. Each cookie is sent only to its own site, including across redirects.
- `--user-agent` should match the browser the cookies came from, because Cloudflare ties `cf_clearance` to it.
- `--chrome-profile DIR` makes `--render` use a copy of your Chrome profile directory.

The cookie file holds live sessions, so keep it private.

## PDFs

Every page of a PDF is read, however long it is. The time allowed grows with the page count.

Files of any size are accepted. What's left to stop a runaway file is each worker's memory cap (`--worker-mem-mb`), which fails that one file as `too_large`, and the time limits on downloading and reading it.

### OCR

`osiris run` reads PDFs with no usable text with [tesseract](https://github.com/tesseract-ocr/tesseract), both web and local. It starts once the generic and local-file lanes finish, so a long scan holds up nothing, and it doesn't wait for YouTube.

- **Scope:** it covers only the lanes in that run. `--only localfile` leaves web PDFs alone, and `--only youtube` does no OCR.
- **Earlier PDFs:** it also works through PDFs left from earlier runs, so the first run after installing tesseract can take a while.
- **Files:** local files are read in place and never changed. Web PDFs are downloaded again to a temporary file, which is then deleted.
- **Speed:** the work is split by page, with `--workers` pages read at once, so every core stays busy even on a single long book.
- **Tries:** each PDF is tried once. Ctrl-C stops at once, and the PDF in progress is tried again next run.
- **Results:** text is kept only if there's more of it than before, with `source: pdf:ocr` or `file:pdf:ocr`.
- **Languages:** `--ocr-lang eng+tur` sets them, after installing their packs. The default is English. List only what you need, since each extra language is slower and can lower accuracy.
- **Missing tesseract:** if tesseract or a language pack is missing, the run logs a warning and carries on, and those PDFs wait without using up their try.
- **Pictures of letters:** some old TeX papers draw every letter as a numbered picture, so reading them gives codes instead of text. A PDF whose text is more than 5% control characters is treated as having no text, so OCR reads it.
- **Limitation:** a PDF with a few readable pages and many scanned ones counts as having text, so it isn't picked up.

`osiris ocr` runs OCR on its own. It takes `--lang`, `--workers`, `--limit`, and `--retry` to try PDFs again, for example in another language.

### Refreshing PDFs

```sh
.venv/bin/osiris run --refresh-pdfs web     # web PDFs only
.venv/bin/osiris run --refresh-pdfs local   # local PDFs only
.venv/bin/osiris run --refresh-pdfs both
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
- **Not included:** arXiv links, which the `arxiv` lane handles, and imported SingleFile pages.

## Saved pages

`osiris import DIR [DIR|FILE ...]` stores pages saved with the [SingleFile](https://github.com/gildas-lormeau/SingleFile) browser extension like every other document. Subdirectories are searched for `.html` and `.htm` files, and the originals are left alone. These documents appear as the `singlefile` lane, with `source: singlefile`.

- **URL and date:** these come from the comment SingleFile writes at the top of the file, and the date becomes `fetched_at`. A file with no comment uses its `<link rel=canonical>`, and failing that is stored under a `file:` key.
- **Dedup:** URLs go through the same dedup as `osiris run`, and imported URLs are never queued for fetching. A saved copy replaces a failed or pending entry, and replaces an archived document only when it has more text. Complete Stack Exchange, Reddit, Hacker News and LessWrong documents are always kept.
- **Skipped sites:** URLs `osiris run` skips on purpose, such as GitHub, chat apps and X profiles, are stored, since you saved them deliberately.
- **Re-running:** files are tracked by content hash, so only new or changed files are read. Files that failed are tried again.
- **Cleaning:** inlined CSS, scripts and images are cut before parsing, as are elements SingleFile marked hidden. Saved shadow DOM is kept.
- **X pages:** these are read from X's own tweet markup, so a saved conversation keeps every tweet with its author and date, and any X Article.
- **Unsupported:** SingleFile's self-extracting ZIP format is reported as `singlefile_zip`. Use the default HTML format.

## Output

Everything goes in `data/`.

| Path | Contents |
|---|---|
| `manifest.sqlite` | One row per URL: lane, status, reason, HTTP status, final URL, character count, dedup key, fallback passes tried and raw path |
| `md/<id[:2]>/<id>.md` | YAML front matter (url, title, author, date, site, source, and `raw` when an original was kept) and the markdown body |
| `corpus.jsonl` | Written by `osiris export` |
| `review.sqlite` | Garbage scores and your review decisions. An archive's older `audit.sqlite` is renamed to this when it's first opened |
| `review-exclude.txt` | Paths of documents marked Bad or Can't fix |
| `lexicon.sqlite` | Words and hyphenated pairs seen at least twice in the archive, for rejoining words broken at line ends. Built by `osiris clean` |
| `clean-report.tsv`, `clean-backup.sqlite` | What the last `osiris clean` changed, and the originals `--undo` puts back |
| `search.sqlite`, `search.lock` | Only with `osiris ui --search-index` or `osiris search`: the full-text index, rebuilt from `md/` if deleted, and the lock that keeps two updates from running at once |
| `ui.json`, `ui_urls.txt`, `ui_inputs.txt` | The web interface's settings, latest URL list and the tab backups behind it |
| `raw/<id[:2]>/<id>.{html.zst,pdf}` | Only with `--keep-raw-pdfs`; the same id as the document in `md/`. `.html.zst` is arXiv's HTML. Budget about 2 MB per PDF |

| Status | Meaning |
|---|---|
| `pending` | Not done yet |
| `ok` | Stored |
| `thin` | Under 200 characters, usually a JavaScript app, paywall or bot wall, or a paper with no abstract. Still saved and exported |
| `failed` | No text. `reason` says why |
| `skipped` | Deliberately not fetched |

`source` records where the text came from, for example `html`, `singlefile`, `pdf:wayback`, `html:render`, `doi:scry`, `reddit:scry+arctic_shift`, `tweet:twitterapi`, `github:api`, `youtube:transcript:auto` or `file:pdf`.

Extraction runs in memory-capped worker processes (`--worker-mem-mb`, default 1536), which are replaced every 500 documents and rebuilt after a crash. Only the document that crashed a worker is lost.

## Tests

```sh
.venv/bin/pip install -e .[test]
.venv/bin/python -m pytest
```

One test prints a page to PDF with Chrome, and is skipped if there's none.
