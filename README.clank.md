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
| DjVu files | `apt install djvulibre-bin`, or `brew install djvulibre` |
| `--render` | `.venv/bin/pip install -e .[render]`, then `playwright install chromium` |

## Web interface

`osiris ui` opens one page with two tabs, **Archive** and **Documents**. Every choice and location is saved in `data/ui.json` as you make it, so it's still there after a run or a restart. A job keeps running if you close or reload the page. The terminal shows the same log as the page, with the page's address repeated every 20 lines.

- **Archive:** add folders of tab backups, local files, tweet files and AI chats with a folder picker, choose options, and press **Start archiving**. That extracts URLs, imports tweet files, AI chats and the web pages in your local folders while **Local files** is ticked under Sources, archives everything and checks for garbage, showing a live log and a progress bar with the time left. The log ends with what the run added, by source, with how many of those documents had failed before and how many thin ones now have full text, and `data/added.tsv` lists them. Fixes end the same way. That counts only the sources you ticked. Each source gets its own estimate from its schedule and recent pace, and because sources run side by side, the time left is the slowest one's, not their sum. arXiv's abstracts and full text are added together, since they run one after the other. So are waiting web links and **Refresh PDFs**' web PDFs, which are read after them. Local PDFs read again count with the local files. The fallbacks for failed and thin web pages and OCR count too, one item per page and fallback still to come, after the waiting links. An account in a list of accounts counts as one item, done when the run has taken all its new tweets. The total is counted at the start and only grows when the run finds work it couldn't have known about, such as a page that just failed and now gets fallbacks; it's kept even while no page is open. The garbage check isn't included. Tab backup files are hashed, and any whose URLs are already in the manifest are skipped unless the file or the rules for choosing URLs have changed. **Stop** saves progress, and starting again resumes.
- **LLM summary:** the small **LLM summary of current status and issues** link under the log asks DeepSeek's `deepseek-flash` for a few lines on what's waiting, failed or thin, why, and what to do about it, ranked by how many documents each issue affects. It works before, during or after a run, and the answer is added to the log. DeepSeek is sent detailed counts from the manifest by source, reason, site, HTTP status, redirect target and day, with some example links of each, your settings (whether a cookies file is set, not its path), the end of the log and this README. No document's text is sent. A summary takes under a minute, reads about 30,000 tokens, costs 1 or 2 cents and needs `DEEPSEEK_API_KEY`.
- **How to archive it:** the common choices are at the top. **Advanced** groups the rest by what they affect: blocked or logged-in pages, what to do when a page fails, speed, time limits, YouTube, tweets, and papers and forums. Number boxes show their defaults as real values, and emptying one brings its default back. Choosing a cookies file fills in the user agent of the browser you have the page open in. Tick **Edit** to type a different one.
- **Why things failed:** counts failed documents by category and by reason, for all time and for the current or latest run. It updates during a run. Choose a row to list its documents, filter them by site, and open any of them.
- **Documents:** everything you've saved, with garbage scores and ways to fix each document. Titles are saved as each document is, so they show and can be searched straight away. Two boxes and five filters narrow the list together: a document shows only if it passes every one. The first box wants every word in the title or URL, in any order; `site:example.com` keeps that site and its subdomains, and several `site:` words mean any of them. The second box, **Inside documents**, searches the saved text alone. Each box lists your last 20 searches in it as you type, newest first, kept in this browser only. The filters are source, status, reason and decision, plus a row of tag ticks for the garbage check's tags (block page, encoding, few common words, odd words, symbols, no spaces, repeated lines), as shown on each document. A document shows if any of its tags is ticked; with **All tags** ticked, documents with no tag show too, including ones never checked. All are ticked at first, which leaves nothing out. Clicking **All tags** ticks or unticks every tag, while a single tag never changes it, so All tags with nothing else ticked lists only the untagged documents. The list loads more as you scroll to its end, adding rows below without moving what you're looking at. While a job runs it refreshes itself every 10 seconds and once when the job ends, keeping as many rows as you've loaded and the row at the top of your view in place; the open document isn't redrawn, and a text search without the index isn't repeated, since it reads every file. **All bad** and **All fine** apply to the same filters, and are hidden while the text box is in use. **Review** sorts whatever is listed by garbage score, worst first, keeping your search and filters; the number on it counts documents flagged and not decided yet. See [Finding garbage](#finding-garbage). **Delete…** removes a document's saved text and osiris's copy of its original for good, after asking. Its links stay in the manifest marked `deleted`, so later runs don't fetch them again from your tab backups. Your own files, such as local files and saved pages, are never touched. It waits while a job is running. With `--search-index`, the text box uses the index, best match first (see [Searching](#searching)). Each document shows its saved text with your search words marked and, where there's one on this machine, its original: your local file, a saved page or a `--keep-raw-pdfs` copy. Web documents link to the live page.
- **Failures:** choose **Failed** to sort failures by reason. Each failed document says in plain words what went wrong, which fallbacks were already tried and what might still work. **Retry this site next run** puts the site on a list under **How to archive it**. The next archive run fetches that site's failed URLs again, like `--retry-host`, and then clears the list.

**Explanations:** hover over any button, option or status to see what it does or means. A word with a dotted underline, such as **thin** or a garbage signal, has an explanation too.

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
- **Tweets:** runs from the page ignore `TWITTERAPI_IO_KEY` unless you tick **Allow twitterapi.io** under Advanced, which is billed per tweet. With it ticked, the accounts in your lists also get all their tweets from it, up to the dollar limit next to it. **Look up tweets in Scry** turns Scry off or on for tweets, and it has a dollar limit too. With both off, tweets wait and listed accounts are skipped.
- **Older imports:** pages imported before this version only recorded a file name, so they have no Original view.
- **Quitting:** Ctrl-C in the terminal, or closing the terminal, stops the page and any running job, with progress saved.

## Finding garbage

**Check for garbage** scores every document from cheap text signals. Anything scoring 0.35 or higher is flagged, and **Review** on the Documents tab lists the worst first. Unchanged documents aren't scored again. It's a ranking for you to read, not a verdict, so expect some false alarms, such as tables, code and languages other than English and Turkish.

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
| **OCR it** | PDFs only. Reads the PDF with OCR in the language chosen next to it. If the text comes back different, the document waits under **Choose version** with both versions kept, as with Fetch again. If OCR reads nothing, the earlier text stays |
| **Fetch again** | Fetches or reads it again, the way chosen next to it (below). For a web page or PDF, if that way doesn't work, the others follow in a run's order (the plain download, impersonate, browser, DOI, Wayback, Scry's crawl if ticked, then OCR), skipping the one already tried. The old length is forgotten first, so whatever comes back is kept to compare. If the text comes back different, the document waits under **Choose version** with both versions kept. If fetching fails, the earlier document is put back exactly as it was |
| **Can't fix** | Like Bad, but no longer counted as something to come back to |

Any document can be decided on, including ones the check hasn't scored yet and ones that failed. Clicking the chosen decision again undoes it. For **OCR it** and **Fetch again** that takes the document off the queue, as long as the fix is still waiting and the same way is chosen next to it; once a running job has taken the fix, it can't be called back.

| Way to fetch again | For |
|---|---|
| The usual way | Every document except saved pages: what an archive run does, with your settings from the Archive tab (cookies, headless browser, Scry options, YouTube pacing) and the fallbacks |
| With a browser's fingerprint, in the headless browser, the Wayback Machine's copy | Web pages: that one way, from the start. The headless browser is offered once Playwright is installed |
| Scry's copy of the page | Web pages, with a Scry key: the text Scry's crawler last read from that address |
| Title and abstract by DOI | Paper links with a DOI in them |
| arXiv's full text again, web version first; full text from Scry | arXiv papers. Scry's copy replaces the full text you have |
| Transcript from Scry | YouTube videos, with a Scry key |
| Scry's copy of the post | LessWrong posts, with a Scry key |
| From twitterapi.io | Tweets. It's billed per tweet, so the page asks first, and only a run with such a fix queued gets the key |

Fetching again the usual way picks up new comments on Hacker News, LessWrong and Stack Exchange threads.

**OCR it** and **Fetch again** start at once, for that document only: nothing else waiting in the archive is fetched. osiris runs one job at a time, so if a job is already running, the fix starts by itself as soon as that job ends, carrying on in the same log. After **Stop**, waiting fixes stay put until you press **Run fixes**. After a fixes job, only fixes asked for during it start by themselves, so one that can't work, such as OCR without tesseract, doesn't loop. The other decisions don't change your documents. A decision only holds for the exact text it was about: if the text changes, the document is scored afresh. A fix that didn't help comes back as Bad, with a note saying so, and the document as it was before.

**Choose version** lists documents that Fetch again or OCR it changed. Both versions show side by side, earlier on the left and new on the right, each with its length, garbage score and where its text came from (such as `pdf:ocr`). Scrolling one scrolls the other to the same share of its length. **Keep this one** keeps that side and deletes the other, then returns the document to undecided. Documents waiting for a choice, and those under **Fixed**, come first in every list whatever the filters, except with **Review** on, which orders by garbage score alone. Until you pick, the new text is the one on disk, and the document is left out of `export`. Keeping the earlier text after OCR still counts the PDF as OCR'd, so a later run's OCR pass won't replace it. Run fixes reaches this through its last step, the garbage check. From the command line, run `osiris review scan` after `osiris run --fixes`.

**Fixed** lists documents whose text OCR or Fetch again changed, where there was no earlier version to choose from. They stay there, however well they score now, until you mark them **Fine** or queue another fix. They aren't left out of exports.

If your indexer reads `data/md` directly, `data/review-exclude.txt` lists the paths to leave out. The file is rewritten by each check.

From the command line: `osiris review scan` scores documents, and `osiris run --fixes` carries out the queued fixes with that run's other options, such as `--cookies` and `--render`. Run `osiris review scan` afterwards to settle them.

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

Without an index, the web page's **Inside documents** box reads the files of the documents the other box and filters leave, in the list's order, and stops at 100 matches. Every word must appear, ignoring case. It starts when you press Enter. For ranked search, keep a full-text index, `data/search.sqlite`. It's off by default, since it takes disk space and some CPU. To turn it on:

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

- **Words:** every word must appear, in the title, the URL or the text. The web page's **Inside documents** box looks in the text only, among the documents its other box and filters leave. A word of three or more letters also matches longer words that start with it, so `paper` finds `papers`.
- **Phrases and exclusions:** `"quoted words"` must appear together, and `-word` or `-"some words"` leaves out documents containing them. Quote a query with a `-word` in it on the command line.
- **Ranking:** best first by BM25. A match in the title counts most, then the URL, then the text.
- **Accents:** these are ignored, so `cafe` finds `café`. There's no stemming, since the archive mixes languages.
- **Size:** on real text the index took about 60% as much space as `md/`. About half of that is a compressed copy of each document's text, which lets a changed document be taken out of the index again.
- **Failed and waiting documents:** these have no text, so they're not in the index. In the web page, with **Failed** or **Waiting** chosen, the text box is set aside and the title box and filters still apply.

These rules apply to the index. Without it, the **Inside documents** scan matches each word exactly as typed, ignoring case.

If the web page is updating the index at the time, `osiris search` waits for it to finish.

## Your originals

Nothing in osiris deletes, moves or changes your local files, tab backups, saved pages or chat exports. They're only opened for reading, and the web interface has no way to delete, rename or upload anything.

- **Guard:** every osiris process refuses, with a `PermissionError`, any attempt by Python code, including third-party parsers, to create, change or delete a file outside the output folder, the system temp folder and `~/.cache` (`osiris/safety.py`). It can't see other programs or native code that makes its own system calls: tesseract only gets page images on stdin, and pdfium and DjVuLibre's tools only read.
- **Output paths:** `extract_urls.py --output` won't write into the backup folder, or over anything that isn't a list of URLs. `osiris export -o` won't overwrite anything but an earlier export.
- **`--chrome-profile`:** this needs a copy, because Chrome rewrites the profile it runs on. Standard real profile locations are refused. The option isn't in the web interface.

**What can change:** files in `data/`. **OCR it** and **Fetch again** replace a document's saved text when you choose the new version, though neither touches the original. **Delete…** removes a document's files from `data/` only. `extract_urls.py` rewrites `all_urls.md` by default, so keep hand-written lists in another file.

## Commands

| Command | What it does |
|---|---|
| `osiris ui` | Opens the web interface. `--search-index` adds [ranked search](#searching) |
| `osiris review scan` | Scores new and changed documents for probable garbage, and settles the fixes that ran |
| `osiris run [FILE]` | Fetches and extracts every pending URL. `FILE` defaults to `all_urls.md`, and `/dev/stdin` works |
| `osiris import DIR [DIR\|FILE ...]` | Stores web pages saved as HTML files, such as SingleFile's. See [Saved pages](#saved-pages) |
| `osiris import-tweets DIR [DIR\|FILE ...]` | Stores tweets from Twitter's export zips, bookmark exports, Community Archive files and `fetch_tweets.py`'s databases. See [Tweet files and account lists](#tweet-files-and-account-lists) |
| `osiris import-chats DIR [DIR\|FILE ...]` | Stores conversations from Claude's, ChatGPT's and DeepSeek's exports and the Claude Conversation Exporter extension, and sessions with Claude Code, pi and omp from their logs. See [AI chats](#ai-chats) |
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
| `--no-scry-tweets`, `--scry-tweets-max-dollars N` | Don't look tweets up in Scry, so that they come from twitterapi.io if keyed and otherwise stay pending. Or stop asking Scry for tweets once a run has spent N dollars on them (1), and leave the rest for the next run |
| `--twitter-accounts PATH`, `--twitterapi-max-dollars N` | Also save every tweet of the accounts in a list, and stop taking them from twitterapi.io once a run has spent about N dollars (5). See [Every tweet of an account](#every-tweet-of-an-account). Repeatable |
| `--tries N`, `--max-retry-wait N`, `--site-down-after N`, `--site-refused-after N` | How hard to retry a web page, and when to give up on a site |
| `--page-timeout N`, `--stall-timeout N`, `--read-timeout N`, `--book-timeout N`, `--ocr-page-timeout N`, `--ocr-document-timeout N` | Time limits in seconds. Raise `--page-timeout` (30) if large PDFs time out |
| `--arxiv-delay N`, `--wayback-delay N`, `--stackexchange-delay N` | Seconds between requests to those services |
| `--ocr-dpi N`, `--thin-under N` | How sharply OCR renders pages, and the character count below which a document is thin |

A run ends with the table of every lane's counts and then with what it added: how many documents, by source, how many of them had failed before, and how many thin documents now have full text. `data/added.tsv` lists them. `--no-summary` leaves that out.

`osiris run --help` lists the rest.

## Adding more to the archive

`osiris run` only queues URLs whose dedup key isn't already in the manifest, so it's safe to repeat. The key comes from `osiris/dedup.py`, which `extract_urls.py` shares. It removes tracking parameters and treats `twitter.com` as `x.com`, `www.`, `m.` and `old.` hosts as the same site, and arXiv abs, pdf and html links as one paper.

To archive tab backups and a folder of local files in one run, as the web page does:

```sh
python3 extract_urls.py --input-dir ~/tab_backups
find ~/local_files -type f ! -iname '*.html' ! -iname '*.htm' -print0 | python3 -c 'import sys,pathlib; [print(pathlib.Path(p).resolve().as_uri()) for p in sys.stdin.read().split("\0") if p]' >> all_urls.md
.venv/bin/osiris import ~/local_files
.venv/bin/osiris run
```

Web pages are imported as [saved pages](#saved-pages) rather than listed, so a page saved with SingleFile is filed under the address it came from.

`extract_urls.py` rewrites `all_urls.md` each time, so run it first. It reads `bookmarks_*.html`, `session-buddy-*.json` and `more.txt` directly in the folder, not in subfolders, and skips other `.txt` files. `more.txt` is a list you write yourself: one link per line, where only the first word of a line counts and a link without `https://`, such as `example.com/page`, gets it added. Editing a backup file makes the next run read it again, adding only the links that are new. It keeps web pages and `file://` links, and drops Google except Scholar, Gmail, local servers and browser-internal links such as `chrome://` and `about:`. Google's redirect links are replaced by the page they lead to: Scholar's result links (`scholar.google.com/scholar_url?url=…`) and search results (`google.com/url?q=…`), so osiris fetches the paper or page without asking Google. osiris does the same for any such link it's given directly.

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
| `TWITTERAPI_IO_KEY` | Tweets Scry lacks, and every tweet of the accounts in your lists, from twitterapi.io, billed per tweet | Tweets Scry lacks are marked `not_in_scry`, and listed accounts come from Scry alone |
| `STACKEXCHANGE_KEY` | 10,000 Stack Exchange API calls a day instead of 300 | The lower quota applies |
| `OPENALEX_API_KEY` | Your own OpenAlex budget | The shared per-IP budget applies |
| `DEEPSEEK_API_KEY` | The page's LLM summary of current status and issues, written by DeepSeek's `deepseek-flash` | The summary says it needs the key |

## Lanes

Each URL goes to the cheapest source that actually returns its text.

| Lane | URLs | How |
|---|---|---|
| `twitter` | `x.com/…/status/<id>` | Scry's archive, which also holds some deleted tweets, then twitterapi.io if keyed. A tweet is known by its ID alone, so a link to a saved tweet under a different username isn't fetched again. Scry is asked for 80 tweets at a time, the most it sends in one answer, and a run stops asking once it has spent `--scry-tweets-max-dollars` (1) on tweets. The rest wait for the next run |
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

`skip` covers GitHub user, organization and site pages, other YouTube pages (search, feeds, Watch Later, Liked), unreadable local files, links to chats (ChatGPT, Claude, Gemini, DeepSeek, Grok), X profiles and searches, media files and local hosts. [Skipped](#skipped) lists each reason.

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
- **From Scry instead:** with `--scry-youtube`, videos Scry has a transcript for never touch YouTube. That's only part of a list: Scry had usable transcripts for 43% of a sample of 400 YouTube links from Hacker News. It's also expensive for bulk use, because Scry counts YouTube data 20 times over against its free 20 MB a day and the price rises as you take more. The free allowance covers about 55 transcripts a day, $1 buys about 300 and $5 about 700, while YouTube itself is free at about 240 an hour. Human-made captions Scry stored without line breaks are skipped, because their words run together at the breaks. Each Scry query reads every video ID Scry has, so it can take a few minutes and is allowed 10. If one fails, its videos come from YouTube and the next batch still asks Scry, until two fail in a row. Scry's source is marked `youtube:transcript:auto:scry` or `:human:scry`.
- **One video refused:** YouTube's bot check can refuse one video while others go through. That counts as a block only if the next video is refused too. Otherwise the video is `failed` with reason `bot_check`, and `--retry-failed` tries it again. A 429 or a captcha page is always a block.
- **After a block:** the block is remembered in `youtube_block.json` in the archive folder. A new run waits out what's left of the pause, and for 24 hours the lane goes at half speed. `--youtube-block-wait 0` skips the wait, which helps after changing network. Failing to reach YouTube at all isn't counted as a block.
- **Finishing early:** once only YouTube is left, the log says so. Ctrl-C is safe, and the next run resumes.

### Local files

**Your files are only ever read.** Nothing moves, changes, deletes or copies them.

| Type | Extensions | How |
|---|---|---|
| PDF | `.pdf` | pypdfium2, then OCR if there's no text |
| DjVu | `.djvu`, `.djv` | The text layer, read with DjVuLibre's `djvutxt`, then OCR if there's none |
| Ebook | `.epub`, `.mobi`, `.azw`, `.azw3` | EPUB in spine order. Kindle files are unpacked with KindleUnpack first |
| Word | `.docx` | mammoth, then markdownify |
| Web page | `.html`, `.htm`, `.xhtml` | In a folder on the web page, `.html` and `.htm` files are imported as [saved pages](#saved-pages). A `file://` link to one, and any `.xhtml` file, is read as a local file with the same reader |
| Text | `.txt`, `.md`, `.markdown`, `.rst`, `.tex`, `.org` | As is |

- **Paths:** the list holds `file:///path/to/My%20Paper.pdf`, one per line. `file:///C:/…` is also looked up under `/mnt/c/…` for WSL.
- **Other formats:** these are `skipped` with `local_other`, and are picked up automatically if a later version supports them. Newer `.kfx` files and old binary `.doc` files aren't supported.
- **DRM:** books under DRM are `failed` with `drm_protected` and aren't worked around.
- **DjVu:** reading DjVu files needs DjVuLibre's command-line tools (`apt install djvulibre-bin`). Without them a DjVu file is `failed` with `djvulibre_missing`, and the run's log says what to install. Once they're installed, `--retry-failed` reads it. A DjVu file's text layer is usually the OCR of the scan it was made from, so it gets the same repairs as a PDF's text. A DjVu book kept as one file per page with an index file, which is rare, comes out twice: whole from the index file, and a page at a time from the others.
- **Missing files:** a file that isn't there is `failed` with `file_missing`. Run on the machine that has it, or use `--retry-failed` once it's back.
- **Text that isn't UTF-8:** UTF-8 and files with a byte-order mark are read as such. Anything else is tried in each common legacy encoding, such as Windows cp1252 English with curly quotes, cp1254 Turkish or cp1251 Russian. A reading is kept only if it's beyond doubt: it has no control characters, symbols where letters belong or words mixing scripts, and no other reading is as plausible. When readings tie, the archive's vocabulary decides, by a clear margin only. Otherwise undecodable bytes become `�`, which the garbage check flags, rather than wrong letters that look right. Web pages served as plain text are read the same way, after any charset the server declares.

Ebooks and Word files follow [MarkItDown](https://github.com/microsoft/markitdown)'s recipe, which keeps all the text rather than guessing at a main content block.

### Generic lane

**First pass**

- Hosts are served round-robin, with at most `--per-host` (4) requests in flight per host and `--concurrency` (64) overall, and cooldowns on 429 and 5xx responses. `Retry-After` is honored.
- `--host-limit PATTERN=N[:SECONDS]` groups matching hosts into one slower queue, each wait varied by up to a quarter either way. The default is `scholar.google.*=1:20`, since Scholar shows CAPTCHAs after bursts (see [Google Scholar](#google-scholar)). Hosts in a group share the pace, but each is judged on its own: after 3 connection failures in a row, DNS included, only that host's remaining links are skipped as `host_down`.
- The `Content-Type` is checked before the body is read, so non-text responses are dropped unread.
- Timeouts, dropped connections, downloads that broke off, 429s and server errors (500, 502, 503, 504 and Cloudflare's 520 to 524) are tried up to 3 times (**Tries per page**), waiting longer each time, up to 60 seconds or what `Retry-After` asks.
- When one site refuses 5 links in a row (403, or 429 after its retries), its remaining links are marked `http_403` or `http_429` without being requested. When it fails to connect 3 times in a row (DNS, connection, TLS or timeout), its remaining links are marked `host_down` the same way. Links marked like this have no HTTP status in the manifest, and the fallback passes still try them. **Skip a site after this many refusals in a row** and **Skip a site after this many dropped connections in a row** under **When a page fails** change the numbers.

**Fallback passes**

These run after the first pass in every run that includes web pages, in the order below, each over what the ones before it couldn't fix. A thin document goes to every pass, whatever its reason. A failed one goes only to the passes that take its reason. Each pass keeps a result only if it has more text than what's saved.

| Pass | Option on the page | Failed reasons it takes | What it does |
|---|---|---|---|
| `impersonate` | **Try again looking like Chrome**, on by default | `http_403`, `http_429`, `http_503`, `connect`, `ssl`, `payload`, `no_text` | Downloads again with curl_cffi and Chrome's TLS fingerprint, with your cookies |
| `render` | **Use a headless browser for blocked pages** (`--render`), off by default. Needs Playwright and its Chromium | The same as `impersonate`, but never links ending in `.pdf` or Google and Scholar links, since Google answered the browser with its block page four times in five | Opens the page in headless Chromium with your cookies, user agent and Chrome profile copy, and waits up to **Wait for a bot check to clear** (`--challenge-wait`, 15) seconds for a bot-check page to clear. It needs a few hundred MB of RAM per page, so it opens 2 at a time |
| `doi` | **Look papers up by DOI**, on by default | Any, for links whose address holds a DOI, a nature.com article ID or a JSTOR ID, which covers dead sci-hub mirrors and publisher `/doi/` pages | Saves the paper's title, authors and abstract from Scry's OpenAlex copy, then the OpenAlex API. That's `ok` with an abstract, and `thin` with reason `no_abstract` without one. It never gets the full text |
| `wayback` | **Use the Wayback Machine**, on by default | Any except `non_text:…`, `too_large`, `bad_url` and `bug:…` | Saves the newest capture that answered 200, 4 requests at a time, a second apart |
| `crawl` | **Use Scry's copy of the page** (`--scry-crawl`), off by default. Needs a Scry key | The same as `wayback` | Saves the text Scry's web crawler last read from the page, under any of its http/https and www spellings, 50 pages per query. It sends those pages' addresses to Scry and stops once the run has spent `--scry-crawl-max-dollars` (1) on it, and the pages it didn't reach wait for the next run. In one test, three pages found cost $0.006 |

- A pass is recorded in the manifest's `tried` column once it has dealt with a link, and doesn't take it again. So a pass you turn on later, such as the headless browser, works through the old failures it takes in the next run, with nothing else to do.
- When all have run, a failed link stays failed until **Try failed URLs again** (`--retry-failed`), **Retry this site next run** (`--retry-host`) or **Fetch again**. Each clears `tried`, so every pass runs again. The first two put failed links back only, not thin ones.
- The rules above for skipping a site after 5 refusals or 3 connection failures apply inside each pass too. There the site's remaining links count as tried by that pass without being requested.
- A pass that can't start, such as the headless browser when Chromium isn't installed, is skipped with one error in the log, and its links keep their turn for a later run.
- OCR comes after these, for PDFs only. See [OCR](#ocr).

### Google Scholar

Scholar rations automated requests. Past some number it answers with an "unusual traffic" page, often as an ordinary page, even with your browser's cookies.

- **Pace:** one request at a time, about 20 seconds apart (15 to 25). Settings saved with the earlier default of 8 seconds move to 20 by themselves; a value you set yourself is kept.
- **Block pages:** Google's block page ("unusual traffic", or "Verifying your request… Please wait while we verify your session"), a 403 or 429 from Google, or a redirect to its `/sorry/` page is never saved. The first one ends Scholar's turn for the run: that link and every Scholar link not reached yet stay waiting for the next run, and the fallback passes leave Scholar alone too, so the block can lift. A Wayback copy that is only the block page counts as a failure.
- **No headless browser:** the browser pass and the browser fix method skip Scholar. Your cookies with the plain download work better.
- Scholar links still waiting usually mean a block page stopped Scholar's turn before they were reached. If Google blocks the first Scholar request of every run, each run asks for one link and no Scholar link ever arrives. Opening that link in your own browser shows whether Google is asking this connection to prove it's human.
- Since 2026-11-10 a 403 from Google counts as a block and leaves the link waiting, so Scholar links failed with `http_403` were recorded by earlier versions, mostly marked without being requested after 5 refusals in a row. They stay failed until **Retry this site next run** for `scholar.google.com`, or **Try failed URLs again**.
- **Profiles:** a profile's list of works is asked for 100 at a time (`cstart=0&pagesize=100`), not the 20 Scholar shows unless you click Show more. Profiles saved before this have 20; fetch one again to get the longer list.

### Browser session

This helps with Scholar, Medium and other logged-in or bot-walled sites:

```sh
.venv/bin/osiris run --cookies ~/cookies.txt --user-agent "<your browser's UA>" --render
```

- `--cookies FILE` takes a Netscape `cookies.txt` or a JSON cookie export. Each cookie is sent only to its own site, including across redirects.
- `--user-agent` should match the browser the cookies came from, because Cloudflare ties `cf_clearance` to it.
- `--chrome-profile DIR` makes `--render` use a copy of your Chrome profile directory.

The cookie file holds live sessions, so keep it private.

## Why documents are waiting, failed or thin

This section is for working out why documents aren't saved and what can still be done about them. The page's [LLM summary](#web-interface) sends the whole README to DeepSeek with the counts, so this is also what it goes by.

The page and the LLM summary count documents. Several links can lead to one document, such as links to different files in one GitHub repo, and they share one record. `osiris stats` and the end of a run's log count links.

### A document's record

Each link has a row in `data/manifest.sqlite`.

| Column | Meaning |
|---|---|
| `status` | `pending` (waiting), `ok` (saved), `thin`, `failed`, `skipped`, or `deleted` (removed with **Delete…** and never fetched again) |
| `reason` | Why it's failed, thin or skipped. For a failed web page it's what the first download got, and fallback passes that fail too leave it as it was. So `http_403` with `impersonate,wayback` in `tried` means the site refused the first download, and neither the Chrome-like download nor the Wayback Machine got the page either. Only a pass that saves text, or **Fetch again**, writes a new reason |
| `http_status` | The site's answer to the download that set the reason. It's empty for failures with no HTTP answer, such as `timeout` or `dns`, and for links marked without being requested, as with `host_down` and with the links marked after a site refused 5 in a row. For failures, only web pages and GitHub record it |
| `final_url` | Where the download ended up after redirects. Another site there, such as a login or consent page, usually means a login wall or a paywall |
| `content_type` | What the site said it sent, such as `text/html` or `application/pdf` |
| `tried` | What has been done to it already, as a list: the fallback passes `impersonate`, `render`, `doi`, `wayback` and `crawl` (see [Generic lane](#generic-lane)), `ocr` (OCR has read it), `fulltext` (arXiv's full text was looked for), `html_checked` and `html_busy` (arXiv's HTML was read, or a run was too busy to read it), `comments` (a Stack Exchange thread was fetched with its comments) and `refresh` (a PDF refresh that hasn't finished) |
| `attempts` | How many times an outcome was recorded for it, over all runs. A fallback pass that fails records nothing, so this isn't the number of downloads |
| `updated_at` | When an outcome was last recorded. A fallback pass that fails doesn't change it |
| `chars` | Characters of text saved |

### Waiting

A waiting document hasn't been fetched or read yet. These are the usual causes.

- The run hasn't reached it yet, or was stopped. Starting again carries on.
- Its source isn't ticked under **What to archive**, or isn't in `--only`. Local files, saved pages, tweet files and AI chats are only read while **Local files** is ticked.
- GitHub links wait while there's no `GITHUB_TOKEN`, or the token is refused.
- Tweets wait while **Look up tweets in Scry** and **Allow twitterapi.io** are both off, or neither key is set, and once a run has spent the dollar limit next to **Look up tweets in Scry**.
- YouTube videos wait after a block, through pauses of 30 to 240 minutes, and the lane stops for the run after 5 blocks in a row. At about 240 videos an hour, a long list takes days. See [YouTube](#youtube).
- Google Scholar links wait after a block page, until the next run. See [Google Scholar](#google-scholar).
- arXiv full text waits until every abstract is in, comes one paper every 15 seconds, and stops for the run if arXiv can't be reached several times in a row or the Scry dollar limit is reached. The page's **arXiv full text** row counts papers as waiting until their full text has been looked for, though they're saved with their abstract.
- Stack Exchange questions wait once the day's API quota runs out, 300 calls without `STACKEXCHANGE_KEY`, until the next day.
- Web pages queued for **Fetch again** in the headless browser wait if the browser can't start.

### Thin

A thin document is saved and exported, but with less than 200 characters of text (**--thin-under**), or with only part of what was wanted. Thin web pages get every fallback pass, and thin PDFs get OCR. **Try failed URLs again** doesn't put thin documents back, but **Fetch again** does.

| Reason | Meaning |
|---|---|
| `short` | Under 200 characters. For a web page that's usually a login wall, a consent or cookie page, a paywall, a bot check, or a JavaScript app that shows nothing without a browser. For a PDF it's usually a scan with no text layer, which OCR reads |
| `no_abstract` | The DOI pass found the paper but no abstract, so it has the title and authors only |
| `no_readme` | A GitHub repo with no README |
| `no_transcript` | A YouTube video with no captions. It has the title and channel |
| `no_videos` | A YouTube playlist or channel that listed no videos |

A document the DOI pass saved is `ok` but holds only the abstract, not the paper.

### Failed

"Passes left" names the fallback passes that take the reason, which run if they're on and haven't run on the link yet. All five are `impersonate`, `render` (not for PDFs or Google), `doi` (only for links with a DOI), `wayback` and `crawl`. Once they have run, a failed document isn't tried again by itself, so "trying later" means **Try failed URLs again** or **Retry this site next run** in a later run, or **Fetch again** for one document.

| Reason | Meaning | Passes left | What else can help |
|---|---|---|---|
| `http_403` | The site refused the download, usually a bot wall or a login | All five | A cookies file with the matching **Browser user agent**, the headless browser, or saving the page with SingleFile into a Local files folder |
| `http_401`, `http_402` | The page needs a login or a subscription | `doi`, `wayback`, `crawl` | Cookies, or SingleFile |
| `http_404`, `http_410` | The page isn't there any more | `doi`, `wayback`, `crawl` | Nothing, unless a copy exists elsewhere. **Can't fix** takes it off the list |
| `http_429` | Too many requests. Tried 3 times in the first pass | All five | Trying later with **Retry this site next run**, or adding the site to the slower sites under **Speed** |
| `http_503` | Server overloaded, or a bot check. Tried 3 times in the first pass | All five | Trying later, the headless browser, or cookies |
| `http_500` to `http_599` other than 503 | Server error. 500, 502, 504 and 520 to 524 are tried 3 times in the first pass | `doi`, `wayback`, `crawl` | Trying later |
| `http_999`, `http_418`, `http_420` and other codes | The site turns away automated requests. LinkedIn answers 999 | `doi`, `wayback`, `crawl` | SingleFile |
| `redirects` | Too many redirects, usually a login loop | `doi`, `wayback`, `crawl` | Cookies, or SingleFile |
| `timeout` | No answer in time (**--page-timeout**, 30 seconds, or 15 seconds with nothing sent). Tried 3 times in the first pass | `doi`, `wayback`, `crawl` | Trying later, or a longer time limit under **Time limits** |
| `connect`, `payload` | The connection failed, or the download broke off. Tried 3 times in the first pass | All five | Trying later |
| `ssl` | The secure connection failed, often an expired certificate | All five | Usually nothing |
| `dns` | The site's name doesn't resolve, so the domain is probably gone | `doi`, `wayback`, `crawl` | Nothing |
| `host_down` | The site failed to connect 3 times in a row, so its other links were marked without being requested | `doi`, `wayback`, `crawl` | **Retry this site next run** once the site is back, which gives each link a real try |
| `no_text` | The page loaded but had no readable text: a JavaScript app, or a PDF with no text layer | All five, and OCR for PDFs | The headless browser for pages. A PDF with `ocr` in `tried` was read by OCR with nothing new found |
| `challenge` | A bot-check page didn't clear in the headless browser, chosen for **Fetch again** | `doi`, `wayback`, `crawl` | Cookies with the user agent they came from, since Cloudflare ties its `cf_clearance` cookie to it, a longer **Wait for a bot check to clear**, or SingleFile |
| `blocked` | The Wayback Machine's copy, chosen for **Fetch again**, was only Google's block page | `doi`, `wayback`, `crawl` | Nothing |
| `non_text:<type>` | An image, video, archive or other file that isn't text, so it wasn't downloaded | `doi` | Nothing |
| `too_large` | Too big to read in memory, or turned down for its size by an older version | `doi` | **Try failed URLs again**, with more **--worker-mem-mb** if needed |
| `pdf_error:<error>`, `extract_error:<error>` | The PDF or page couldn't be parsed | `doi`, `wayback`, `crawl` | **OCR it** for a PDF |
| `extract_crashed`, `extract_timeout` | Reading it crashed or took longer than **--read-timeout**, usually a huge or broken file | `doi`, `wayback`, `crawl` | A longer time limit, or more worker memory |
| `bad_url` | The address isn't valid | None | Nothing |
| `bug:<error>`, `error:<error>` | Something went wrong in osiris itself. The log has the details | `doi`, and `wayback` and `crawl` for `error:` | Trying again after an update |
| `not_in_scry` | Scry doesn't have the tweet, and twitterapi.io wasn't used | None | **Allow twitterapi.io** with `TWITTERAPI_IO_KEY`, billed per tweet, then **Try failed URLs again** or **Fetch again** from twitterapi.io. Or import your own tweet files |
| `not_found` | The source says it doesn't exist: an unknown arXiv ID, a deleted or private GitHub repo, a deleted Hacker News item, LessWrong post or Stack Exchange question, or a tweet twitterapi.io doesn't have because it was deleted or is protected | None | Nothing |
| `api_error`, `bad_response:<error>`, `network` | Stack Exchange's, Hacker News', LessWrong's or GitHub's API failed or couldn't be reached | None | Trying later |
| `bot_check` | YouTube refused this video while others went through | None | Trying later |
| `unavailable` | The YouTube video or list is private, removed or otherwise unavailable | None | Nothing |
| `youtube_request_failed`, `list_error` | YouTube's answer was an error | None | Trying later |
| A name in CamelCase, such as `AgeRestricted` or `VideoUnplayable` | YouTube wouldn't give this video's captions | None | Trying later sometimes works. **Use Scry's transcripts first** may have it |
| `file_missing` | The local file isn't at its path on this machine, usually because the archive was moved or the drive isn't mounted | None | Running where the file is, then **Try failed URLs again** |
| `file_unreadable` | The local file couldn't be opened | None | Checking its permissions |
| `drm_protected` | The ebook is under DRM | None | Nothing |
| `djvulibre_missing`, `djvu_error` | DjVuLibre isn't installed, or the DjVu file couldn't be read | None | `apt install djvulibre-bin`, then **Try failed URLs again** |

### Skipped

Skipped links are left alone on purpose and are never fetched. None needs doing anything about.

| Reason | Links |
|---|---|
| `x_non_status` | X pages that aren't a single post: profiles, searches, lists, home. An account's every tweet comes from a list of accounts instead (see [Every tweet of an account](#every-tweet-of-an-account)) |
| `private_app` | Links to your own chats on ChatGPT, Claude, Gemini, DeepSeek or Grok, which need your login. Chat exports bring them in (see [AI chats](#ai-chats)) |
| `youtube_other` | YouTube pages that aren't a video, playlist or channel, such as search, feeds, Watch Later and Liked |
| `arxiv_other` | arXiv addresses that aren't a paper osiris can name, such as author pages and listings |
| `github_account`, `github_other` | GitHub user and organization pages, and GitHub's own pages |
| `media:<extension>` | Images, video, audio, archives and installers, judged by the address's extension |
| `local`, `local_other` | Addresses on your own network, and local files in a format osiris can't read |
| `bad_url` | Addresses that aren't valid |

### Clues to a cause

- A site whose failed links mostly have no HTTP status refused or dropped the first few, and the rest were marked without being requested. **Retry this site next run** gives each a real try.
- Many failures from one site with `impersonate` and `wayback` already in `tried` point to a bot wall or a login. Cookies, the headless browser or SingleFile are what's left.
- A failed link whose `tried` lacks a pass that's on now and takes its reason will get that pass in the next run by itself.
- Failures that all happened on one day or in one run, across many sites, point to a network problem or a block at the time, and trying again is worth it.
- Many `file_missing` local files usually mean the archive was moved to another machine, or a drive isn't mounted.
- Waiting Scholar links mean Scholar is blocking. See [Google Scholar](#google-scholar).
- `host_down` on sci-hub mirrors, archive.is or archive.ph means those sites were unreachable. The DOI pass covers links with a DOI in them.

## PDFs

Every page of a PDF is read, however long it is. The time allowed grows with the page count.

Files of any size are accepted. What's left to stop a runaway file is each worker's memory cap (`--worker-mem-mb`), which fails that one file as `too_large`, and the time limits on downloading and reading it.

### OCR

`osiris run` reads PDFs with no usable text with [tesseract](https://github.com/tesseract-ocr/tesseract), both web and local, and DjVu files on this computer that have no text layer. It starts once the generic and local-file lanes finish, so a long scan holds up nothing, and it doesn't wait for YouTube.

- **Scope:** it covers only the lanes in that run. `--only localfile` leaves web PDFs alone, and `--only youtube` does no OCR.
- **Earlier PDFs:** it also works through PDFs left from earlier runs, so the first run after installing tesseract can take a while.
- **Files:** local files are read in place and never changed. Web PDFs are downloaded again to a temporary file, which is then deleted.
- **Speed:** the work is split by page, with `--workers` pages read at once, so every core stays busy even on a single long book.
- **Progress:** the log says when OCR starts each file and how many pages it has, and the progress line every 15 seconds adds how many pages of each file it has read so far, such as `OCR: scan.pdf, 40 of 300 pages read; 2 of 5 files done`.
- **Tries:** each PDF is tried once. Ctrl-C stops at once, and the PDF in progress is tried again next run.
- **Results:** text is kept only if there's more of it than before, with `source: pdf:ocr`, `file:pdf:ocr` or `file:djvu:ocr`.
- **Languages:** `--ocr-lang eng+tur` sets them, after installing their packs. The default is English. List only what you need, since each extra language is slower and can lower accuracy.
- **Missing tesseract:** if tesseract or a language pack is missing, the run logs a warning and carries on, and those PDFs wait without using up their try. A DjVu file waits the same way while DjVuLibre is missing.
- **Sideways and upside-down pages:** each page's orientation is found first (tesseract's `--psm 1`), so scanned slides and rotated pages come out as text. Upright pages read the same, about a second slower each. This needs tesseract's `osd` data, which most installs include (`apt install tesseract-ocr-osd` otherwise); without it the run says so and reads pages as they are. PDFs OCR'd before this, upside down, keep their garbled text until you press **OCR it** on them.
- **Pictures of letters:** some old TeX papers draw every letter as a numbered picture, so reading them gives codes instead of text. A PDF whose text is more than 5% control characters is treated as having no text, so OCR reads it.
- **Limitation:** a PDF with a few readable pages and many scanned ones counts as having text, so it isn't picked up.

`osiris ocr` runs OCR on its own. It takes `--lang`, `--workers`, `--limit`, and `--retry` to try PDFs again, for example in another language.

### Refreshing PDFs

```sh
.venv/bin/osiris run --refresh-pdfs web     # web PDFs only
.venv/bin/osiris run --refresh-pdfs local   # local PDFs only
.venv/bin/osiris run --refresh-pdfs both
```

**In the log:** a line when each refresh starts, progress about every 30 seconds (`refresh: 120 of 900 web PDFs read again, 14 got more text`) and a summary when it finishes. Web PDFs are read again only after every waiting web link is done, and the log says so at the start of the run.

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

The `.html` and `.htm` files in the web page's local folders are stored as saved pages, and `osiris import DIR [DIR|FILE ...]` does the same from the command line. They're usually pages saved with the [SingleFile](https://github.com/gildas-lormeau/SingleFile) browser extension. Subfolders are searched too, except hidden ones, and the originals are left alone. These documents appear as the `singlefile` lane, with `source: singlefile`, or `source: html_file` for a page SingleFile didn't save.

- **URL and date:** these come from the comment SingleFile writes at the top of the file, and the date becomes `fetched_at`. A file without that comment, such as a page a browser saved, is stored under a `file:` key of its own, since a site can give many pages the same canonical link. Its canonical link is kept as its `url`.
- **Dedup:** URLs go through the same dedup as `osiris run`, and imported URLs are never queued for fetching. A saved copy replaces a failed or pending entry, and replaces an archived document only when it has more text. Complete Stack Exchange, Reddit, Hacker News and LessWrong documents are always kept.
- **Skipped sites:** URLs `osiris run` skips on purpose, such as GitHub, chat apps and X profiles, are stored, since you saved them deliberately.
- **Re-running:** files are tracked by content hash, so only new or changed files are read. Files that failed are tried again.
- **Cleaning:** inlined CSS, scripts and images are cut before parsing, as are elements SingleFile marked hidden. Saved shadow DOM is kept.
- **X pages:** these are read from X's own tweet markup, so a saved conversation keeps every tweet with its author and date, and any X Article.
- **Unsupported:** SingleFile's self-extracting ZIP format is reported as `singlefile_zip`. Use the default HTML format.
- **Pages read as local files before:** saved pages used to have a folder list of their own on the web page, and its folders are now among the local folders. A web page that an earlier version archived as a local file keeps that document and isn't imported again.

## Tweet files and account lists

`osiris import-tweets DIR [DIR|FILE ...]` stores the tweets in four kinds of file, one document per tweet in the same format as the `twitter` lane, with `source: tweet:import`.

| File | What is read |
|---|---|
| Twitter's own export, as the `.zip` it arrives in | The account's tweets, replies and retweets, with long tweets in full. The zip is read where it is and never unpacked, and an export you unpacked yourself isn't read. Likes, messages and deleted tweets are left out, and the export holds no bookmarks |
| Bookmarks saved with the [twitter-web-exporter](https://github.com/prinsss/twitter-web-exporter) browser extension, as `.json` or `.html` | Each bookmarked tweet with its author, its whole text and the tweet it quotes. Both formats give the same documents, so having both does no harm |
| [Community Archive](https://www.community-archive.org) uploads, one `.json` file per account | The same as the export, which holds the same data. Tweets posted in Communities are included |
| SQLite databases written by `fetch_tweets.py`, with a `tweets` table | Every tweet. A retweet keeps the original tweet's full text, as twitterapi.io's do |

`fetch_tweets.py` is deprecated and has moved to `osiris/deprecated/`. It is a separate script that saves one account's tweets from twitterapi.io to a SQLite database, which a list of usernames now does inside osiris. It reads `TWITTERAPI_IO_KEY` from the environment or from `.env`.

Folders are searched, subfolders included, for `.zip`, `.json`, `.html`, `.htm`, `.db`, `.sqlite` and `.sqlite3` files, and a file that is none of these kinds is skipped. Files are read a tweet at a time, so their size doesn't matter. On the page, add the folders under **Tweets**. Each run with **Local files** ticked imports them before archiving, so the tweets they hold aren't looked up in Scry or twitterapi.io.

Tweets are matched by ID, so a username that changed doesn't matter. A tweet that's already saved, or that you deleted, is left alone. One that's waiting or failed, such as `not_in_scry`, gets the file's text. New tweets join the `twitter` lane, so **Fetch again** works on them. The export and the Community Archive hold only a retweet's `RT @user: …` text, often cut short, so that's what is kept, and they hold no quoted tweets. Text is kept as the file has it, `t.co` links included. Dates are written in UTC as `2026-09-16 22:18:18`, like Scry's. Running the import again only adds tweets that are new.

### Every tweet of an account

A file named `accounts.txt` directly in one of these folders is a list of accounts, one username per line. Other `.txt` files are skipped, and so is an `accounts.txt` in a subfolder. A line can be `name`, `@name` or a profile link, and `#` starts a comment. `osiris run --twitter-accounts PATH` saves every tweet of those accounts that it can get, where `PATH` is a list of any name or a folder whose `accounts.txt` is read. The page passes your **Tweets** folders by itself.

An `accounts.txt` with a line that isn't a username is left alone as a whole, and the log names the line.

Where the tweets come from depends on what's turned on.

- twitterapi.io is used when `TWITTERAPI_IO_KEY` is set and, on the page, **Allow twitterapi.io** is ticked. It gives the account's timeline with replies, newest first, for about $0.15 per 1,000 tweets. Each page of 20 is saved as it arrives, so stopping a run loses nothing that was paid for. A run stops once it has spent about `--twitterapi-max-dollars` (5). The next run carries on from the same place, and then takes what the account has posted since.
- Scry is used when it has a key and isn't turned off. It gives what its archive holds for the account, which includes tweets deleted since and is rarely all of them. Finding them cost about $0.03 per 1,000 tweets in a test. Scry also meters the data it sends: its allowance covers about 2,000 tweets a day, and its price list charges for a day's data past that at a rising rate, which the test didn't reach. A run stops once it has spent `--scry-tweets-max-dollars` (1) on tweets.
- With both on, twitterapi.io goes first and Scry adds what it has beyond that.
- With Scry alone, what it holds of an account is only what it happened to see. So osiris also asks Scry to collect each listed account, which costs nothing. Scry then walks back through the account's latest 3,200 or so tweets in its own time and looks again once a day, and later runs take what that adds. Scry only takes accounts it has seen at least one tweet of, and the log names any it couldn't take. osiris never ends the collecting. Scry's `twitter_watch` tool does that.
- With neither, the accounts are skipped with a warning.

Each run takes only what's new. twitterapi.io is read down to the newest tweet of the last finished walk, which costs one page per account when nothing is new, and Scry is asked only for tweets that aren't saved yet. How far each account has got is kept in the manifest, so a run that was stopped carries on without paying for the same pages again. These documents have `source: tweet:twitterapi` or `tweet:scry`.

## AI chats

`osiris import-chats DIR [DIR|FILE ...]` stores your conversations with Claude, ChatGPT and DeepSeek, one document per conversation. On the page, add the folders under **AI chats**, and each archive run with **Local files** ticked imports them first. These files are read, in the folders given and their subfolders:

| File | What is read |
|---|---|
| Claude's data export | `conversations.json`, inside the `conversations-000.zip` parts its manifest lists or the single zip older exports came as. The manifest's links expire after a day, so download every part at once. Projects, memories and artifacts aren't read yet |
| The [Claude Conversation Exporter](https://github.com/socketteer/Claude-Conversation-Exporter) extension | Its JSON files, one conversation each, or the zip its bulk export makes. Its Markdown and plain text files aren't read |
| ChatGPT's data export | `conversations.json`, or the `conversations-000.json` files newer exports split it into, loose or in the zip |
| DeepSeek's data export | The files in `chat_sessions/`, one per conversation |

Zips are read where they are, without unpacking them, and nothing in the folders is changed.

A conversation is known by its own ID, the one in its address, so its copies in several exports, or in an export and the extension's files, make one document. Exports that overlap do no harm, and every export you download can stay in the same folder.

- The newest copy is kept, whatever order the files are read in. That's the copy with the newest message, then the one updated last. A copy with fewer messages than the one saved never replaces it.
- Of two copies from the same moment, the extension's is kept, since it records which branch of the conversation you were on. The official export doesn't, so for its copies the branch is the one ending in the newest message.
- A conversation that has gone on since it was last imported is written again from the newer copy. One you've since deleted on the site keeps its document, and one deleted from the archive with **Delete…** isn't brought back.
- A file read before is skipped unless it has changed. When a new version of osiris writes chats differently, every file is read again and every document rewritten.

A document holds the branch you were on, with a heading for each turn saying who wrote it, when, in UTC, and for ChatGPT which model. It has your messages and the replies, text you attached, files and artifacts Claude wrote, and a line for each web search and page read, with the addresses Claude cited at the end of its reply. Thinking, tool output, images and the system text the sites add are left out. ChatGPT's hidden citation markers are removed, while the names and links they marked keep their words. The extension's copies also name the model in the front matter, and a later export that replaces such a copy keeps it.

Documents have `source: chat:claude`, `chat:claude:extension`, `chat:chatgpt` or `chat:deepseek`, and the conversation's address, so the Documents tab links to the live chat. **Fetch again** doesn't apply to them. Import a newer export instead. Links to conversations in your tab backups are never fetched, and share the conversation's document once it's imported.

If a site changes its format, the parts osiris doesn't know are left out and named in the log, and a conversation that can't be read is counted and skipped without stopping the rest.

### Sessions with coding agents

The same command, and the same **AI chats** folders, also store your sessions with the coding agents Claude Code, pi and omp, one document per session. Give it the folders where they keep their logs, or copies of them, or a folder holding those. Hidden folders are skipped as elsewhere, except `.claude`, `.claude-<name>`, `.pi` and `.omp`, which are searched for their `.jsonl` logs alone.

| Agent | Folder |
|---|---|
| Claude Code | `~/.claude`, and `~/.claude-<name>` for each other profile. The sessions are in `projects/`, and `history.jsonl` holds the prompts you typed |
| pi | `~/.pi/agent/sessions` |
| omp | `~/.omp/agent/sessions` |

A session is known by its own ID, so copies of its log make one document. A session that has gone on since it was last imported is written again under the same document, and a log that hasn't changed is skipped. If you rewound or branched, the document follows the branch you were last on.

A document is the conversation between you and the model, with a heading for each turn saying who wrote it, when, in UTC, and which model replied. It has your prompts and what you pasted, the prompts of guests in an omp-serve session, everything the model wrote to you, the plans it showed you for approval, and the questions it asked you, quoted, with your answers. A paste longer than 2,000 words becomes a line saying how long it was and how it began. Reasoning, tool calls and their output, subagents, the text the agent adds to your messages, shell commands you ran and compaction summaries are left out, and so is a one-line note the model writes just before a tool call. Headings inside messages are made third-level, so the second-level headings are only the ones saying who is speaking. The front matter names the folder the session ran in.

Claude Code deletes a session's log after 30 days unless you raise its `cleanupPeriodDays` setting, but your prompts stay in `history.jsonl`. A session whose log was never imported gets a document of your prompts alone, with `source: chat:claude-code:history`, which the log replaces if it turns up. A document is never deleted because its log has gone. A backup made with `rsync --delete` would lose the log, though, so leave that option off for these folders.

Resuming or forking a session can copy it into a new one. A session that holds another whole, from its first message to its last, takes over the shorter one's document, and the shorter one's address leads to it. One that only starts partway through another, as a continued session can, keeps its own.

Session documents have `source: chat:claude-code`, `chat:omp` or `chat:pi`, and an address such as `session://omp/<ID>`, which nothing fetches. pi and omp write the same format, so a log under a folder named `omp` or `.omp` is taken as omp's and one under `pi` or `.pi` as pi's. Keep those folder names in your copies. Subagents' logs, in Claude Code's `subagents/` folders and the folders omp keeps beside each session's log, are skipped.

## Output

Everything goes in `data/`.

| Path | Contents |
|---|---|
| `manifest.sqlite` | One row per URL: lane, status, reason, HTTP status, final URL, character count, dedup key, fallback passes tried and raw path. Also how far each listed account's tweets have been taken, which copy of each AI chat is saved, and which chat exports have been read |
| `md/<id[:2]>/<id>.md` | YAML front matter (url, title, author, date, site, source, and `raw` when an original was kept) and the markdown body |
| `corpus.jsonl` | Written by `osiris export` |
| `added.tsv` | The documents the last run added, one per line: whether each is new, had failed or was thin before, its source, title, address and file. Every `osiris run` and every run on the page writes it again |
| `review.sqlite` | Garbage scores and your review decisions. An archive's older `audit.sqlite` is renamed to this when it's first opened |
| `review-exclude.txt` | Paths of documents marked Bad or Can't fix |
| `lexicon.sqlite` | Words and hyphenated pairs seen at least twice in the archive, for rejoining words broken at line ends. Built by `osiris clean` |
| `clean-report.tsv`, `clean-backup.sqlite` | What the last `osiris clean` changed, and the originals `--undo` puts back |
| `search.sqlite`, `search.lock` | Only with `osiris ui --search-index` or `osiris search`: the full-text index, rebuilt from `md/` if deleted, and the lock that keeps two updates from running at once |
| `.osiris-busy` | An empty file that `run` (with or without `--fixes`), `import`, `import-tweets`, `ocr` and `clean` hold a shared lock on while they run, so that a program reading the archive, such as isis, can tell that osiris is writing to it and put off heavy work until it's done. The system drops the lock when the command ends, however it ends. `osiris.store.is_archiving(data)` tests it |
| `ui.json`, `ui_urls.txt`, `ui_inputs.txt` | The web interface's settings, latest URL list and the tab backups behind it |
| `raw/<id[:2]>/<id>.{html.zst,pdf}` | Only with `--keep-raw-pdfs`; the same id as the document in `md/`. `.html.zst` is arXiv's HTML. Budget about 2 MB per PDF |

| Status | Meaning |
|---|---|
| `pending` | Not done yet |
| `ok` | Stored |
| `thin` | Under 200 characters, usually a JavaScript app, paywall or bot wall, or a paper with no abstract. Still saved and exported |
| `failed` | No text. `reason` says why |
| `skipped` | Deliberately not fetched |
| `deleted` | Removed with **Delete…**, and not fetched again |

[Why documents are waiting, failed or thin](#why-documents-are-waiting-failed-or-thin) explains each reason and what can still be done.

`source` records where the text came from, for example `html`, `singlefile`, `pdf:wayback`, `html:render`, `doi:scry`, `reddit:scry+arctic_shift`, `tweet:twitterapi`, `tweet:import`, `chat:claude`, `github:api`, `youtube:transcript:auto` or `file:pdf`.

Extraction runs in memory-capped worker processes (`--worker-mem-mb`, default 1536), which are replaced every 500 documents and rebuilt after a crash. Only the document that crashed a worker is lost.

## Tests

```sh
.venv/bin/pip install -e .[test]
.venv/bin/python -m pytest
```

One test prints a page to PDF with Chrome, and is skipped if there's none.

## The screenshot

`screenshot.png` is the picture of the Archive tab that `README.md` shows. This takes it again from the page as it is now:

```sh
.venv/bin/python tools/screenshot.py
```

The picture shows the same archive every time, on any machine. Its folders, settings, numbers and log come from `tools/screenshot.json`, and nothing is read from your own archive or settings. To change what a box or a table shows, change that file. A setting the page has gained since shows its default. Everything the picture shows was copied from the picture it replaced. Two things in the file that the picture can't show are made up, which are the log lines above the ones in view and the counts beside the sites in `hosts`. The command warns about any name in the file that the server no longer knows, and a test does the same.

The page is drawn in a window 900 px wide, and the picture is 1676 pixels wide. It ends as far under the last line as the first card is under the tabs. Two runs on the same machine give the same file, byte for byte. Another machine's browser can draw a few pixels differently.

It needs Playwright's Chromium, which `pip install -e .[render]` and `playwright install chromium` set up, and the two fonts the file names, Ubuntu Sans and DejaVu Sans Mono. An Ubuntu 24.04 desktop has both. Elsewhere, `--fonts FOLDER` adds a folder of font files. The command stops without writing anything if the browser drew the page in other fonts.

GitHub serves the file unchanged but shows it 838 px wide. The picture is twice that width, so GitHub shows it pixel for pixel on a 2x screen and exactly halved on a 1x one, which keeps the text sharp. `--wide PIXELS` makes it another width with the same page in it. The picture's text is antialiased in grey, since the coloured fringes of ordinary desktop text turn into coloured speckle when shrunk. `--lcd` brings the fringes back.

`tools/pre-commit` is a git hook that takes the picture again on every fifth commit and adds it to that commit. Git doesn't copy hooks along with a clone, so turn it on once in each clone, from the repo's top folder:

```sh
ln -s ../../tools/pre-commit .git/hooks/pre-commit
```

The hook counts every commit in the branch's history, so the fifth ones are commits 110, 115 and so on. Such a commit takes about 15 seconds longer. The picture is of the page as the files are at that moment, staged or not. If the page hasn't changed since the picture was last taken on the same machine, it comes out as the same file and the commit doesn't include it. If the picture can't be taken, for example because Playwright or a font is missing, the hook says so and the commit goes ahead. A commit of named files, as in `git commit FILE`, can't take a picture along, so one that falls on a fifth commit leaves the picture for the next fifth one.
