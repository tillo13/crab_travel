"""Shared visitor-logging module for ALL of Andy's Flask apps.

Lives at: ~/Desktop/code/_local_infrastructure/visitor_logging/visitor_logging.py
Synced into each project's utilities/ via deploy.json shared_files.

Purpose:
- Log every public page view across all 12+ Andy sites into one shared table
  (kumori_ops.visitor_log) so Andy can see crawler traffic, real human clicks,
  and reverse-sell DM-link engagement portfolio-wide.
- Self-contained: brings its own psycopg2 + Secret Manager creds, depends on
  nothing in the consuming app. Mirrors anthropic_logger.py's shape.

Install per-app — two lines in app.py:

    from utilities.visitor_logging import install_middleware
    install_middleware(app, 'inroads')   # pass your site's slug

That's it. Every GET request automatically logged. Failures are swallowed.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Volume control (per r/webdev community consensus 2026-05-08) ─────────────
# Logging every page view individually is anti-pattern at portfolio scale.
# Three levers, all on by default:
#
#   Lever 1 — bot sampling: log only BOT_SAMPLE_RATE of bot hits (default 2%).
#             Cuts ~98% of bot row volume while preserving trend visibility.
#             get_stats() multiplies bot counts by 1/BOT_SAMPLE_RATE so
#             dashboards still show accurate (extrapolated) numbers.
#
#   Lever 2 — skip noise paths: internal cron + polling + API endpoints that
#             aren't real eyeball traffic (see _SKIP_PATH_PREFIXES below).
#
#   Lever 3 — 90-day TTL: flusher periodically purges rows older than
#             VISITOR_LOG_TTL_DAYS. No external cron needed.

BOT_SAMPLE_RATE = 0.02
VISITOR_LOG_TTL_DAYS = 90
TTL_PURGE_INTERVAL_SEC = 3600  # purge at most once per hour from any process
TTL_PURGE_BATCH = 5000         # rows per DELETE — keeps each statement well under the 5s statement_timeout
TTL_PURGE_MAX_BATCHES = 40     # safety cap (<=200k rows/cycle); a huge backlog drains over successive hours

_KUMORI_PROJECT = 'kumori-404602'

# ─── DB creds (cached) ────────────────────────────────────────────────────────

_DB_CREDS_CACHE: Optional[dict] = None


def _get_db_creds() -> dict:
    global _DB_CREDS_CACHE
    if _DB_CREDS_CACHE:
        return _DB_CREDS_CACHE
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()

    def fetch(name: str) -> str:
        path = f"projects/{_KUMORI_PROJECT}/secrets/{name}/versions/latest"
        return client.access_secret_version(request={"name": path}).payload.data.decode("UTF-8")

    def fetch_or(primary: str, fallback: str) -> str:
        # Loggers connect as the dedicated least-privilege telemetry_writer role
        # (INSERT-only on the telemetry tables) rather than the shared postgres
        # role, so an app's logger can never reach another tenant's data. Falls
        # back to KUMORI_* until TELEMETRY_* is provisioned, so no flag day.
        try:
            return fetch(primary)
        except Exception:
            return fetch(fallback)

    _DB_CREDS_CACHE = {
        'host': fetch('KUMORI_POSTGRES_IP'),
        'dbname': fetch('KUMORI_POSTGRES_DB_NAME'),
        'user': fetch_or('TELEMETRY_POSTGRES_USERNAME', 'KUMORI_POSTGRES_USERNAME'),
        'password': fetch_or('TELEMETRY_POSTGRES_PASSWORD', 'KUMORI_POSTGRES_PASSWORD'),
        'connection_name': fetch('KUMORI_POSTGRES_CONNECTION_NAME'),
    }
    return _DB_CREDS_CACHE


def _connect():
    """Connect to kumori-404602 Postgres. Works with either psycopg2 (v2) or
    psycopg (v3) installed — Andy's projects mix the two."""
    creds = _get_db_creds()
    is_gcp = os.environ.get('GAE_ENV', '').startswith('standard') or os.path.exists('/cloudsql')
    if is_gcp:
        socket_dir = os.environ.get('DB_SOCKET_DIR', '/cloudsql')
        host = f"{socket_dir}/{creds['connection_name']}"
    else:
        host = creds['host']
    try:
        import psycopg2
        return psycopg2.connect(
            host=host, dbname=creds['dbname'], user=creds['user'],
            password=creds['password'], connect_timeout=5,
            options='-c statement_timeout=5000',
        )
    except ImportError:
        import psycopg
        return psycopg.connect(
            host=host, dbname=creds['dbname'], user=creds['user'],
            password=creds['password'], connect_timeout=5,
            options='-c statement_timeout=5000',
        )


