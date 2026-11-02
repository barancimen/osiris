"""Which saved URLs to keep, and when two URLs are the same page.

Shared by extract_urls.py (building the URL list) and the archiver manifest (skipping
URLs already archived when a new list is loaded).
"""

import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote

# --- filtering ---------------------------------------------------------------

FILE_URL = re.compile(r'^file:/', re.I)
GOOGLE_HOST = re.compile(r'(^|\.)google(\.com?)?\.[a-z]{2,3}$')  # google.com, mail.google.com, google.co.uk, ...
YOUTUBE_HOST = re.compile(r'(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$')
LOCAL_HOST = re.compile(r'^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\])$')

def bare_host(u):
    return (urlsplit(u).hostname or '').lower()

def keep(u):
    """Web pages and local files, no Google (except Scholar)/Gmail, no local servers."""
    if FILE_URL.match(u):
        return True
    if not re.match(r'^https?://', u, re.I):
        return False  # chrome://, chrome-extension://, blob:, javascript:, data:, about:, ...
    host = bare_host(u)
    if host.startswith('scholar.google.'):
        return True  # the one Google service worth keeping
    if not host or LOCAL_HOST.match(host) or GOOGLE_HOST.search(host) or host.endswith('gmail.com'):
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

YOUTUBE_VIDEO_PATH = re.compile(r'^/(?:shorts|embed|live|v|e)/([\w-]{11})(?:/|$)')
YOUTUBE_CHANNEL_PATH = re.compile(r'^/(?:(@[^/]+)|channel/(UC[\w-]+)|(?:c|user)/([^/]+))(?:/|$)')
VIDEO_ID = re.compile(r'^[\w-]{11}$')
# the viewer's own lists and auto-generated mixes: nothing public to fetch
PRIVATE_PLAYLIST = re.compile(r'^(?:WL|LL|LM|RD.*)$')

def youtube_target(u):
    """('video', id), ('playlist', id) or ('channel', key) for a YouTube URL; None for other pages
    and other sites. A watch link inside a playlist is that video."""
    p = urlsplit(u)
    if not YOUTUBE_HOST.search((p.hostname or '').lower()):
        return None
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    if (p.hostname or '').lower() == 'youtu.be':
        vid = p.path.strip('/').split('/')[0]
        return ('video', vid) if VIDEO_ID.match(vid) else None
    if p.path.rstrip('/') == '/watch':
        return ('video', query['v']) if VIDEO_ID.match(query.get('v', '')) else None
    m = YOUTUBE_VIDEO_PATH.match(p.path)
    if m:
        return ('video', m.group(1))
    if p.path.rstrip('/') == '/playlist' and query.get('list'):
        return None if PRIVATE_PLAYLIST.match(query['list']) else ('playlist', query['list'])
    m = YOUTUBE_CHANNEL_PATH.match(unquote(p.path))
    if m:
        handle, channel_id, legacy = m.groups()
        return ('channel', channel_id or (handle or legacy).lower())
    return None

def dedup_key(u):
    p = urlsplit(u)
    if p.scheme.lower() == 'file':  # same file whatever the viewer's #page=3 says
        return 'file://' + unquote(p.path)
    target = youtube_target(u)
    if target:
        return 'youtube:' + ':'.join(target)
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
