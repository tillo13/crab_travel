"""Daily heartbeat for crab.travel — one email/day, exception-report shape.

Two sections:
  1. Is Crab Healthy? — ✅/⚠️/🔴 status of every cron, DB, OpenCrab pipeline,
     LLM routing. Quick-skim line per item.
  2. Things We're Waiting On — active price watches, II scrape position,
     OpenCrab pass activity, plans with pending state.

Folds the 5 separate OpenCrab test emails into a single line summary.
Each subsystem query is wrapped in try/except so a single missing table
or schema drift never silences the whole digest.

Wire: registered as `daily_heartbeat_bp` in app.py + cron.yaml entry at 8am PT.
"""
import logging
from datetime import datetime, timedelta, timezone

import psycopg2.extras
from flask import Blueprint, jsonify, request

from utilities.gmail_utils import send_simple_email
from utilities.postgres_utils import get_db_connection

logger = logging.getLogger(__name__)

bp = Blueprint('daily_heartbeat', __name__)

ADMIN = 'andy.tillo@gmail.com'
PT = timezone(timedelta(hours=-7))


def _safe(label, fn, conn=None):
    """Run a query callable; return its result or an error sentinel for the
    digest to render as ⚠️  rather than crashing the whole report.

    On failure, rolls back the connection so a subsequent query doesn't
    inherit "current transaction is aborted" state from the abort cascade.
    """
    try:
        return fn()
    except Exception as e:
        logger.warning(f"heartbeat[{label}]: {e}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return {'_error': str(e)[:200]}


def _status_emoji(ok, stale=False):
    if isinstance(ok, dict) and '_error' in ok:
        return '⚠️'
    if stale:
        return '🟠'
    return '✅' if ok else '🔴'


def _check_crons(cur):
    """Return list of cron health rows. Each: (name, emoji, summary)."""
    rows = []
    # /tasks/crawl every 2h → expect a bot_run within 3h slack
    cur.execute("""
        SELECT MAX(started_at) AS last_run, COUNT(*) AS runs_24h
        FROM crab.bot_runs
        WHERE started_at > NOW() - INTERVAL '24 hours'
    """)
    r = cur.fetchone()
    last = r['last_run']
    age_min = int((datetime.now(timezone.utc) - last).total_seconds() / 60) if last else None
    fresh = age_min is not None and age_min < 180
    summary = (f"last run {age_min}min ago, {r['runs_24h']} runs in 24h"
               if last else "no runs in 24h")
    rows.append(('crawl (every 2h)', _status_emoji(fresh), summary))

    # /tasks/refresh-deals every 24h → expect deals_cache.updated_at within 26h
    cur.execute("""
        SELECT MAX(updated_at) AS last_refresh, COUNT(*) AS deals
        FROM crab.deals_cache
    """)
    r = cur.fetchone()
    last = r['last_refresh']
    age_h = int((datetime.now(timezone.utc) - last).total_seconds() / 3600) if last else None
    fresh = age_h is not None and age_h < 26
    summary = (f"last refresh {age_h}h ago, {r['deals']} deals cached"
               if last else "deals_cache empty")
    rows.append(('refresh-deals (every 24h)', _status_emoji(fresh), summary))

    # /tasks/check-watches every 8h → use member_watches.last_checked_at
    cur.execute("""
        SELECT MAX(last_checked_at) AS last_check, COUNT(*) AS active
        FROM crab.member_watches WHERE active = TRUE
    """)
    r = cur.fetchone()
    last = r['last_check']
    age_h = int((datetime.now(timezone.utc) - last).total_seconds() / 3600) if last else None
    fresh = age_h is not None and age_h < 10
    summary = (f"last check {age_h}h ago, {r['active']} active watches"
               if last else (f"{r['active']} active watches but never checked"
                             if r['active'] else "no active watches"))
    rows.append(('check-watches (every 8h)', _status_emoji(fresh), summary))

    return rows


def _check_opencrab(cur):
    """Roll up today's OpenCrab activity into a single line."""
    cur.execute("""
        SELECT COUNT(*) AS sent_24h,
               COUNT(DISTINCT plan_id) AS distinct_plans
        FROM crab.notifications_sent
        WHERE created_at > NOW() - INTERVAL '24 hours'
          AND notification_type LIKE 'opencrab%'
    """)
    r = cur.fetchone()
    sent = int(r['sent_24h'] or 0)
    plans = int(r['distinct_plans'] or 0)
    if sent == 0:
        return ('OpenCrab', '🟠', 'no notifications recorded in last 24h '
                                   '(OpenCrab silent? check VPS)')
    return ('OpenCrab', '✅',
            f"{sent} notification(s) recorded across {plans} plan(s) in last 24h")


def _check_db_pool(cur):
    cur.execute("""
        SELECT COUNT(*) AS active,
               (SELECT setting::int FROM pg_settings WHERE name='max_connections') AS cap
        FROM pg_stat_activity WHERE state = 'active'
    """)
    r = cur.fetchone()
    pct = round(r['active'] / r['cap'] * 100, 1) if r['cap'] else 0
    fresh = pct < 70
    summary = f"{r['active']}/{r['cap']} active connections ({pct}%)"
    return ('Cloud SQL pool', _status_emoji(fresh), summary)


def _check_llm_routing(cur):
    """Read kumori_llm_daily_caps for crab_travel's LLM activity."""
    cur.execute("""
        SELECT backend, SUM(call_count) AS calls
        FROM kumori_llm_daily_caps
        WHERE app_name = 'crab_travel'
          AND usage_date > CURRENT_DATE - INTERVAL '7 days'
        GROUP BY backend ORDER BY calls DESC LIMIT 5
    """)
    rows = cur.fetchall()
    if not rows:
        return ('LLM routing', '🟠', 'no crab_travel LLM calls in 7d (trip-AI silent?)')
    top = ', '.join(f"{r['backend']}={r['calls']}" for r in rows[:3])
    total = sum(int(r['calls']) for r in rows)
    return ('LLM routing', '✅', f"{total} calls/7d — top: {top}")


def _waiting_watches(cur):
    cur.execute("""
        SELECT COUNT(*) AS active,
               MIN(created_at) AS oldest_created,
               COUNT(*) FILTER (WHERE last_match_at IS NULL) AS never_matched
        FROM crab.member_watches
        WHERE active = TRUE
    """)
    r = cur.fetchone()
    active = int(r['active'] or 0)
    if active == 0:
        return None
    oldest = r['oldest_created']
    days = int((datetime.now(timezone.utc) - oldest).total_seconds() / 86400) if oldest else 0
    return (f"<b>{active}</b> active price watches · "
            f"<b>{r['never_matched']}</b> have never matched · "
            f"oldest is {days}d old")


def _waiting_ii_scrape(cur):
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE status='pending') AS pending,
               COUNT(*) FILTER (WHERE status='in_progress') AS in_progress,
               COUNT(*) FILTER (WHERE status='complete') AS complete,
               COUNT(*) FILTER (WHERE status='failed') AS failed
        FROM crab.ii_scrape_queue
    """)
    r = cur.fetchone()
    pending = int(r['pending'] or 0)
    if pending == 0 and r['in_progress'] == 0:
        return None
    return (f"II scrape queue: <b>{pending}</b> pending · "
            f"{r['in_progress']} in progress · {r['complete']} complete · "
            f"{r['failed']} failed")


def _waiting_plans(cur):
    cur.execute("""
        SELECT COUNT(*) AS open_plans
        FROM crab.plans
        WHERE status = 'open' OR status IS NULL
    """)
    r = cur.fetchone()
    n = int(r['open_plans'] or 0)
    if n == 0:
        return None
    return f"<b>{n}</b> open plans (members may be voting / contributing dates)"


def _waiting_leg_hunts(cur):
    cur.execute("""
        SELECT modality, COUNT(*) AS due
        FROM crab.leg_hunts
        WHERE last_hunted_at IS NULL OR last_hunted_at < NOW() - INTERVAL '24 hours'
        GROUP BY modality ORDER BY due DESC LIMIT 5
    """)
    rows = cur.fetchall()
    if not rows:
        return None
    parts = ', '.join(f"{r['modality']}={r['due']}" for r in rows)
    total = sum(int(r['due']) for r in rows)
    return f"<b>{total}</b> leg-hunts due (>24h since last): {parts}"


@bp.route('/cron/daily-heartbeat')
def cron_daily_heartbeat():
    """Daily heartbeat email — health check + waiting queue. 8am PT."""
    if not request.headers.get('X-Appengine-Cron') and request.args.get('force') != '1':
        return jsonify({'error': 'Cron only (append ?force=1 to test manually)'}), 403

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    health = []
    crons = _safe('crons', lambda: _check_crons(cur), conn=conn)
    if isinstance(crons, list):
        health.extend(crons)
    opencrab = _safe('opencrab', lambda: _check_opencrab(cur), conn=conn)
    if isinstance(opencrab, tuple):
        health.append(opencrab)
    health.append(_safe('db_pool', lambda: _check_db_pool(cur), conn=conn))
    health.append(_safe('llm', lambda: _check_llm_routing(cur), conn=conn))

    waiting = []
    for label, fn in [
        ('watches', _waiting_watches),
        ('ii_scrape', _waiting_ii_scrape),
        ('plans', _waiting_plans),
        ('leg_hunts', _waiting_leg_hunts),
    ]:
        v = _safe(label, lambda f=fn: f(cur), conn=conn)
        if isinstance(v, str):
            waiting.append(v)
        elif isinstance(v, dict) and '_error' in v:
            waiting.append(f"⚠️  {label} query failed: {v['_error']}")

    cur.close(); conn.close()

    # Render
    health_rows = []
    for item in health:
        if isinstance(item, tuple) and len(item) == 3:
            name, emoji, summary = item
            health_rows.append(f"<tr><td>{emoji}</td><td><b>{name}</b></td>"
                               f"<td>{summary}</td></tr>")
        elif isinstance(item, dict) and '_error' in item:
            health_rows.append(f"<tr><td>⚠️</td><td colspan=2>"
                               f"query failed: {item['_error']}</td></tr>")

    health_html = ('<table cellpadding=8 style="border-collapse:collapse;'
                   'border:1px solid #ccc;font-family:-apple-system,sans-serif;'
                   'font-size:14px;">' + ''.join(health_rows) + '</table>')

    if waiting:
        waiting_html = '<ul style="font-family:-apple-system,sans-serif;">' + \
                       ''.join(f'<li>{w}</li>' for w in waiting) + '</ul>'
    else:
        waiting_html = '<p><em>nothing pending — queues clear.</em></p>'

    # Status badge for subject line
    badge_counts = {'🔴': 0, '🟠': 0, '⚠️': 0, '✅': 0}
    for item in health:
        if isinstance(item, tuple):
            badge_counts[item[1]] = badge_counts.get(item[1], 0) + 1
    if badge_counts['🔴']:
        badge = f"🔴 {badge_counts['🔴']} down"
    elif badge_counts['⚠️']:
        badge = f"⚠️  {badge_counts['⚠️']} broken-check"
    elif badge_counts['🟠']:
        badge = f"🟠 {badge_counts['🟠']} stale"
    else:
        badge = '🟢 all green'

    today = datetime.now(PT).strftime('%a %b %-d')
    subject = f"[crab heartbeat] {today} — {badge}"

    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;
            font-size:14px;line-height:1.55;color:#0f172a;
            max-width:680px;margin:0;">
  <h2 style="margin:0 0 4px 0;">crab.travel daily heartbeat — {today}</h2>
  <p style="color:#64748b;margin:0 0 18px 0;">
    Exception report. If everything is ✅ you can stop reading here.
  </p>

  <h3 style="margin:18px 0 6px 0;">Is crab healthy?</h3>
  {health_html}

  <h3 style="margin:24px 0 6px 0;">Things we're waiting on</h3>
  {waiting_html}

  <hr style="margin-top:30px;border:none;border-top:1px solid #e2e8f0;">
  <p style="color:#94a3b8;font-size:12px;margin:8px 0 0 0;">
    /cron/daily-heartbeat · daily 8am PT · queries crab.bot_runs,
    deals_cache, member_watches, notifications_sent, ii_scrape_queue,
    plans, leg_hunts, kumori_llm_daily_caps. Per-plan OpenCrab test
    emails are folded into the OpenCrab line above.
  </p>
</div>
"""

    # Plain-text fallback for clients that don't render HTML
    plain = (
        f"crab.travel daily heartbeat — {today}\n\n"
        f"Status: {badge}\n"
        f"Health items: {len(health)} · Waiting items: {len(waiting)}\n\n"
        "Open the HTML version in Gmail for the full breakdown.\n"
        "/cron/daily-heartbeat\n"
    )
    sent = send_simple_email(subject=subject, body=plain, to_email=ADMIN,
                             from_name='crab.travel heartbeat', html=html)

    return jsonify({
        'sent': bool(sent),
        'badge': badge,
        'health_items': len(health),
        'waiting_items': len(waiting),
        'subject': subject,
    })