# ─── Bot detection / source classification ───────────────────────────────────

_BOT_UA_MARKERS = (
    'bot', 'crawler', 'spider', 'preview', 'unfurl', 'fetch',
    'linkedinbot', 'slackbot', 'twitterbot', 'facebookexternalhit',
    'whatsapp', 'discordbot', 'telegrambot', 'googlebot', 'bingbot',
    'duckduckbot', 'applebot', 'embedly', 'curl', 'wget', 'python-requests',
)


def _looks_like_bot(user_agent: Optional[str]) -> bool:
    if not user_agent:
        return True
    return any(m in user_agent.lower() for m in _BOT_UA_MARKERS)


_SOURCE_RULES = (
    ('linkedinbot', 'LinkedIn'),
    ('slackbot', 'Slack'),
    ('twitterbot', 'Twitter'),
    ('facebookexternalhit', 'Facebook'),
    ('whatsapp', 'WhatsApp'),
    ('discordbot', 'Discord'),
    ('telegrambot', 'Telegram'),
    ('gptbot', 'GPTBot (OpenAI)'),
    ('oai-searchbot', 'OAI-SearchBot'),
    ('chatgpt-user', 'ChatGPT-User'),
    ('claudebot', 'ClaudeBot (Anthropic)'),
    ('claude-searchbot', 'Claude-SearchBot'),
    ('claude-user', 'Claude-User'),
    ('perplexitybot', 'Perplexity'),
    ('perplexity-user', 'Perplexity-User'),
    ('ccbot', 'CommonCrawl'),
    ('bytespider', 'Bytespider (TikTok)'),
    ('google-extended', 'Google-Extended'),
    ('googlebot', 'Googlebot'),
    ('bingbot', 'Bingbot'),
    ('duckduckbot', 'DuckDuckBot'),
    ('applebot', 'Applebot'),
    ('ahrefsbot', 'Ahrefs'),
    ('semrushbot', 'Semrush'),
    ('mj12bot', 'Majestic'),
    ('embedly', 'Embedly'),
    ('curl/', 'curl'),
    ('wget/', 'wget'),
    ('python-requests', 'python-requests'),
)


def classify_user_agent(user_agent: Optional[str]) -> str:
    """Short human-readable source label for a UA string."""
    if not user_agent:
        return 'Unknown'
    ua = user_agent.lower()
    for marker, label in _SOURCE_RULES:
        if marker in ua:
            return label
    if 'mozilla' in ua and 'bot' not in ua and 'crawler' not in ua:
        return 'Browser'
    return 'Other'


# ─── robots.txt bot-block stanza (ship with every site) ──────────────────────
#
# Andy's universal bot-block policy — append this stanza to every site's
# existing robots.txt content. Keeps real search engines (Googlebot, Bingbot,
# Applebot, DuckDuckBot) and live-citation engines (OAI-SearchBot,
# ChatGPT-User, PerplexityBot, Claude-SearchBot) UNBLOCKED so the site stays
# discoverable + cited. Blocks training crawlers and SEO scrapers that pay
# nothing back. Decision data: kumori_ops.visitor_log shows GPTBot alone
# accounts for ~94% of all bot traffic across the portfolio (May 2026).

