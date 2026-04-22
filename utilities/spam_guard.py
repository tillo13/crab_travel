"""Shared contact form spam guard, used across all kumori-hosted sites.

Layers (checked in order; first hit wins):
1. Honeypot field — hidden input bots fill, humans don't see
2. Origin/Referer check — blocks direct API POSTs with no source page
3. User-Agent blocklist — curl / python-requests / wget / generic libs
4. Timing check — client sends time_open, reject under 3s
5. IP rate limiting — in-memory, per-site, resets on deploy (2/hr)
6. Email domain blocklist — disposable / known-spam domains
7. Normalized-email rate limiting — collapses Gmail dot/plus variants
   (`a.b.c@gmail.com` == `abc@gmail.com` == `abc+tag@gmail.com`)
8. StopForumSpam reputation — free shared-intel API lookup of IP + email
9. Content pattern blocklist — SEO pitches, backlink spam, lead-gen
"""

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict

logger = logging.getLogger('spam_guard')

# ── Content patterns that indicate spam ──────────────────────────────────
# Each is (compiled_regex, description), case-insensitive match against
# concatenated form fields (name + company + subject + message).
SPAM_PATTERNS = [
    (re.compile(r'(SEO|search engine).{0,30}(rank|result|optimiz|appear|visib|first page)', re.I),
     'SEO pitch'),
    (re.compile(r'(digital marketing|web design|web development).{0,20}(agency|firm|company|service)', re.I),
     'marketing agency pitch'),
    (re.compile(r'review(ed)?\s+(of\s+)?your\s+(web)?site', re.I),
     'site review pitch'),
    (re.compile(r'(boost|improve|increase).{0,20}(traffic|ranking|visib|lead|conversion)', re.I),
     'traffic/ranking pitch'),
    (re.compile(r'(backlink|link.?building|guest.?post|article.?placement)', re.I),
     'backlink/guest-post spam'),
    (re.compile(r'(white.?label|outsourc).{0,20}(develop|design|market|SEO)', re.I),
     'outsourcing pitch'),
    (re.compile(r'(struggl|fail|poor|lacking).{0,30}(search|google|rank|traffic|online presence)', re.I),
     'negative SEO pitch'),
    (re.compile(r'(free|complimentary).{0,20}(audit|analysis|review|consultation|quote)', re.I),
     'free audit offer'),
    (re.compile(r'(get|drive|attract)\s+more\s+(client|customer|visitor|lead|traffic)', re.I),
     'lead generation pitch'),
    (re.compile(r'(first page|page one|top\s+(of\s+)?google)', re.I),
     'Google ranking promise'),
]

# ── Disposable / known-spam email domains ────────────────────────────────
BLOCKED_DOMAINS = {
    # Disposable email services
    'mailinator.com', 'guerrillamail.com', 'guerrillamail.de', 'tempmail.com',
    'throwaway.email', 'temp-mail.org', 'fakeinbox.com', 'sharklasers.com',
    'guerrillamailblock.com', 'grr.la', 'dispostable.com', 'yopmail.com',
    'trashmail.com', 'trashmail.me', 'mailnesia.com', 'maildrop.cc',
    'discard.email', 'tempail.com', 'emailondeck.com', 'getnada.com',
    'mohmal.com', '10minutemail.com', 'minutemail.com', 'tempr.email',
    'binkmail.com', 'safetymail.info', 'filzmail.com',
}

# ── Gmail dot/plus normalization ─────────────────────────────────────────
GMAIL_DOMAINS = {'gmail.com', 'googlemail.com'}

# ── Rate limiting ────────────────────────────────────────────────────────
# Two buckets: one keyed by IP, one by normalized email.
_ip_submissions: dict[str, list[float]] = defaultdict(list)
_email_submissions: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_IP = 2          # tightened 2026-04-21 after multi-site spam wave
RATE_LIMIT_EMAIL = 2
RATE_WINDOW = 3600          # seconds (1 hour)

# ── User-Agent blocklist ─────────────────────────────────────────────────
# Real browsers never send these. Dumb bots do.
_UA_BLOCKLIST = re.compile(
    r'^\s*$|^(python-requests|python-urllib|curl|wget|go-http-client|java|'
    r'httpx|aiohttp|axios|libwww|HTTPie|Scrapy|node-fetch|okhttp|'
    r'apache-httpclient|postmanruntime)\b',
    re.I,
)

# ── StopForumSpam reputation cache (in-memory, 1-hour TTL) ───────────────
_sfs_cache: dict[str, tuple[float, bool]] = {}  # key -> (timestamp, is_spam)
_SFS_CACHE_TTL = 3600
_SFS_URL = 'https://api.stopforumspam.org/api'
_SFS_FREQUENCY_THRESHOLD = 1  # seen reported ≥1 times across network = spam
_SFS_TIMEOUT = 2.0             # seconds — fail open if API slow


