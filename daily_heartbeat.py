"""Daily heartbeat for crab.travel — one email/day, exception-report shape.

Two sections:
  1. Is Crab Healthy? — ✅/⚠️/🔴 status of every cron, DB, LLM routing. Quick-skim line per item.
  2. Things We're Waiting On — active price watches, II scrape position,
     plans with pending state.
Each subsystem query is wrapped in try/except so a single missing table
or schema drift never silences the whole digest.

Wire: registered as `daily_heartbeat_bp` in app.py + cron.yaml entry at 8am PT.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

import psycopg2.extras
from flask import Blueprint, jsonify, request

from utilities.gmail_utils import send_simple_email
from utilities.postgres_utils import get_db_connection

logger = logging.getLogger(__name__)

bp = Blueprint('daily_heartbeat', __name__)

ADMIN = 'andy.tillo@gmail.com'
PT = timezone(timedelta(hours=-7))


def _norm_waiting(w):
    """Normalize a waiting-line for hashing: drop HTML, collapse plain counts to
    '#', but BAND any hour/day lag into coarse buckets so a watch cron going from
    'briefly late' to 'fully dead' still flips the hash and re-alerts."""
    s = re.sub(r'<[^>]+>', '', w)

    def _band(m):
        n = int(m.group(1))
        hours = n * 24 if m.group(2).lower().startswith('d') else n
        # Digit-free labels so the \d+ collapse below can't mangle or collide them
        # (LAG24h+ and LAG72h+ would both become LAG#h+ otherwise).
        if hours >= 168:
            return 'LAGdead'
        if hours >= 72:
            return 'LAGhigh'
        if hours >= 24:
            return 'LAGmed'
        return 'LAGlow'

    s = re.sub(r'(\d+)\s*(h|hr|hrs|hours?|d|days?)\b', _band, s, flags=re.I)
    s = re.sub(r'\d+', '#', s)
    return re.sub(r'\s+', ' ', s).strip()


def _state_hash(health, waiting):
    """Stable hash of the *meaningful* health state — status, not drifting counts.
    Health rows hash as name=emoji (a real ✅→🟠 flip changes it; a count drift
    does not). Waiting lines hash by category presence + a coarse lag band. This
    is what suppress-on-unchanged compares to decide whether to re-send."""
    parts = []
    for item in health:
        if isinstance(item, tuple) and len(item) == 3:
            parts.append(f"{item[0]}={item[1]}")
        elif isinstance(item, dict) and '_error' in item:
            parts.append('err')
    parts.sort()
    wsig = sorted({_norm_waiting(w) for w in waiting})
    blob = '|'.join(parts) + '||' + '|'.join(wsig)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


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
    """Return list of cron health rows. Each: (name, emoji, summary).

    NOTE: the /tasks/crawl (Crab Crawlers bot) check was removed 2026-06-04 —
    that cron was retired 2026-04-13 (commit f1164f2), so crab.bot_runs never
    gets new rows and the check fired a false 🔴 'no runs in 24h' every single
    day. That was the source of every red badge in the heartbeat inbox.
    """
    rows = []
    # /tasks/refresh-deals every 24h → expect deals_cache.last_seen_at within 26h
    cur.execute("""
        SELECT MAX(last_seen_at) AS last_refresh, COUNT(*) AS deals
        FROM crab.deals_cache
    """)
    r = cur.fetchone()
    last = r['last_refresh']
    age_h = int((datetime.now(timezone.utc) - last).total_seconds() / 3600) if last else None
    fresh = age_h is not None and age_h < 26
    summary = (f"last refresh {age_h}h ago, {r['deals']} deals cached"
               if last else "deals_cache empty")
    rows.append(('refresh-deals (every 24h)', _status_emoji(fresh), summary))

    return rows


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
        # Informational (ℹ️), NOT amber: with no active trips driving trip-AI on a
        # solo build, zero LLM calls is the EXPECTED state, not a fault — it should
        # not keep the status email permanently non-green. Re-promote to 🟠 if
        # trip-AI is ever expected to run continuously.
        return ('LLM routing', 'ℹ️', 'no crab_travel LLM calls in 7d — expected (trip-AI idle, no active trips)')
    top = ', '.join(f"{r['backend']}={r['calls']}" for r in rows[:3])
    total = sum(int(r['calls']) for r in rows)
    return ('LLM routing', '✅', f"{total} calls/7d — top: {top}")


def _waiting_watches(cur):
    cur.execute("""
        SELECT COUNT(*) AS active,
               COUNT(*) FILTER (WHERE last_checked_at IS NULL) AS never_checked,
               COUNT(*) FILTER (WHERE best_price_usd IS NULL) AS never_priced,
               MIN(last_checked_at) AS oldest_check
        FROM crab.member_watches
        WHERE status = 'active'
    """)
    r = cur.fetchone()
    active = int(r['active'] or 0)
    if active == 0:
        return None
    oldest = r['oldest_check']
    days = int((datetime.now(timezone.utc) - oldest).total_seconds() / 86400) if oldest else 0
    age_str = f"oldest checked {days}d ago" if oldest else "never checked yet"
    return (f"<b>{active}</b> active price watches · "
            f"<b>{r['never_priced']}</b> have never matched a price · "
            f"{age_str}")


def _waiting_alert_skips(cur):
    """Surface why the watch engine *didn't* send alerts in the last 24h.
    Each reason is a different class of quality issue; keeping them visible
    means adapter drift gets noticed instead of buried.
    """
    cur.execute("""
        SELECT reason, COUNT(*) AS n, MAX(skipped_at) AS most_recent
        FROM crab.watch_alert_skips
        WHERE skipped_at > NOW() - INTERVAL '24 hours'
        GROUP BY reason
        ORDER BY n DESC
    """)
    rows = cur.fetchall()
    if not rows:
        return None
    parts = [f"<b>{int(r['n'])}</b> × {r['reason']}" for r in rows]
    return "alert skips (24h): " + " · ".join(parts)


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
    # OpenClaw VPS retired 2026-09-21: its OpenCrab, hunter, flight-scanner and
    # watch-scan rows went with it.
    health.append(_safe('db_pool', lambda: _check_db_pool(cur), conn=conn))
    health.append(_safe('llm', lambda: _check_llm_routing(cur), conn=conn))

    waiting = []
    for label, fn in [
        ('watches', _waiting_watches),
        ('alert_skips', _waiting_alert_skips),
        ('ii_scrape', _waiting_ii_scrape),
        ('plans', _waiting_plans),
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
    subject = f"[crab.travel status] {today} — {badge}"

    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;
            font-size:14px;line-height:1.55;color:#0f172a;
            max-width:680px;margin:0;">
  <h2 style="margin:0 0 4px 0;">crab.travel status — {today}</h2>
  <p style="color:#64748b;margin:0 0 18px 0;">
    The one crab status email. Exception report — if everything is ✅ you can
    stop reading here. Silent when all-green; surfaces only when something breaks.
  </p>

  <h3 style="margin:18px 0 6px 0;">Is crab healthy?</h3>
  {health_html}

  <h3 style="margin:24px 0 6px 0;">Things we're waiting on</h3>
  {waiting_html}

  <hr style="margin-top:30px;border:none;border-top:1px solid #e2e8f0;">
  <p style="color:#94a3b8;font-size:12px;margin:8px 0 0 0;">
    /cron/daily-heartbeat · daily 8am PT, suppress-on-unchanged with a 7-day
    proof-of-life floor. Queries crab.deals_cache, member_watches, ii_scrape_queue,
    plans, kumori_llm_daily_caps.
  </p>
</div>
"""

    # Suppress-on-unchanged: only mail when the meaningful health state changes,
    # with a 7-day floor so an unchanged-but-healthy state still proves the cron
    # is alive. Kills the ~30 near-identical emails/month.
    from utilities.notification_utils import state_email_should_send, state_email_record
    new_hash = _state_hash(health, waiting)
    is_real_cron = bool(request.headers.get('X-Appengine-Cron'))
    should_send, why = state_email_should_send('daily_heartbeat', new_hash)

    # Plain-text fallback for clients that don't render HTML. Strip HTML tags
    # and unescape the few entities our summaries use (&gt; in the stale-watch line).
    def _txt(s):
        s = re.sub(r'<[^>]+>', '', str(s))
        return s.replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&')
    plain_lines = [f"crab.travel status — {today}", "", f"Status: {badge}", ""]
    for item in health:
        if isinstance(item, tuple) and len(item) == 3:
            plain_lines.append(f"{item[1]} {item[0]} — {_txt(item[2])}")
    if waiting:
        plain_lines += ["", "Waiting on:"] + [f"- {_txt(w)}" for w in waiting]
    plain_lines += ["", "Open the HTML version in Gmail for the formatted breakdown.",
                    "/cron/daily-heartbeat"]
    plain = "\n".join(plain_lines) + "\n"
    sent = False
    if should_send:
        sent = bool(send_simple_email(subject=subject, body=plain, to_email=ADMIN,
                                      from_name='crab.travel status', html=html))
        # Only a real cron advances the dead-man clock — a ?force=1 manual poke
        # must not mask a wedged cron by bumping sent_at.
        if sent and is_real_cron:
            state_email_record('daily_heartbeat', new_hash)

    return jsonify({
        'sent': sent,
        'suppressed': not should_send,
        'reason': why,
        'badge': badge,
        'state_hash': new_hash,
        'health_items': len(health),
        'waiting_items': len(waiting),
        'subject': subject,
    })