BOT_BLOCK_STANZA = """
# ─── AI training crawlers (block — they pay nothing back) ───
User-agent: GPTBot
Disallow: /

User-agent: ClaudeBot
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: Applebot-Extended
Disallow: /

User-agent: meta-externalagent
Disallow: /

User-agent: Meta-ExternalFetcher
Disallow: /

User-agent: Amazonbot
Disallow: /

User-agent: Diffbot
Disallow: /

User-agent: omgili
Disallow: /

User-agent: FacebookBot
Disallow: /

User-agent: cohere-ai
Disallow: /

User-agent: ImagesiftBot
Disallow: /

User-agent: PetalBot
Disallow: /

User-agent: Timpibot
Disallow: /

User-agent: VelenPublicWebCrawler
Disallow: /

User-agent: Webzio-Extended
Disallow: /

User-agent: YouBot
Disallow: /

# ─── SEO scrapers (block — they sell competitor research, no traffic to you) ───
User-agent: SemrushBot
Disallow: /

User-agent: AhrefsBot
Disallow: /

User-agent: MJ12bot
Disallow: /

User-agent: DataForSeoBot
Disallow: /

User-agent: BLEXBot
Disallow: /

User-agent: DotBot
Disallow: /

User-agent: SeekportBot
Disallow: /

User-agent: serpstatbot
Disallow: /
""".strip() + "\n"


def append_bot_block(existing_robots_txt: str) -> str:
    """Append the canonical bot-block stanza to an existing robots.txt body.
    Idempotent — if the stanza is already present, returns input unchanged."""
    if 'GPTBot' in (existing_robots_txt or ''):
        return existing_robots_txt
    body = (existing_robots_txt or '').rstrip() + '\n\n'
    return body + BOT_BLOCK_STANZA


# ─── Insert: queue + batched flusher ──────────────────────────────────────────
#
# Why batched: per-request `psycopg2.connect()` hammers Cloud SQL
# (max_connections=50 on the shared kumori instance, ~9k page views/hour during
# peak GPTBot crawl = ~9k connect/close per hour from this module alone). On
# 2026-05-08 that pattern combined with a deploy-time 2× instance overlap on
# one high-traffic app saturated the pool and slowed sites portfolio-wide. This was a direct
# violation of db-speed-first/SKILL.md ("NEVER use direct psycopg2.connect()
# without pooling").
#
# Now: log_view() only enqueues. A daemon thread holds ONE persistent
# connection per process and flushes the queue every FLUSH_INTERVAL_SEC, OR
# when the queue hits FLUSH_BATCH_MAX, whichever comes first. Connection
# count from this module drops from O(requests) to O(processes).
#
# ─── kumori-shared connection budget (per db-speed-first/SKILL.md) ───────────
# Allocation: 1 connection per App Engine process per site (held by the
# flusher daemon). At auto-scale of 1-3 instances per site × 11 sites, the
# steady-state cap from visitor_logging is ~33 connections in the worst case,
# typically ~11. The budget table in db-speed-first should be amended to add:
#     visitor_logging: 1/process across all sites
# Stale-connection probe (SELECT 1) is mandatory before reuse — same fix that
# resolved a 2026-04-04 prod outage on a portfolio app.

import queue as _q

FLUSH_INTERVAL_SEC = 5.0
FLUSH_BATCH_MAX    = 200       # also flush when queue reaches this size
QUEUE_HARD_CAP     = 5000      # drop oldest if we exceed (backpressure)

_log_queue: _q.Queue = _q.Queue(maxsize=QUEUE_HARD_CAP)
_flusher_started = False
_flusher_lock = threading.Lock()
_persistent_conn = None  # module-level reusable connection


def _get_persistent_conn():
    """Return the module-level Postgres connection, reconnecting if dropped.

    Cloud SQL drops idle connections after ~10 min and App Engine scales to 0,
    so we MUST do a SELECT 1 liveness probe before reusing — pattern from
    db-speed-first/SKILL.md (the same fix that resolved the portfolio-app
    2026-04-04 prod outage). `.closed` alone isn't enough: a TCP-half-open
    socket can read 0 from .closed yet fail on first execute()."""
    global _persistent_conn
    if _persistent_conn is not None:
        try:
            if getattr(_persistent_conn, 'closed', 1) == 0:
                cur = _persistent_conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
                return _persistent_conn
        except Exception:
            try:
                _persistent_conn.close()
            except Exception:
                pass
            _persistent_conn = None
    _persistent_conn = _connect()
    return _persistent_conn


