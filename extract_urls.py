import argparse
import json
import re
import glob
import os
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)  # works under python -I and from any cwd
from osiris.dedup import keep, dedup_key  # noqa: E402  (shared with the osiris manifest)

# Only explicitly saved links: bookmarks + Session Buddy, web pages and local files (file://).
# htu_*.tsv (browser history) and twitter-Following-*.json are excluded.

def unescape_attr(s):
    # html.unescape() also decodes legacy entities without a trailing ';'
    # (e.g. "&reg" in "&registerDownload" -> "®"), so only undo what the
    # bookmarks exporter actually escapes.
    return (s.replace('&quot;', '"').replace('&lt;', '<')
             .replace('&gt;', '>').replace('&#39;', "'").replace('&amp;', '&'))

def extract_bookmarks(path):
    urls = []
    with open(path, encoding='utf-8', errors='replace') as f:
        content = f.read()
    for m in re.finditer(r'HREF="([^"]*)"', content):
        u = unescape_attr(m.group(1)).strip()
        if u:
            urls.append(u)
    return urls

def extract_session_buddy(path):
    urls = []
    with open(path, encoding='utf-8', errors='replace') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return urls
    for col in (data.get('collections') or []):
        if not isinstance(col, dict):
            continue
        for folder in (col.get('folders') or []):
            if not isinstance(folder, dict):
                continue
            for link in (folder.get('links') or []):
                if isinstance(link, dict) and link.get('url'):
                    urls.append(link['url'].strip())
    for h in (data.get('history') or []):
        if not isinstance(h, dict):
            continue
        for win in (h.get('windows') or []):
            if not isinstance(win, dict):
                continue
            for tab in (win.get('tabs') or []):
                if isinstance(tab, dict) and tab.get('url'):
                    urls.append(tab['url'].strip())
    return urls

INPUT_DIR = os.path.normpath(os.path.join(REPO_DIR, '..', 'tab-backups'))
OUTPUT = os.path.join(REPO_DIR, 'all_urls.md')  # default input of `osiris run`

def refuse_to_overwrite(path, input_dir):
    """This rewrites its output, so that must never be one of your files: not in the backup folder,
    and if something's already there, only an earlier list of URLs."""
    if os.path.realpath(os.path.dirname(path) or '.') == os.path.realpath(input_dir):
        sys.exit(f'{path} is in the backup folder {input_dir}: choose another output path')
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            lines = [line for line in f if line.strip()]
    except (OSError, ValueError):
        lines = ['not a list']
    # Lists are one URL per line, with anything else ignored by `osiris run`.
    if not all(re.match(r'[a-z][a-z0-9+.-]*:', line.strip(), re.I) for line in lines):
        sys.exit(f"{path} exists and isn't a list of URLs, so it won't be overwritten. Choose another path.")

def main():
    ap = argparse.ArgumentParser(description='Collect unique saved URLs from bookmark and Session Buddy backups.')
    ap.add_argument('--input-dir', default=INPUT_DIR, help=f'backup directory (default: {INPUT_DIR})')
    ap.add_argument('--output', default=OUTPUT, help=f'URL list to write (default: {OUTPUT})')
    args = ap.parse_args()

    d = args.input_dir
    refuse_to_overwrite(args.output, d)
    files = []
    files += sorted(glob.glob(os.path.join(d, 'bookmarks_*.html')))
    files += sorted(glob.glob(os.path.join(d, 'session-buddy-*.json')))
    if not files:
        sys.exit(f'no bookmarks_*.html or session-buddy-*.json in {d}')

    seen = set()
    ordered = []  # first-seen original spelling of each page
    keys = {}  # dedup key of each exact URL, or None if it's filtered out: backups repeat most of their URLs
    for path in files:
        name = os.path.basename(path)
        if name.startswith('bookmarks_'):
            urls = extract_bookmarks(path)
        else:
            urls = extract_session_buddy(path)
        filtered = added = 0
        for u in urls:
            if u not in keys:
                keys[u] = dedup_key(u) if keep(u) else None
            k = keys[u]
            if k is None:
                filtered += 1
            elif k not in seen:
                seen.add(k)
                ordered.append(u)
                added += 1
        print(f'{name}: {len(urls)} total, {filtered} filtered, '
              f'{added} new, running unique {len(seen)}', file=sys.stderr)

    out = args.output
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(ordered) + '\n')
    print(f'wrote {out}: {len(ordered)} unique urls', file=sys.stderr)

if __name__ == '__main__':
    main()