def normalize_email(email: str) -> str:
    """Lowercase, strip, and for Gmail: drop dots in local part + strip +tags.

    Gmail routes `a.b.c@gmail.com`, `abc@gmail.com`, and `abc+foo@gmail.com`
    all to the same inbox, so collapse them into one rate-limit key.
    `googlemail.com` is an alias for `gmail.com` — treat as one.
    """
    email = (email or '').strip().lower()
    if '@' not in email:
        return email
    local, domain = email.rsplit('@', 1)
    if domain in GMAIL_DOMAINS:
        local = local.split('+', 1)[0].replace('.', '')
        domain = 'gmail.com'
    return f'{local}@{domain}'


def _clean_old(bucket: dict[str, list[float]], key: str):
    """Remove timestamps older than the rate window."""
    cutoff = time.time() - RATE_WINDOW
    bucket[key] = [t for t in bucket[key] if t > cutoff]


def _check_stopforumspam(ip: str, email: str) -> str | None:
    """Query StopForumSpam for IP + email reputation. Fails open on API error.
    Results cached in-memory for _SFS_CACHE_TTL seconds to avoid hammering the API."""
    cache_key = f'{ip}|{email}'
    now = time.time()
    cached = _sfs_cache.get(cache_key)
    if cached and (now - cached[0]) < _SFS_CACHE_TTL:
        if cached[1]:
            return 'stopforumspam:cached'
        return None

    params = {'json': '1'}
    if ip:
        params['ip'] = ip
    if email and '@' in email:
        params['email'] = email
    if 'ip' not in params and 'email' not in params:
        return None

    try:
        url = f'{_SFS_URL}?{urllib.parse.urlencode(params)}'
        with urllib.request.urlopen(url, timeout=_SFS_TIMEOUT) as resp:
            body = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception as e:
        logger.debug(f'StopForumSpam API error (failing open): {e}')
        return None

    if not body.get('success'):
        return None

    is_spam = False
    for field in ('ip', 'email'):
        block = body.get(field)
        if isinstance(block, dict) and block.get('frequency', 0) >= _SFS_FREQUENCY_THRESHOLD:
            is_spam = True
            _sfs_cache[cache_key] = (now, True)
            return f'stopforumspam:{field}_freq_{block.get("frequency")}'
    _sfs_cache[cache_key] = (now, False)
    return None


def check_spam(data: dict, ip: str, fields: list[str] | None = None,
               origin: str | None = None, user_agent: str | None = None,
               expected_hosts: list[str] | None = None) -> str | None:
    """Run all spam checks against a contact form submission.

    Args:
        data: form data dict (expects keys like name, email, message, etc.)
        ip: requester IP address
        fields: which keys to concatenate for content scanning
                (default: name, company, subject, message)
        origin: request.headers.get('Origin') or Referer — for header validation
        user_agent: request.headers.get('User-Agent') — for UA filter
        expected_hosts: allowed Origin/Referer hosts for this site
                        (e.g., ['crab.travel', 'www.crab.travel']).
                        If None, the Origin check is skipped (back-compat).

    Returns:
        None if clean, or a short reason string if spam.
        The reason is for logging only; never expose to the submitter.
    """
    # 1. Honeypot: check common field names
    for hp_field in ('website', 'honeypot', 'url', 'fax'):
        if data.get(hp_field, '').strip():
            return f'honeypot:{hp_field}'

    # 2. Origin / Referer check — blocks direct API POSTs from non-browsers
    if expected_hosts:
        host = None
        if origin:
            try:
                host = urllib.parse.urlparse(origin).hostname
            except Exception:
                host = None
        if not host or host not in expected_hosts:
            return f'bad_origin:{host or "missing"}'

    # 3. User-Agent blocklist — no real browser sends curl/python-requests
    if user_agent is not None and _UA_BLOCKLIST.match(user_agent):
        return f'bad_ua:{user_agent[:40]}'

    # 4. Timing: client sends time_open in ms; reject if under 3s
    time_open = data.get('time_open', 0)
    try:
        time_open = int(time_open)
    except (TypeError, ValueError):
        time_open = 0
    if time_open < 3000:
        return f'too_fast:{time_open}ms'

    # 5. IP rate limit
    _clean_old(_ip_submissions, ip)
    if len(_ip_submissions[ip]) >= RATE_LIMIT_IP:
        return f'rate_limit_ip:{ip}'

    # 6. Email domain check + normalized-email rate limit
    email = data.get('email', '')
    normalized = normalize_email(email)
    if '@' in normalized:
        domain = normalized.rsplit('@', 1)[1]
        if domain in BLOCKED_DOMAINS:
            return f'blocked_domain:{domain}'

    if normalized:
        _clean_old(_email_submissions, normalized)
        if len(_email_submissions[normalized]) >= RATE_LIMIT_EMAIL:
            return f'rate_limit_email:{normalized}'

    # 7. StopForumSpam shared-intel reputation — IP + email
    sfs_reason = _check_stopforumspam(ip, email)
    if sfs_reason:
        return sfs_reason

    # 8. Content pattern scan
    if fields is None:
        fields = ['name', 'company', 'subject', 'message']
    blob = ' '.join(str(data.get(f, '')) for f in fields)
    for pattern, desc in SPAM_PATTERNS:
        if pattern.search(blob):
            return f'content:{desc}'

    # All clear: record this submission for rate limiting
    _ip_submissions[ip].append(time.time())
    if normalized:
        _email_submissions[normalized].append(time.time())
    return None
