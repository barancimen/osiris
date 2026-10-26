import json
import re
import glob
import os
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote

# Only explicitly saved links: bookmarks + Session Buddy.
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

# --- filtering ---------------------------------------------------------------

GOOGLE_HOST = re.compile(r'(^|\.)google(\.com?)?\.[a-z]{2,3}$')  # google.com, mail.google.com, google.co.uk, ...
YOUTUBE_HOST = re.compile(r'(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$')
LOCAL_HOST = re.compile(r'^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\])$')

def bare_host(u):
    return (urlsplit(u).hostname or '').lower()

def keep(u):
    """Web pages only (http/https), no Google (except Scholar)/Gmail/YouTube, no local servers."""
    if not re.match(r'^https?://', u, re.I):
        return False  # chrome://, chrome-extension://, file://, blob:, javascript:, data:, about:, ...
    host = bare_host(u)
    if host.startswith('scholar.google.'):
        return True  # the one Google service worth keeping
    if not host or LOCAL_HOST.match(host) or GOOGLE_HOST.search(host) or host.endswith('gmail.com')\
            or YOUTUBE_HOST.search(host):
        return False
    return True

# --- dedup key -----------------------------------------------------------------

TRACKING = re.compile(
    r'^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|_hsenc|_hsmi'
    r'|ref_src|ref_url|si|embeds_referring_euri|source_ve_path|ab_channel)$')
HOST_TRACKING = {
    'x.com': {'s', 't', 'ref'},
    'marketwatch.com': {'mod'},
}
# Hosts whose paths are case-insensitive (user names, tickers, repo owners).
CASE_INSENSITIVE_PATH = {'x.com', 'github.com', 'reddit.com', 'marketwatch.com'}
HOST_ALIASES = {'twitter.com': 'x.com'}

def norm_host(host):
    host = re.sub(r'^(www\d?|m|mobile|old|new|np)\.', '', host)
    host = host.replace('.m.wikipedia.org', '.wikipedia.org')
    return HOST_ALIASES.get(host, host)

def keep_fragment(frag, path):
    # Hash-routed apps (#/path, #!/path, #key=value) use the fragment as the page;
    # plain anchors (#Section), text fragments (#:~:text=), PDF viewer state
    # (#page=3) and Wikipedia's image lightbox (#/media/...) do not.
    frag = frag.split(':~:', 1)[0]
    if not frag or path.lower().endswith('.pdf') or frag.startswith('/media/'):
        return False
    return frag[0] in '/!' or '=' in frag

def dedup_key(u):
    p = urlsplit(u)
    host = norm_host((p.hostname or '').lower())
    path = unquote(p.path).rstrip('/')

    if host == 'arxiv.org':
        m = re.match(r'^/(?:abs|pdf|html)/(.+?)(?:v\d+)?(?:\.pdf)?$', path)
        if m:
            return 'arxiv:' + m.group(1)
    if host == 'x.com':
        path = re.sub(r'^(/[^/]+/status/\d+)/(photo|video|analytics)(/\d+)?$', r'\1', path)
    if host in CASE_INSENSITIVE_PATH:
        path = path.lower()

    drop = HOST_TRACKING.get(host, set())
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not TRACKING.match(k) and k not in drop)
    frag = unquote(p.fragment) if keep_fragment(p.fragment, path) else ''
    return urlunsplit(('https', host, path, urlencode(query), frag))

def main():
    d = os.path.dirname(os.path.abspath(__file__))
    files = []
    files += sorted(glob.glob(os.path.join(d, 'bookmarks_*.html')))
    files += sorted(glob.glob(os.path.join(d, 'session-buddy-*.json')))

    seen = set()
    ordered = []  # first-seen original spelling of each page
    for path in files:
        name = os.path.basename(path)
        if name.startswith('bookmarks_'):
            urls = extract_bookmarks(path)
        else:
            urls = extract_session_buddy(path)
        kept = [u for u in urls if keep(u)]
        added = 0
        for u in kept:
            k = dedup_key(u)
            if k not in seen:
                seen.add(k)
                ordered.append(u)
                added += 1
        print(f'{name}: {len(urls)} total, {len(urls) - len(kept)} filtered, '
              f'{added} new, running unique {len(seen)}', file=sys.stderr)

    out = os.path.join(d, 'all_urls.md')
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(ordered) + '\n')
    print(f'wrote {out}: {len(ordered)} unique urls', file=sys.stderr)

if __name__ == '__main__':
    main()
