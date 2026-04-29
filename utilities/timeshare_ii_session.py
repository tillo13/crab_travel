"""
Interval International session keep-alive + cookie store.

The hard constraint: II's auth uses Akamai bot detection on the login boundary.
Headless login is impossible (proven in docs/ii_scraper_playbook.md §12). The
only path is cookie-replay from a real Chrome session that already passed
Akamai's challenge.

This module manages the cookie blob + session health:

- POST /api/timeshare/ii-cookies/refresh  ← Mac LaunchAgent (or manual seed)
                                            pushes fresh cookies here
- GET  /tasks/timeshare-ii-keepalive       ← App Engine cron pings II's
                                            /web/my/home every 18-29 min so
                                            JSESSIONID's idle timeout never
                                            fires

Cost ceiling: $1/mo budget alert on crab-travel project (approved 2026-04-28).
Realistic incremental cost: $0.00–$0.06/mo. Math in
docs/timeshare_buildout.md §"II keep-alive cost approval".
"""
import json
import logging
import os
import random
import time
from datetime import datetime, timezone

import psycopg2.extras
import requests

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.timeshare_ii_session')

II_HOME = "https://www.intervalworld.com/web/my/home"
II_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
# Jitter window: 18–29 min. Spring Security idle timeout is unknown; tonight's
# evidence shows cookies died inside a 30-min window. Ping just inside that
# ceiling, randomized so we don't hit a fixed cadence detectable as a bot.
JITTER_MIN_SEC = 18 * 60
JITTER_MAX_SEC = 29 * 60


def _now():
    return datetime.now(timezone.utc)


def get_session_row(member_login='tilloat'):
    """Return the single row for this II login, or None."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM crab.timeshare_ii_session WHERE member_login=%s",
            (member_login,)
        )
        return cur.fetchone()
    finally:
        conn.close()


def upsert_cookies(cookies_dict, member_login='tilloat', source='manual'):
    """Write a fresh cookie blob. Resets failure counters on success."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_ii_session
                (member_login, cookies, last_pushed_from, updated_at)
            VALUES (%s, %s::jsonb, %s, NOW())
            ON CONFLICT (member_login) DO UPDATE SET
                cookies = EXCLUDED.cookies,
                last_pushed_from = EXCLUDED.last_pushed_from,
                consecutive_failures = 0,
                updated_at = NOW()
        """, (member_login, json.dumps(cookies_dict), source))
        conn.commit()
        return True
    finally:
        conn.close()


def _record_keepalive(member_login, healthy, error=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if healthy:
            cur.execute("""
                UPDATE crab.timeshare_ii_session
                   SET last_keepalive_at = NOW(),
                       last_keepalive_status = 'healthy',
                       last_error = NULL,
                       consecutive_failures = 0,
                       keepalive_count = keepalive_count + 1,
                       updated_at = NOW()
                 WHERE member_login = %s
            """, (member_login,))
        else:
            cur.execute("""
                UPDATE crab.timeshare_ii_session
                   SET last_keepalive_at = NOW(),
                       last_keepalive_status = 'unhealthy',
                       last_error = %s,
                       consecutive_failures = consecutive_failures + 1,
                       updated_at = NOW()
                 WHERE member_login = %s
            """, (error or 'unknown', member_login))
        conn.commit()
    finally:
        conn.close()


def keepalive_ping(member_login='tilloat'):
    """One ping cycle. Returns dict with status. Designed to be called from
    the App Engine cron handler. Self-defers if too soon since last ping."""
    row = get_session_row(member_login)
    if not row:
        return {'status': 'no_session', 'message': 'no cookies seeded yet'}

    last = row['last_keepalive_at']
    if last:
        elapsed = (_now() - last).total_seconds()
        target = JITTER_MIN_SEC + random.random() * (JITTER_MAX_SEC - JITTER_MIN_SEC)
        if elapsed < target:
            return {
                'status': 'deferred',
                'elapsed_sec': round(elapsed),
                'target_sec': round(target),
                'reason': f'next ping in {round(target - elapsed)}s',
            }

    cookies = row['cookies'] or {}
    if not cookies:
        return {'status': 'no_cookies'}

    S = requests.Session()
    S.headers.update({
        'User-Agent': II_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    S.cookies.update(cookies)
    try:
        r = S.get(II_HOME, timeout=20, allow_redirects=True)
    except Exception as e:
        _record_keepalive(member_login, healthy=False, error=f'request failed: {e}')
        return {'status': 'request_error', 'error': str(e)}

    body_head = r.text[:8000] if r.text else ''
    # Positive signal: member-area nav in the response body. Negative signals:
    # redirect to loginPage or "Sign In" prompt visible.
    healthy = (
        r.status_code == 200
        and 'loginPage' not in (r.url or '')
        and ('My Account' in body_head or 'My Units' in body_head)
        and 'Member Login' not in body_head
    )

    _record_keepalive(
        member_login,
        healthy=healthy,
        error=None if healthy else f'status={r.status_code} url={r.url[:120]}',
    )

    # Update the cookie blob with whatever the server sent back (e.g. refreshed __uzm*)
    if healthy and r.cookies:
        merged = dict(cookies)
        for k, v in r.cookies.get_dict().items():
            merged[k] = v
        if merged != cookies:
            upsert_cookies(merged, member_login=member_login, source='keepalive_refresh')

    return {
        'status': 'healthy' if healthy else 'unhealthy',
        'http_status': r.status_code,
        'final_url': r.url,
        'response_bytes': len(r.text or ''),
    }