def _flush_batch(rows):
    """Insert a batch of rows via the persistent connection. On failure,
    drop the connection so the next flush retries fresh."""
    global _persistent_conn
    if not rows:
        return
    try:
        conn = _get_persistent_conn()
        cur = conn.cursor()
        # executemany works for both psycopg2 and psycopg v3
        cur.executemany("""
            INSERT INTO kumori_ops.visitor_log
                (app, path, ip, user_agent, referrer, is_bot, is_authed, user_email)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, rows)
        conn.commit()
    except Exception as e:
        logger.warning(f"visitor_logging: batch flush failed ({len(rows)} rows): {e}")
        # Drop the connection so the next flush retries fresh.
        try:
            if _persistent_conn is not None:
                _persistent_conn.close()
        except Exception:
            pass
        _persistent_conn = None


_last_ttl_purge_at = 0.0


def _maybe_ttl_purge():
    """Run a 90-day TTL purge at most once per TTL_PURGE_INTERVAL_SEC per
    process. Cheap if there's nothing to delete (indexed on viewed_at).
    Doesn't block the queue — purge runs after a flush, on the same conn."""
    global _last_ttl_purge_at, _persistent_conn
    now = time.time()
    if now - _last_ttl_purge_at < TTL_PURGE_INTERVAL_SEC:
        return
    _last_ttl_purge_at = now
    try:
        conn = _get_persistent_conn()
        cur = conn.cursor()
        # Delete in bounded batches so a single statement never approaches the
        # 5s statement_timeout, even with a large backlog, and only short locks
        # are held on the shared kumori_ops.visitor_log table. A huge backlog
        # drains across successive hourly cycles rather than one giant DELETE.
        deleted = 0
        for _ in range(TTL_PURGE_MAX_BATCHES):
            cur.execute(
                "DELETE FROM kumori_ops.visitor_log WHERE ctid IN ("
                "  SELECT ctid FROM kumori_ops.visitor_log "
                "  WHERE viewed_at < NOW() - (%s || ' days')::interval "
                "  LIMIT %s)",
                (VISITOR_LOG_TTL_DAYS, TTL_PURGE_BATCH),
            )
            n = cur.rowcount
            conn.commit()
            if n > 0:
                deleted += n
            if n < TTL_PURGE_BATCH:
                break  # caught up — fewer than a full batch remained
        if deleted:
            logger.info(f"visitor_logging: TTL purged {deleted} rows older than {VISITOR_LOG_TTL_DAYS}d")
    except Exception as e:
        logger.warning(f"visitor_logging: TTL purge failed: {e}")
        try:
            if _persistent_conn is not None:
                _persistent_conn.close()
        except Exception:
            pass
        _persistent_conn = None


def _flusher_loop():
    """Daemon loop: drain queue every FLUSH_INTERVAL_SEC (or when full)."""
    while True:
        # Block-with-timeout for the first item so we don't busy-loop empty.
        try:
            first = _log_queue.get(timeout=FLUSH_INTERVAL_SEC)
        except _q.Empty:
            _maybe_ttl_purge()  # opportunistic — empty cycle is a fine time
            continue
        batch = [first]
        # Drain whatever else is already queued, up to FLUSH_BATCH_MAX.
        while len(batch) < FLUSH_BATCH_MAX:
            try:
                batch.append(_log_queue.get_nowait())
            except _q.Empty:
                break
        try:
            _flush_batch(batch)
        except Exception as e:
            logger.warning(f"visitor_logging: flusher swallowed: {e}")
        _maybe_ttl_purge()


def _ensure_flusher_started():
    global _flusher_started
    if _flusher_started:
        return
    with _flusher_lock:
        if _flusher_started:
            return
        t = threading.Thread(target=_flusher_loop, daemon=True,
                             name='visitor_logging.flusher')
        t.start()
        _flusher_started = True


def log_view(app_name: str, path: str, ip: str = '', user_agent: str = '',
             referrer: str = '', is_authed: bool = False,
             user_email: str = '') -> None:
    """Enqueue one row. Returns immediately; insert happens in a batched
    flush on a daemon thread. Drops silently if the queue is at hard cap
    (acceptable for telemetry — never block a request on logging).

    Bot rows are sampled at BOT_SAMPLE_RATE (default 2%) — get_stats()
    extrapolates back to true counts. Human rows always 100%.

    user_email is captured ONLY when is_authed=True. Enables per-user path
    forensics on .io admin pages without an email/Slack notification rail."""
    is_bot = _looks_like_bot(user_agent)
    if is_bot and random.random() >= BOT_SAMPLE_RATE:
        return  # sampled out
    _ensure_flusher_started()
    row = (
        (app_name or '')[:64],
        (path or '')[:1000],
        (ip or '')[:64],
        (user_agent or '')[:1000],
        (referrer or '')[:1000],
        is_bot,
        bool(is_authed),
        (user_email or '').lower()[:320] or None,
    )
    try:
        _log_queue.put_nowait(row)
    except _q.Full:
        # Backpressure: drop and warn. Better than blocking the request.
        logger.warning("visitor_logging: queue at hard cap, dropping row")


# ─── Flask middleware install ────────────────────────────────────────────────

# Paths whose hits we don't care about (cuts noise + avoids logging the
# dashboard itself, which would create a feedback loop). Expanded 2026-05-08
# after seeing internal cron / polling endpoints dominate "humans" stats —
# galactica's /api/crew/mission/*, dandy's /cron/check-email, wattson's
# /api/cron/poll, one app's /api/time-pulse/* etc. were all logged as
# "humans" because they pass UA-based bot detection.
_SKIP_PATH_PREFIXES = (
    '/static/',
    '/admin/', '/api/admin/',
    '/health', '/healthz',
    '/favicon.ico', '/robots.txt', '/sitemap.xml', '/feed.xml',
    # Server-to-server cron + polling
    '/cron/', '/api/cron/', '/_ah/', '/tasks/',
    # App-internal API patterns surfaced by visitor_logging itself —
    # safe to skip on EVERY site since they're low-cardinality internal traffic
    '/api/crew/', '/api/time-pulse/', '/api/seen', '/api/track',
    '/api/random', '/api/stats', '/api/robot/',
)


def install_middleware(flask_app, app_name: str, *,
                       authed_check=None,
                       skip_prefixes: tuple = _SKIP_PATH_PREFIXES):
    """Register a before_request hook that logs every public GET to
    kumori_ops.visitor_log under `app_name`. Idempotent — safe to call twice.

    `authed_check` is an optional callable taking no args returning bool;
    defaults to checking Flask `session` for a 'user' key.

    `skip_prefixes` overrides the default list of paths to skip."""
    from flask import request, session

    if getattr(flask_app, '_visitor_logging_installed', False):
        return
    flask_app._visitor_logging_installed = True

    def _default_authed():
        try:
            return 'user' in session
        except Exception:
            return False

    auth_fn = authed_check or _default_authed

    def _default_email():
        try:
            # When admin is mimicking, attribute the visit to the REAL admin
            # not the mimicked target. Otherwise visitor_log would show "Kenny
            # visited X" when Andy was actually mimicking Kenny — confusing for
            # forensics. Surface the target in the path-drawer view separately.
            real = session.get('_real_user')
            if real and real.get('email'):
                return (real['email'] or '').lower()
            u = session.get('user') or {}
            return (u.get('email') or '').lower()
        except Exception:
            return ''

    @flask_app.before_request
    def _visitor_log_hook():
        try:
            if request.method != 'GET':
                return None
            path = request.path or ''
            for p in skip_prefixes:
                if path.startswith(p):
                    return None
            ip = (request.headers.get('X-Forwarded-For') or
                  request.remote_addr or '').split(',')[0].strip()
            authed = bool(auth_fn())
            log_view(
                app_name=app_name,
                path=path,
                ip=ip,
                user_agent=request.headers.get('User-Agent', ''),
                referrer=request.headers.get('Referer', ''),
                is_authed=authed,
                user_email=_default_email() if authed else '',
            )
        except Exception as e:
            logger.warning(f"visitor_logging hook error: {e}")
        return None


# ─── Read helpers (used by dashboards) ───────────────────────────────────────

def get_recent_views(app_name: Optional[str] = None, limit: int = 300,
                     human_only: bool = True):
    """Return recent rows. Each row is a dict with `source` + `from_linkedin`
    fields derived in Python (so dashboards don't need to re-derive)."""
    sql = """
        SELECT id, app, path, ip, user_agent, referrer,
               is_bot, is_authed, viewed_at
        FROM kumori_ops.visitor_log
        WHERE (%s = FALSE OR is_bot = FALSE)
          AND (%s::text IS NULL OR app = %s)
        ORDER BY viewed_at DESC
        LIMIT %s
    """
    try:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, (human_only, app_name, app_name, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"visitor_logging.get_recent_views: {e}")
        return []
    for r in rows:
        r['source'] = classify_user_agent(r.get('user_agent'))
        ref = (r.get('referrer') or '').lower()
        r['from_linkedin'] = 'linkedin.com' in ref or 'lnkd.in' in ref
    return rows


def get_stats(app_name: Optional[str] = None, hours: int = 24) -> dict:
    """Counts of humans / LinkedIn-referred / bots in the last N hours, plus
    a top-source breakdown for the bot side, and per-app totals if no filter.

    Bot row counts are EXTRAPOLATED back to true volume using BOT_SAMPLE_RATE
    (rows divided by 0.02 → ~50× the literal table count). The dashboard
    surfaces the extrapolated numbers; raw table counts are available via
    get_recent_views(human_only=False)."""
    out = {
        'humans': 0, 'from_linkedin': 0, 'bots': 0, 'total': 0,
        'bot_breakdown': [], 'app_breakdown': [],
        'window_hours': hours, 'app_filter': app_name,
        'bot_sample_rate': BOT_SAMPLE_RATE,
    }
    bot_mult = (1.0 / BOT_SAMPLE_RATE) if BOT_SAMPLE_RATE > 0 else 1.0
    try:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE is_bot = FALSE) AS humans,
                    COUNT(*) FILTER (WHERE is_bot = FALSE
                                      AND (LOWER(referrer) LIKE %s
                                           OR LOWER(referrer) LIKE %s)) AS from_linkedin,
                    COUNT(*) FILTER (WHERE is_bot = TRUE) AS bots,
                    COUNT(*) AS total
                FROM kumori_ops.visitor_log
                WHERE viewed_at > NOW() - (%s || ' hours')::interval
                  AND (%s::text IS NULL OR app = %s)
            """, ('%linkedin.com%', '%lnkd.in%', hours, app_name, app_name))
            row = cur.fetchone()
            humans, from_li, bots_raw, total_raw = row
            # Extrapolate bot rows back to true volume. Humans aren't sampled.
            out['humans'] = humans
            out['from_linkedin'] = from_li
            out['bots'] = int(round(bots_raw * bot_mult))
            out['total'] = humans + out['bots']

            cur.execute("""
                SELECT user_agent, COUNT(*) AS n
                FROM kumori_ops.visitor_log
                WHERE is_bot = TRUE
                  AND viewed_at > NOW() - (%s || ' hours')::interval
                  AND (%s::text IS NULL OR app = %s)
                GROUP BY user_agent
                ORDER BY n DESC
                LIMIT 8
            """, (hours, app_name, app_name))
            out['bot_breakdown'] = [
                {'source': classify_user_agent(ua), 'count': int(round(n * bot_mult))}
                for ua, n in cur.fetchall()
            ]

            if app_name is None:
                cur.execute("""
                    SELECT app,
                           COUNT(*) FILTER (WHERE is_bot = FALSE) AS humans,
                           COUNT(*) FILTER (WHERE is_bot = TRUE)  AS bots,
                           COUNT(*) AS total
                    FROM kumori_ops.visitor_log
                    WHERE viewed_at > NOW() - (%s || ' hours')::interval
                    GROUP BY app
                    ORDER BY total DESC
                """, (hours,))
                out['app_breakdown'] = [
                    {
                        'app': a, 'humans': h,
                        'bots': int(round(b * bot_mult)),
                        'total': h + int(round(b * bot_mult)),
                    }
                    for a, h, b, t in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"visitor_logging.get_stats: {e}")
    return out
