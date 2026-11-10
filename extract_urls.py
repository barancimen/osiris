import argparse
import hashlib
import json
import pathlib
import re
import glob
import os
import sqlite3
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)  # works under python -I and from any cwd
import osiris.dedup  # noqa: E402
from osiris.dedup import keep, dedup_key, unwrap  # noqa: E402  (shared with the osiris manifest)

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

def rules_digest():
    """Changes with the rules for choosing URLs, so that files read under older rules get read again."""
    h = hashlib.sha256()
    for path in (__file__, osiris.dedup.__file__):
        with open(path, 'rb') as f:
            h.update(f.read())
    return h.digest()

def file_key(path, rules):
    h = hashlib.sha256(rules)
    with open(path, 'rb') as f:
        while chunk := f.read(2 ** 20):
            h.update(chunk)
    return h.hexdigest()

def already_read(manifest):
    """Keys of the backup files whose URLs are all in the manifest."""
    try:
        db = sqlite3.connect(pathlib.Path(manifest).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            return {key for (key,) in db.execute('SELECT key FROM input_files')}
        finally:
            db.close()
    except sqlite3.Error:  # no manifest yet, or one from before files were recorded: read everything
        return set()

def main():
    ap = argparse.ArgumentParser(description='Collect unique saved URLs from bookmark and Session Buddy backups.')
    ap.add_argument('--input-dir', default=INPUT_DIR, help=f'backup directory (default: {INPUT_DIR})')
    ap.add_argument('--output', default=OUTPUT, help=f'URL list to write (default: {OUTPUT})')
    ap.add_argument('--manifest', help='skip backup files whose URLs this manifest already has')
    ap.add_argument('--hashes-out', help='add the files read here, for `osiris run --mark-inputs`')
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
    rules = rules_digest() if args.manifest else b''
    known = already_read(args.manifest) if args.manifest else set()
    unchanged = 0
    read = []  # (key, file name)
    for path in files:
        name = os.path.basename(path)
        if args.manifest:
            key = file_key(path, rules)
            if key in known:
                unchanged += 1
                continue
            known.add(key)  # an identical copy further on needs no reading either
            read.append((key, name))
        if name.startswith('bookmarks_'):
            urls = extract_bookmarks(path)
        else:
            urls = extract_session_buddy(path)
        filtered = added = 0
        for u in urls:
            if u not in keys:
                target = unwrap(u)  # a Google redirect link: the page it leads to
                keys[u] = (dedup_key(target), target) if keep(target) else None
            k = keys[u]
            if k is None:
                filtered += 1
            elif k[0] not in seen:
                seen.add(k[0])
                ordered.append(k[1])
                added += 1
        print(f'{name}: {len(urls)} total, {filtered} filtered, '
              f'{added} new, running unique {len(seen)}', file=sys.stderr)

    out = args.output
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(ordered) + '\n')
    print(f'wrote {out}: {len(ordered)} unique urls', file=sys.stderr)
    if args.manifest:
        print(f'skipped {unchanged} of {len(files)} files, already read and unchanged', file=sys.stderr)
    if args.hashes_out:
        with open(args.hashes_out, 'a', encoding='utf-8') as f:
            f.writelines(f'{key}\t{name}\n' for key, name in read)

if __name__ == '__main__':
    main()
