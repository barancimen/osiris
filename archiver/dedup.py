"""Which saved URLs to keep, and when two URLs are the same page.

Shared by extract_urls.py (building the URL list) and the archiver manifest (skipping
URLs already archived when a new list is loaded).
"""

import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote

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
