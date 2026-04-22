"""Shared contact form spam guard, used across all kumori-hosted sites.

Layers:
1. Honeypot check (already on most sites, but centralized here)
2. Timing check with server-side verification
3. Content pattern blocklist (catches SEO pitches, marketing spam)
4. Email domain blocklist (disposable/known-spam domains)
5. IP rate limiting (in-memory, resets on deploy)
6. Normalized-email rate limiting — collapses Gmail dot/plus variants so
   `a.b.c@gmail.com`, `abc@gmail.com`, and `abc+tag@gmail.com` share a bucket
   (added 2026-04-16 after pilgrims.world waitlist was flooded with dot spam)
"""

import logging
import re
import time
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
RATE_LIMIT_IP = 3
RATE_LIMIT_EMAIL = 2
RATE_WINDOW = 3600      # seconds (1 hour)


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


def check_spam(data: dict, ip: str, fields: list[str] | None = None) -> str | None:
    """Run all spam checks against a contact form submission.

    Args:
        data: form data dict (expects keys like name, email, message, etc.)
        ip: requester IP address
        fields: which keys to concatenate for content scanning
                (default: name, company, subject, message)

    Returns:
        None if clean, or a short reason string if spam.
        The reason is for logging only; never expose to the submitter.
    """
    # 1. Honeypot: check common field names
    for hp_field in ('website', 'honeypot', 'url', 'fax'):
        if data.get(hp_field, '').strip():
            return f'honeypot:{hp_field}'

    # 2. Timing: client sends time_open in ms; reject if under 3s
    time_open = data.get('time_open', 0)
    try:
        time_open = int(time_open)
    except (TypeError, ValueError):
        time_open = 0
    if time_open < 3000:
        return f'too_fast:{time_open}ms'

    # 3. IP rate limit
    _clean_old(_ip_submissions, ip)
    if len(_ip_submissions[ip]) >= RATE_LIMIT_IP:
        return f'rate_limit_ip:{ip}'

    # 4. Email domain check + normalized-email rate limit
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

    # 5. Content pattern scan
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
