"""OpenCrab notify endpoint.

Single, narrow server→server API for OpenCrab (VPS) to request that crab.travel
send an email/SMS to an admin (Andy) or a plan member. OpenCrab never sees
emails, phone numbers, credentials, or raw recipient fields.

Hardening:
- Bearer auth via CRAB_OPENCRAB_BEARER_TOKEN.
- Template whitelist — OpenCrab submits structured data; subject/body rendered here.
- Recipient resolved server-side:
    type='admin' → CRAB_OPENCRAB_ADMIN_RECIPIENT secret
    type='member' → crab.plan_members lookup by (plan_id, member_id)
- Test mode (ON by default until secret flipped): every outbound reroutes to
  admin with [→ would've gone to <real>] prefix.
- Rate limit: max 1 per plan per UTC day (notifications_sent table) and a
  hard global cap per UTC day so a runaway VPS can't flood the inbox.
"""
import logging
import re
from datetime import datetime

from flask import Blueprint, jsonify, request

from route_helpers import bearer_auth_required

logger = logging.getLogger(__name__)

bp = Blueprint('opencrab', __name__)

NOTIFICATION_TYPE = 'opencrab_digest'
GLOBAL_DAILY_CAP = 20
PER_PLAN_DAILY_CAP = 1

# Least-privilege caps for /watch-results — OpenCrab can only write under these.
WATCH_RESULTS_MAX_PAYLOAD_BYTES = 50_000
WATCH_RESULTS_MAX_PER_SOURCE = 20
WATCH_RESULTS_MAX_SOURCES = 12
WATCH_RESULTS_PER_WATCH_HOURLY_CAP = 12  # ~matches */5 min crawl cadence
WATCH_RESULT_ALLOWED_FIELDS = {
    'source', 'price_usd', 'airline', 'provider', 'name',
    'detail', 'stops', 'deep_link', 'url',
    'depart_at', 'arrive_at', 'checkin', 'checkout',
    'nights', 'rating',
}

# ─── Modality-agnostic hunting caps (for /transport-options) ───
TRANSPORT_MAX_PAYLOAD_BYTES = 100_000
TRANSPORT_MAX_OPTIONS_PER_POST = 200
TRANSPORT_PER_LEG_HOURLY_CAP = 60  # across all modalities/providers

# ─── Area-context caps (for /area-observations + /area-synthesis) ───
AREA_OBS_MAX_PAYLOAD_BYTES = 200_000    # ~200 KB — Reddit threads can be wordy
AREA_OBS_MAX_PER_POST = 100              # records/call max
AREA_OBS_HOURLY_CAP = 50                 # POSTs per hour per skill
AREA_OBS_ALLOWED_SOURCES = {'reddit', 'tug', 'youtube', 'news', 'tourism', 'climate'}
AREA_OBS_ALLOWED_AREAS = {'hawaii'}      # whitelist; expand as new areas come online
AREA_OBS_ALLOWED_FIELDS = {
    'area_slug', 'resort_pid', 'source', 'source_id', 'source_url', 'source_sub',
    'author', 'title', 'text', 'posted_at', 'score', 'raw_json', 'scraped_by',
}
AREA_OBS_MAX_TEXT_LEN = 20_000           # per-snippet text cap
AREA_SYN_MAX_PAYLOAD_BYTES = 50_000
ALLOWED_MODALITIES = {
    'flight', 'train', 'bus', 'drive', 'rental_car',
    'rideshare', 'ferry', 'transfer', 'bike', 'walk', 'multimodal',
}
TRANSPORT_OPTION_ALLOWED_FIELDS = {
    'modality', 'provider', 'external_id', 'price_usd', 'currency',
    'duration_minutes', 'transfers', 'depart_at', 'arrive_at',
    'summary', 'deep_link', 'data',
}

# Cadence tiers — controls how often each modality gets re-hunted.
# Crab decides what's due based on (leg, modality).last_hunted_at.
# OpenClaw just asks "what's due?" and obeys.
HUNT_CADENCES = {
    # cadence label: {modality: min_seconds_between_hunts}
    'fast':  {'flight': 300, 'rideshare': 300},                           # 5 min
    'warm':  {'flight': 3600, 'rideshare': 3600, 'transfer': 3600},       # 1 hr
    'slow':  {'train': 21600, 'bus': 21600, 'ferry': 21600,               # 6 hr
              'rental_car': 21600, 'multimodal': 21600,
              'drive': 86400, 'bike': 86400, 'walk': 86400},
    'daily': {m: 0 for m in [                                             # all, daily
        'flight', 'train', 'bus', 'drive', 'rental_car',
        'rideshare', 'ferry', 'transfer', 'bike', 'walk', 'multimodal',
    ]},
}
LEGS_TO_HUNT_MAX = 100


def _sanitize_transport_option(o):
    """Strip to allowlist; coerce + validate. Returns None if unusable."""
    if not isinstance(o, dict):
        return None
    clean = {k: v for k, v in o.items() if k in TRANSPORT_OPTION_ALLOWED_FIELDS}
    mod = str(clean.get('modality') or '').strip().lower()
    if mod not in ALLOWED_MODALITIES:
        return None
    clean['modality'] = mod
    prov = str(clean.get('provider') or '').strip()
    if not prov or len(prov) > 60:
        return None
    clean['provider'] = prov[:60]
    if 'external_id' in clean and clean['external_id'] is not None:
        clean['external_id'] = str(clean['external_id'])[:200]
    if 'price_usd' in clean and clean['price_usd'] is not None:
        try:
            p = float(clean['price_usd'])
        except (TypeError, ValueError):
            return None
        if p < 0 or p > 100_000:
            return None
        clean['price_usd'] = p
    for k in ('duration_minutes', 'transfers'):
        if k in clean and clean[k] is not None:
            try:
                clean[k] = int(clean[k])
            except (TypeError, ValueError):
                clean.pop(k, None)
    cur = str(clean.get('currency') or 'USD')[:3].upper()
    clean['currency'] = cur if len(cur) == 3 else 'USD'
    for k in ('summary',):
        if k in clean and clean[k] is not None:
            clean[k] = str(clean[k])[:400]
    url = clean.get('deep_link')
    if url is not None:
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')) or len(url) > 2000:
            clean['deep_link'] = None
    # data is a free jsonb bag but must be a dict and not huge
    d = clean.get('data')
    if d is None:
        clean['data'] = {}
    elif not isinstance(d, dict):
        clean['data'] = {}
    else:
        import json as _j
        if len(_j.dumps(d, default=str)) > 8000:
            clean['data'] = {}
    return clean


def _readonly_mode():
    """Kill switch: if CRAB_OPENCRAB_READONLY=='on', block all OpenCrab writes."""
    try:
        from utilities.google_auth_utils import get_secret
        v = (get_secret('CRAB_OPENCRAB_READONLY') or 'off').strip().lower()
        return v == 'on'
    except Exception:
        return False


def _sanitize_result(r):
    """Strip any field not on the allowlist. Returns None if required fields missing."""
    if not isinstance(r, dict):
        return None
    clean = {k: v for k, v in r.items() if k in WATCH_RESULT_ALLOWED_FIELDS}
    if 'price_usd' not in clean:
        return None
    try:
        clean['price_usd'] = float(clean['price_usd'])
    except (TypeError, ValueError):
        return None
    if clean['price_usd'] <= 0 or clean['price_usd'] > 100_000:
        return None
    url = clean.get('deep_link') or clean.get('url')
    if not url or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return None
    if len(url) > 2000:
        return None
    clean['source'] = str(clean.get('source') or 'unknown')[:40]
    for k in ('airline', 'provider', 'name', 'detail'):
        if k in clean and clean[k] is not None:
            clean[k] = str(clean[k])[:200]
    return clean

TEMPLATES = {
    'delta_digest': {
        'subject': '[crab.travel] {plan_title} — daily trip update',
        'body': (
            "Hey {recipient_name}!\n\n"
            "OpenCrab ran today's pass on your trip: {plan_title}.\n"
            "Trip date: {trip_date}  ({days_out} days out)\n\n"
            "{summary}\n\n"
            "Top options right now:\n{top_options}\n\n"
            "What changed since yesterday:\n{deltas}\n\n"
            "—\n"
            "View plan: https://crab.travel/plan/{plan_id}\n"
        ),
    },
    'plan_state_summary': {
        'subject': '[crab.travel] {plan_title} — state summary',
        'body': (
            "Hi {recipient_name},\n\n"
            "{summary}\n\n"
            "— crab.travel\n"
        ),
    },
}


def _get_admin_recipient():
    from utilities.google_auth_utils import get_secret
    return (get_secret('CRAB_OPENCRAB_ADMIN_RECIPIENT') or '').strip()


def _test_mode_enabled():
    """Default ON until an explicit override secret is set to 'off'.
    Values: 'on' (default — reroute member emails to admin),
            'off' (real members get real emails),
            'digest_only' (record to notifications_sent for the daily
             heartbeat to roll up, but skip the actual email send).
    """
    try:
        from utilities.google_auth_utils import get_secret
        v = (get_secret('CRAB_OPENCRAB_TEST_MODE') or 'on').strip().lower()
        return v != 'off'
    except Exception:
        return True


def _digest_only_mode():
    """True when CRAB_OPENCRAB_TEST_MODE='digest_only' — record the
    notification but suppress the email. Daily heartbeat picks it up."""
    try:
        from utilities.google_auth_utils import get_secret
        return (get_secret('CRAB_OPENCRAB_TEST_MODE') or '').strip().lower() == 'digest_only'
    except Exception:
        return False


def _resolve_member(plan_id, member_id):
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.pk_id, u.email, u.full_name
            FROM crab.plan_members m
            JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s::uuid AND u.pk_id = %s
            LIMIT 1
        """, (str(plan_id), member_id))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _plan_title(plan_id):
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT title FROM crab.plans WHERE plan_id = %s::uuid", (str(plan_id),))
        row = cur.fetchone()
        return row[0] if row else 'your trip'
    finally:
        conn.close()


def _counts_today(plan_id):
    """Return (global_today, plan_today) counts for opencrab_digest."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) AS global_today,
                COUNT(*) FILTER (WHERE plan_id = %s::uuid) AS plan_today
            FROM crab.notifications_sent
            WHERE notification_type = %s
              AND sent_at::date = NOW()::date
        """, (str(plan_id), NOTIFICATION_TYPE))
        row = cur.fetchone()
        return int(row[0] or 0), int(row[1] or 0)
    finally:
        conn.close()


def _record_sent(plan_id, user_id, channel):
    from utilities.postgres_utils import get_db_connection
    # Guard the column width (crab.notifications_sent.channel = varchar(32)). A label
    # longer than the column silently 500'd the whole /notify call — 'email_suppressed'
    # vs the old varchar(10) cost ~4 failed calls/day for weeks. Clamp so no future
    # channel literal can ever truncate again.
    channel = str(channel)[:32]
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.notifications_sent (plan_id, user_id, notification_type, channel)
            VALUES (%s::uuid, %s, %s, %s)
        """, (str(plan_id), user_id, NOTIFICATION_TYPE, channel))
        conn.commit()
    finally:
        conn.close()


@bp.route('/api/opencrab/notify', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_notify():
    from utilities.gmail_utils import send_simple_email

    body = request.get_json(silent=True) or {}
    plan_id = body.get('plan_id')
    template = body.get('template')
    data = body.get('data') or {}
    recipient_req = body.get('recipient') or {}

    if not plan_id or template not in TEMPLATES:
        return jsonify({'error': 'plan_id and valid template required',
                        'allowed_templates': list(TEMPLATES.keys())}), 400

    # Rate limits
    global_today, plan_today = _counts_today(plan_id)
    if global_today >= GLOBAL_DAILY_CAP:
        logger.warning(f"opencrab/notify: global daily cap {GLOBAL_DAILY_CAP} hit")
        return jsonify({'skipped': 'global_daily_cap', 'cap': GLOBAL_DAILY_CAP}), 200
    if plan_today >= PER_PLAN_DAILY_CAP:
        logger.info(f"opencrab/notify: plan {plan_id} daily cap hit")
        return jsonify({'skipped': 'plan_daily_cap'}), 200

    # Resolve intended recipient
    r_type = recipient_req.get('type')
    if r_type == 'admin':
        real_email = _get_admin_recipient()
        real_name = 'Andy'
        real_user_id = None
    elif r_type == 'member':
        m = _resolve_member(plan_id, recipient_req.get('member_id'))
        if not m:
            return jsonify({'error': 'member not found in plan'}), 404
        real_email = m['email']
        real_name = m['full_name'] or 'Traveler'
        real_user_id = m['pk_id']
    else:
        return jsonify({'error': "recipient.type must be 'admin' or 'member'"}), 400

    if not real_email:
        return jsonify({'error': 'no email resolved for recipient'}), 500

    # Test-mode override
    test_mode = _test_mode_enabled()
    admin_email = _get_admin_recipient()
    if test_mode and r_type == 'member':
        delivered_to = admin_email
        test_prefix = f"[TEST → would've gone to {real_name} <{real_email}>] "
    else:
        delivered_to = real_email
        test_prefix = ''

    # Render template server-side
    tpl = TEMPLATES[template]
    ctx = {
        'plan_id': plan_id,
        'plan_title': _plan_title(plan_id),
        'recipient_name': real_name,
        'trip_date': data.get('trip_date', 'TBD'),
        'days_out': data.get('days_out', '?'),
        'summary': data.get('summary', ''),
        'top_options': data.get('top_options', '(none)'),
        'deltas': data.get('deltas', '(no change)'),
    }
    try:
        subject = test_prefix + tpl['subject'].format(**ctx)
        mail_body = tpl['body'].format(**ctx)
    except KeyError as e:
        return jsonify({'error': f'template render missing field: {e}'}), 400

    ok = False
    digest_only = _digest_only_mode()
    if digest_only:
        # Suppress email but record the notification so the daily heartbeat
        # can roll it up as one line ("OpenCrab: N notifications recorded
        # across M plans"). Replaces the old per-plan test-email noise.
        logger.info(f"opencrab/notify[digest_only]: would have emailed {delivered_to} "
                    f"with subject {subject!r}")
        _record_sent(plan_id, real_user_id, 'email_suppressed')
        return jsonify({
            'ok': True,
            'mode': 'digest_only',
            'would_have_delivered_to': delivered_to,
            'intended_recipient': real_email if r_type == 'member' else 'admin',
            'template': template,
        }), 200

    try:
        ok = bool(send_simple_email(subject, mail_body, delivered_to))
    except Exception as e:
        logger.error(f"opencrab/notify send failed: {e}")
        return jsonify({'error': 'send failed', 'detail': str(e)}), 500

    if ok:
        # Record under the REAL recipient's user_id (or admin fallback) so the
        # per-plan daily cap is accurate.
        _record_sent(plan_id, real_user_id, 'email')

    return jsonify({
        'ok': ok,
        'test_mode': test_mode,
        'delivered_to': delivered_to,
        'intended_recipient': real_email if r_type == 'member' else 'admin',
        'template': template,
        'global_today': global_today + (1 if ok else 0),
        'plan_today': plan_today + (1 if ok else 0),
    }), 200


@bp.route('/api/openclaw/admin-digest', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def openclaw_admin_digest():
    """Send one preformatted digest email from the OpenClaw VPS to admin.

    Narrow by design: only the admin recipient (CRAB_OPENCRAB_ADMIN_RECIPIENT),
    plain-text body, no template rendering, no member lookup. Subject and body
    are accepted as-is from the VPS — this is the ONE email path the VPS can
    trigger that bypasses the per-plan dedup logic, scoped to once-daily ops
    summaries. Caller is responsible for not abusing it (cron runs once a day).
    """
    # RETIRED 2026-06-04 — the standalone 'OpenClaw daily' email was consolidated
    # into the daily heartbeat. The heartbeat now reads kumori_ops.openclaw_runs
    # directly and surfaces hunter errors as its 'OpenClaw VPS' health row, so
    # this VPS-pushed digest is redundant. The VPS cron may still POST here; we
    # ack it as a no-op. Flip CRAB_OPENCLAW_DIGEST_EMAIL='on' to revive the send.
    from utilities.google_auth_utils import get_secret
    if (get_secret('CRAB_OPENCLAW_DIGEST_EMAIL') or 'off').strip().lower() != 'on':
        return jsonify({'ok': True, 'emailed': False,
                        'suppressed': 'consolidated_into_heartbeat'}), 200

    from utilities.gmail_utils import send_simple_email
    body_in = request.get_json(silent=True) or {}
    subject = (body_in.get('subject') or '').strip()
    body = body_in.get('body') or ''
    if not subject or not body:
        return jsonify({'error': 'subject and body required'}), 400
    if len(subject) > 250:
        subject = subject[:247] + '...'
    if len(body) > 50_000:
        return jsonify({'error': 'body too large (>50KB)'}), 400
    admin = _get_admin_recipient()
    if not admin:
        return jsonify({'error': 'CRAB_OPENCRAB_ADMIN_RECIPIENT not set'}), 500

    # Suppress-on-unchanged: the VPS POSTs a full digest daily, but most days the
    # hunter health is identical and only the counts drift. Hash the normalized
    # subject+body (digits/whitespace collapsed) and re-send only when the content
    # actually changes (a hunter goes silent, a new error appears) or a 7-day floor
    # elapses. Preserves silent-hunter detection; kills the daily-identical email.
    import hashlib as _hl
    norm = re.sub(r'\s+', ' ', re.sub(r'\d+', '#', f"{subject}\n{body}")).strip().lower()
    digest_hash = _hl.sha256(norm.encode('utf-8')).hexdigest()
    from utilities.notification_utils import state_email_should_send, state_email_record
    should_send, why = state_email_should_send('openclaw_admin_digest', digest_hash)
    if not should_send:
        logger.info(f"openclaw/admin-digest: suppressed unchanged ({why})")
        return jsonify({'ok': True, 'emailed': False, 'suppressed': True,
                        'reason': why, 'delivered_to': admin}), 200
    try:
        ok = bool(send_simple_email(subject, body, admin))
    except Exception as e:
        logger.error(f"openclaw/admin-digest send failed: {e}")
        return jsonify({'error': 'send failed', 'detail': str(e)}), 500
    if ok:
        state_email_record('openclaw_admin_digest', digest_hash)
    return jsonify({'ok': ok, 'emailed': ok, 'reason': why, 'delivered_to': admin}), 200


@bp.route('/api/opencrab/plans-eligible', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_plans_eligible():
    """Return plans eligible for today's OpenCrab pass.

    Criteria: plan has at least one flight watch, trip is in the future and
    within max_days_out, and (for this test phase) the plan was bot-seeded.
    Server-side filter only — OpenCrab never gets raw member PII.
    """
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    body = request.get_json(silent=True) or {}
    max_days_out = int(body.get('max_days_out', 120))
    limit = min(int(body.get('limit', 40)), 100)

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.plan_id, p.title, p.invite_token,
                   MIN(w.checkin) AS trip_date,
                   (MIN(w.checkin) - CURRENT_DATE) AS days_out,
                   json_agg(json_build_object(
                       'watch_id', w.pk_id,
                       'origin', w.origin,
                       'destination', w.destination,
                       'checkin', w.checkin,
                       'checkout', w.checkout,
                       'last_price_usd', w.last_price_usd,
                       'best_price_usd', w.best_price_usd,
                       'last_checked_at', w.last_checked_at,
                       'deep_link', w.deep_link
                   ) ORDER BY w.last_price_usd NULLS LAST) AS flight_watches
            FROM crab.plans p
            JOIN crab.member_watches w ON w.plan_id = p.plan_id
            WHERE w.watch_type = 'flight'
              AND w.checkin >= CURRENT_DATE
              AND w.checkin <= CURRENT_DATE + (%s || ' days')::interval
              AND COALESCE(p.status, '') <> 'booked'
              AND p.title NOT LIKE '[BOT]%%'
            GROUP BY p.plan_id, p.title, p.invite_token
            ORDER BY MIN(w.checkin) ASC
            LIMIT %s
        """, (max_days_out, limit))
        plans = []
        for row in cur.fetchall():
            plans.append({
                'plan_id': str(row['plan_id']),
                'plan_title': row['title'] or 'Untitled trip',
                'invite_token': row.get('invite_token'),
                'trip_date': row['trip_date'].isoformat() if row['trip_date'] else None,
                'days_out': int(row['days_out']) if row['days_out'] is not None else None,
                'flight_watches': row['flight_watches'] or [],
            })
        return jsonify({'plans': plans, 'count': len(plans)}), 200
    finally:
        conn.close()


@bp.route('/api/opencrab/watch-results', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_watch_results():
    """VPS writes normalized per-source crawl results for an EXISTING watch.

    Least-privilege contract:
      - UPDATE only (never INSERT/DELETE). Watch must already exist.
      - Only these columns mutate: data->'opencrab_results', data->'updated_at',
        last_price_usd, deep_link, last_checked_at.
      - Never touches member_id, plan_id, watch_type, origin, destination, dates.
      - Payload capped, per-source results capped, write rate capped.
      - Writes can be frozen globally via CRAB_OPENCRAB_READONLY=on.
    """
    if _readonly_mode():
        return jsonify({'error': 'opencrab writes frozen (CRAB_OPENCRAB_READONLY=on)'}), 503

    if (request.content_length or 0) > WATCH_RESULTS_MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload too large',
                        'max_bytes': WATCH_RESULTS_MAX_PAYLOAD_BYTES}), 413

    body = request.get_json(silent=True) or {}
    watch_id = body.get('watch_id')
    results_in = body.get('results') or []
    if not isinstance(watch_id, int) or watch_id <= 0:
        return jsonify({'error': 'watch_id (positive int) required'}), 400
    if not isinstance(results_in, list):
        return jsonify({'error': 'results must be a list'}), 400

    # Sanitize + group by source, cap per-source and total-sources
    by_source = {}
    dropped = 0
    for item in results_in:
        clean = _sanitize_result(item)
        if not clean:
            dropped += 1
            continue
        src = clean['source']
        lst = by_source.setdefault(src, [])
        if len(lst) < WATCH_RESULTS_MAX_PER_SOURCE:
            lst.append(clean)
        else:
            dropped += 1
    if len(by_source) > WATCH_RESULTS_MAX_SOURCES:
        # Drop extra sources alphabetically — deterministic, not OpenCrab-controlled
        extras = sorted(by_source.keys())[WATCH_RESULTS_MAX_SOURCES:]
        for s in extras:
            dropped += len(by_source.pop(s))

    flat = []
    for src, rows in by_source.items():
        rows.sort(key=lambda r: r['price_usd'])
        flat.extend(rows)
    flat.sort(key=lambda r: r['price_usd'])

    cheapest = flat[0] if flat else None

    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Per-watch hourly write cap — prevents a runaway VPS from spamming jsonb
        cur.execute("""
            SELECT pk_id, plan_id,
                   (data->>'opencrab_write_count_hour')::int AS wc,
                   (data->>'opencrab_write_window_start')::timestamptz AS wstart
            FROM crab.member_watches WHERE pk_id = %s
        """, (watch_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'watch not found'}), 404
        _, plan_id_for_watch, wc, wstart = row

        import json as _json
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        if wstart is None or (now - wstart) > timedelta(hours=1):
            wc = 0
            wstart = now
        if (wc or 0) >= WATCH_RESULTS_PER_WATCH_HOURLY_CAP:
            return jsonify({
                'error': 'per-watch hourly write cap hit',
                'cap': WATCH_RESULTS_PER_WATCH_HOURLY_CAP,
                'window_resets_at': (wstart + timedelta(hours=1)).isoformat(),
            }), 429

        merge = {
            'opencrab_results': flat,
            'opencrab_updated_at': now.isoformat(),
            'opencrab_write_count_hour': (wc or 0) + 1,
            'opencrab_write_window_start': wstart.isoformat(),
        }

        # Named-column UPDATE only. No dynamic SQL, no way to set other fields.
        if cheapest:
            cur.execute("""
                UPDATE crab.member_watches
                SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                    last_price_usd = %s,
                    deep_link = COALESCE(%s, deep_link),
                    last_checked_at = %s
                WHERE pk_id = %s
            """, (_json.dumps(merge), cheapest['price_usd'],
                  cheapest.get('deep_link') or cheapest.get('url'),
                  now, watch_id))
        else:
            cur.execute("""
                UPDATE crab.member_watches
                SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                    last_checked_at = %s
                WHERE pk_id = %s
            """, (_json.dumps(merge), now, watch_id))

        # Append to watch_history in a savepoint — its failure must never
        # poison the main UPDATE transaction.
        if cheapest:
            try:
                cur.execute("SAVEPOINT hist")
                cur.execute("""
                    INSERT INTO crab.watch_history (watch_id, price_usd, source, deep_link, data)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                """, (watch_id, cheapest['price_usd'], cheapest.get('source', 'opencrab'),
                      cheapest.get('deep_link') or cheapest.get('url'),
                      _json.dumps(cheapest)))
                cur.execute("RELEASE SAVEPOINT hist")
            except Exception as e:
                logger.debug(f"watch_history insert skipped: {e}")
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT hist")
                except Exception:
                    pass

        conn.commit()
    finally:
        conn.close()

    return jsonify({
        'ok': True,
        'watch_id': watch_id,
        'plan_id': str(plan_id_for_watch),
        'accepted': len(flat),
        'sources': len(by_source),
        'dropped': dropped,
        'cheapest_price_usd': cheapest['price_usd'] if cheapest else None,
    }), 200


@bp.route('/api/opencrab/transport-options', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_transport_options():
    """VPS writes hunted A→B options for a (leg, modality, provider) combo.

    Least-privilege contract — same guardrails as /watch-results, extended for
    the modality-agnostic schema:
      - UPSERT only into crab.transport_options. Never touches trip_legs ownership
        fields (plan_id, member_id, origin/destination/dates).
      - UNIQUE key (leg_id, modality, provider, external_id) → idempotent replay.
      - Options in payload for (leg, modality, provider) replace prior rows: any
        existing option NOT in this payload gets is_stale=TRUE (never deleted —
        historical prices stay queryable).
      - price_history auto-appends on price change.
      - Payload + per-post option count + per-leg hourly write cap.
      - Frozen globally via CRAB_OPENCRAB_READONLY=on.
    """
    if _readonly_mode():
        return jsonify({'error': 'opencrab writes frozen (CRAB_OPENCRAB_READONLY=on)'}), 503

    if (request.content_length or 0) > TRANSPORT_MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload too large',
                        'max_bytes': TRANSPORT_MAX_PAYLOAD_BYTES}), 413

    body = request.get_json(silent=True) or {}
    leg_id = body.get('leg_id')
    modality = (body.get('modality') or '').strip().lower()
    provider = (body.get('provider') or '').strip()
    options_in = body.get('options') or []

    if not isinstance(leg_id, int) or leg_id <= 0:
        return jsonify({'error': 'leg_id (positive int) required'}), 400
    if modality not in ALLOWED_MODALITIES:
        return jsonify({'error': 'modality not allowed', 'allowed': sorted(ALLOWED_MODALITIES)}), 400
    if not provider or len(provider) > 60:
        return jsonify({'error': 'provider required (<=60 chars)'}), 400
    if not isinstance(options_in, list):
        return jsonify({'error': 'options must be a list'}), 400
    if len(options_in) > TRANSPORT_MAX_OPTIONS_PER_POST:
        return jsonify({'error': 'too many options',
                        'max': TRANSPORT_MAX_OPTIONS_PER_POST,
                        'got': len(options_in)}), 413

    # Force every option's modality+provider to match the header — OpenClaw
    # can't smuggle in other modalities via the batch body.
    cleaned = []
    dropped = 0
    for raw in options_in:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        raw = {**raw, 'modality': modality, 'provider': provider}
        c = _sanitize_transport_option(raw)
        if c is None:
            dropped += 1
            continue
        cleaned.append(c)

    from utilities.postgres_utils import get_db_connection
    import json as _json
    from datetime import datetime, timezone, timedelta

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Validate leg exists + fetch plan_id for response
        cur.execute("""
            SELECT pk_id, plan_id, status FROM crab.trip_legs WHERE pk_id = %s
        """, (leg_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'leg not found'}), 404
        _, plan_id_for_leg, leg_status = row

        # Per-leg hourly rate cap (across all modalities/providers)
        cur.execute("""
            SELECT COUNT(*) FROM crab.leg_hunts
            WHERE leg_id = %s AND last_hunted_at > NOW() - INTERVAL '1 hour'
        """, (leg_id,))
        recent_hunts = int(cur.fetchone()[0] or 0)
        if recent_hunts >= TRANSPORT_PER_LEG_HOURLY_CAP:
            return jsonify({
                'error': 'per-leg hourly hunt cap hit',
                'cap': TRANSPORT_PER_LEG_HOURLY_CAP,
            }), 429

        now = datetime.now(timezone.utc)
        inserted = updated = unchanged = 0

        # Existing rows for this (leg, modality, provider) — used for delta +
        # staleness of anything not in this payload.
        cur.execute("""
            SELECT pk_id, COALESCE(external_id, ''), price_usd, price_history
            FROM crab.transport_options
            WHERE leg_id = %s AND modality = %s AND provider = %s
        """, (leg_id, modality, provider))
        existing_by_ext = {r[1]: {'pk_id': r[0], 'price_usd': r[2], 'history': r[3] or []}
                           for r in cur.fetchall()}

        seen_ext_ids = set()
        for o in cleaned:
            ext = o.get('external_id') or ''
            seen_ext_ids.add(ext)
            price = o.get('price_usd')
            prev = existing_by_ext.get(ext)

            # Compute price_history delta — append if price changed (or first observation with a price).
            new_history_entry = None
            if price is not None:
                if prev is None:
                    new_history_entry = {'at': now.isoformat(), 'price_usd': price}
                elif prev['price_usd'] is None or float(prev['price_usd']) != price:
                    new_history_entry = {'at': now.isoformat(), 'price_usd': price}

            cur.execute("""
                INSERT INTO crab.transport_options
                    (leg_id, modality, provider, external_id, price_usd, currency,
                     duration_minutes, transfers, depart_at, arrive_at,
                     summary, deep_link, data,
                     first_seen_at, last_seen_at, last_price_usd,
                     price_history, is_stale)
                VALUES (%s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s::jsonb,
                        %s, %s, NULL,
                        %s::jsonb, FALSE)
                ON CONFLICT (leg_id, modality, provider, COALESCE(external_id, ''))
                DO UPDATE SET
                    price_usd       = EXCLUDED.price_usd,
                    currency        = EXCLUDED.currency,
                    duration_minutes= EXCLUDED.duration_minutes,
                    transfers       = EXCLUDED.transfers,
                    depart_at       = EXCLUDED.depart_at,
                    arrive_at       = EXCLUDED.arrive_at,
                    summary         = EXCLUDED.summary,
                    deep_link       = EXCLUDED.deep_link,
                    data            = EXCLUDED.data,
                    last_seen_at    = EXCLUDED.last_seen_at,
                    last_price_usd  = crab.transport_options.price_usd,
                    price_history   = CASE
                        WHEN %s::jsonb IS NULL THEN crab.transport_options.price_history
                        ELSE crab.transport_options.price_history || %s::jsonb
                    END,
                    is_stale        = FALSE
                RETURNING (xmax = 0) AS inserted
            """, (
                leg_id, modality, provider, o.get('external_id'),
                price, o.get('currency', 'USD'),
                o.get('duration_minutes'), o.get('transfers'),
                o.get('depart_at'), o.get('arrive_at'),
                o.get('summary'), o.get('deep_link'),
                _json.dumps(o.get('data') or {}),
                now, now,
                _json.dumps([new_history_entry] if new_history_entry else []),
                # ON CONFLICT path history-append args:
                _json.dumps([new_history_entry]) if new_history_entry else None,
                _json.dumps([new_history_entry]) if new_history_entry else None,
            ))
            was_insert = cur.fetchone()[0]
            if was_insert:
                inserted += 1
            elif new_history_entry is not None:
                updated += 1
            else:
                unchanged += 1

        # Mark options not in this payload as stale for this (leg, modality, provider)
        stale_marked = 0
        if existing_by_ext:
            to_stale = [ext for ext in existing_by_ext.keys() if ext not in seen_ext_ids]
            if to_stale:
                cur.execute("""
                    UPDATE crab.transport_options
                    SET is_stale = TRUE
                    WHERE leg_id = %s AND modality = %s AND provider = %s
                      AND COALESCE(external_id, '') = ANY(%s)
                """, (leg_id, modality, provider, to_stale))
                stale_marked = cur.rowcount

        # Update leg_hunts tracker
        cur.execute("""
            INSERT INTO crab.leg_hunts (leg_id, modality, provider, last_hunted_at, last_ok_at, hunt_count)
            VALUES (%s, %s, %s, %s, %s, 1)
            ON CONFLICT (leg_id, modality, COALESCE(provider, ''))
            DO UPDATE SET
                last_hunted_at = EXCLUDED.last_hunted_at,
                last_ok_at     = EXCLUDED.last_ok_at,
                last_error     = NULL,
                hunt_count     = crab.leg_hunts.hunt_count + 1
        """, (leg_id, modality, provider, now, now))

        # Bump trip_legs.last_hunted_at + baselined_at
        cur.execute("""
            UPDATE crab.trip_legs
            SET last_hunted_at = %s,
                baselined_at   = COALESCE(baselined_at, %s),
                updated_at     = %s
            WHERE pk_id = %s
        """, (now, now, now, leg_id))

        conn.commit()
    finally:
        conn.close()

    return jsonify({
        'ok': True,
        'leg_id': leg_id,
        'plan_id': str(plan_id_for_leg),
        'modality': modality,
        'provider': provider,
        'accepted': len(cleaned),
        'dropped': dropped,
        'inserted': inserted,
        'updated': updated,
        'unchanged': unchanged,
        'stale_marked': stale_marked,
    }), 200


@bp.route('/api/opencrab/legs-to-hunt', methods=['GET'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_legs_to_hunt():
    """Discovery endpoint — returns which legs + modalities are due to hunt.

    OpenClaw asks, crab decides. OpenClaw cannot see or pick legs that
    aren't active, and cannot pick modalities that aren't due per the
    cadence rules here. Rate governance is enforced by omission.

    Query params:
        cadence  'fast' | 'warm' | 'slow' | 'daily'  (default 'daily')
        limit    int, default 40, max LEGS_TO_HUNT_MAX
    """
    cadence = (request.args.get('cadence') or 'daily').strip().lower()
    if cadence not in HUNT_CADENCES:
        return jsonify({'error': 'unknown cadence',
                        'allowed': list(HUNT_CADENCES.keys())}), 400
    rules = HUNT_CADENCES[cadence]
    try:
        limit = max(1, min(int(request.args.get('limit') or 40), LEGS_TO_HUNT_MAX))
    except (TypeError, ValueError):
        limit = 40

    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Only active legs where trip date is still in the future (or unset),
        # and the parent plan isn't a [BOT] demo plan. Bot legs are mostly
        # synthetic routes the OpenClaw adapters can't price; they starve real
        # legs behind the LIMIT and produced "0 new options on every run".
        cur.execute("""
            SELECT l.pk_id, l.plan_id, l.member_id,
                   l.origin, l.origin_kind, l.destination, l.destination_kind,
                   l.depart_window_start, l.depart_window_end, l.pax,
                   l.baselined_at, l.last_hunted_at, l.source_watch_id
            FROM crab.trip_legs l
            JOIN crab.plans p ON p.plan_id = l.plan_id
            WHERE l.status = 'active'
              AND p.title NOT LIKE '[BOT]%%'
              AND (l.depart_window_start IS NULL
                   OR l.depart_window_start >= CURRENT_DATE)
            ORDER BY l.last_hunted_at NULLS FIRST, l.pk_id
            LIMIT %s
        """, (limit,))
        legs = cur.fetchall()

        out = []
        for (leg_id, plan_id, member_id, origin, origin_kind, destination,
             destination_kind, dstart, dend, pax,
             baselined_at, last_hunted_at, source_watch_id) in legs:

            # Per-modality due check via leg_hunts
            cur.execute("""
                SELECT modality, last_hunted_at
                FROM crab.leg_hunts
                WHERE leg_id = %s
            """, (leg_id,))
            last_by_mod = {m: lh for (m, lh) in cur.fetchall()}

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            due = []
            for mod, min_secs in rules.items():
                lh = last_by_mod.get(mod)
                if lh is None or (now - lh).total_seconds() >= min_secs:
                    due.append(mod)
            if not due:
                continue

            out.append({
                'leg_id': leg_id,
                'plan_id': str(plan_id),
                'member_id': member_id,
                'origin': origin,
                'origin_kind': origin_kind,
                'destination': destination,
                'destination_kind': destination_kind,
                'depart_window_start': dstart.isoformat() if dstart else None,
                'depart_window_end': dend.isoformat() if dend else None,
                'pax': pax,
                'source_watch_id': source_watch_id,
                'baselined': baselined_at is not None,
                'due_modalities': sorted(due),
            })
    finally:
        conn.close()

    return jsonify({
        'cadence': cadence,
        'count': len(out),
        'legs': out,
        'allowed_modalities': sorted(ALLOWED_MODALITIES),
    }), 200


@bp.route('/api/opencrab/status', methods=['GET'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_status():
    """Lets VPS check caps + test-mode before a run."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM crab.notifications_sent
            WHERE notification_type = %s AND sent_at::date = NOW()::date
        """, (NOTIFICATION_TYPE,))
        global_today = int(cur.fetchone()[0] or 0)
    finally:
        conn.close()
    return jsonify({
        'test_mode': _test_mode_enabled(),
        'readonly': _readonly_mode(),
        'global_today': global_today,
        'global_daily_cap': GLOBAL_DAILY_CAP,
        'per_plan_daily_cap': PER_PLAN_DAILY_CAP,
        'allowed_templates': list(TEMPLATES.keys()),
        'watch_results_caps': {
            'max_payload_bytes': WATCH_RESULTS_MAX_PAYLOAD_BYTES,
            'max_per_source': WATCH_RESULTS_MAX_PER_SOURCE,
            'max_sources': WATCH_RESULTS_MAX_SOURCES,
            'per_watch_hourly_cap': WATCH_RESULTS_PER_WATCH_HOURLY_CAP,
        },
        'transport_options_caps': {
            'max_payload_bytes': TRANSPORT_MAX_PAYLOAD_BYTES,
            'max_options_per_post': TRANSPORT_MAX_OPTIONS_PER_POST,
            'per_leg_hourly_cap': TRANSPORT_PER_LEG_HOURLY_CAP,
            'allowed_modalities': sorted(ALLOWED_MODALITIES),
        },
        'hunt_cadences': {k: sorted(v.keys()) for k, v in HUNT_CADENCES.items()},
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }), 200


# ─── Member-watch scan, OpenClaw side ────────────────────────────────────────
# Two endpoints power the cutover from App Engine sync cron to OpenClaw-hosted
# scanning (per the discovery-vs-decision rule). Same guardrails as the rest
# of /api/opencrab/* plus extras for adversarial-VPS safety. See README of
# changes in this commit for the threat model.

# Cadence: OpenClaw cron is every 30min. Watches scanned within the last
# WATCH_SCAN_CADENCE_MIN minutes are omitted from the discovery list.
WATCH_SCAN_CADENCE_MIN = 25

# Observation-endpoint guardrails.
WATCH_OBS_MAX_PAYLOAD_BYTES = 50 * 1024        # 50 KB
WATCH_OBS_MAX_PER_POST = 100                   # observations per request
WATCH_OBS_PER_WATCH_SOURCE_RATE_MIN = 10       # min between obs for (watch, source)
WATCH_OBS_PRICE_FLOOR_USD = 20
WATCH_OBS_PRICE_CEILING_FLIGHT = 20_000
WATCH_OBS_PRICE_CEILING_HOTEL = 5_000

# Source names from OpenClaw must start with this prefix so audit can
# separate VPS-origin observations from anything written server-side.
WATCH_OBS_SOURCE_PREFIX = 'openclaw_'

# Allowed top-level keys per observation. Anything else → 400.
_WATCH_OBS_KEYS = {
    'watch_id', 'source', 'price_usd',
    'deep_link', 'deep_link_honors_price', 'data',
}

# Allowed top-level keys for the scan-travelpayouts proxy request body.
_SCAN_TP_KEYS = {
    'watch_id', 'watch_type', 'origin', 'destination', 'checkin', 'checkout',
}


@bp.route('/api/opencrab/watches-to-scan', methods=['GET'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_watches_to_scan():
    """Discovery — return active watches due for scanning.

    Strips ALL user-identifying fields: no user_id, plan_id, member_id, email,
    phone, name, budget. OpenClaw receives only the route + dates + the
    watch_id (which is meaningless outside this DB) so it can scrape and
    write back via /watch-observations.

    Rate governance: watches scanned in the last WATCH_SCAN_CADENCE_MIN
    minutes are omitted — manifest §17 says rate caps are enforced by
    "omission from the work list" not by checking on the OpenClaw side.
    """
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 200))
    except (TypeError, ValueError):
        limit = 50

    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Pick + claim in one transaction. UPDATE...RETURNING bumps
        # last_checked_at so the same watches don't re-queue next cron run
        # even when downstream scrapes return 0 observations (e.g.,
        # un-priceable destinations like "Scottsdale AZ" with no airport).
        # This converts the column's meaning from "last priced" to "last
        # dispatched for scan" — the actual price write still updates it
        # via record_watch_scan, which is the same UPDATE, so no conflict.
        # Exclude watches attached to [BOT] demo plans — they pollute the
        # rotation, mostly have unscannable destinations, and starve real-user
        # watches behind a 40-row LIMIT. Bot plans stay visible on /live but
        # don't burn OpenClaw cycles.
        cur.execute("""
            WITH due AS (
                SELECT mw.pk_id FROM crab.member_watches mw
                JOIN crab.plans p ON p.plan_id = mw.plan_id
                WHERE mw.status = 'active'
                  AND p.title NOT LIKE '[BOT]%%'
                  AND mw.checkin IS NOT NULL
                  AND mw.checkin >= CURRENT_DATE
                  AND mw.checkin <= CURRENT_DATE + INTERVAL '60 days'
                  AND (mw.last_checked_at IS NULL
                       OR mw.last_checked_at < NOW() - %s::interval)
                ORDER BY mw.last_checked_at NULLS FIRST, mw.pk_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE crab.member_watches mw
            SET last_checked_at = NOW()
            FROM due
            WHERE mw.pk_id = due.pk_id
            RETURNING mw.pk_id, mw.watch_type, mw.origin, mw.destination,
                      mw.checkin, mw.checkout, mw.last_checked_at
        """, (f'{WATCH_SCAN_CADENCE_MIN} minutes', limit))
        rows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        'count': len(rows),
        'cadence_minutes': WATCH_SCAN_CADENCE_MIN,
        'watches': [
            {
                'watch_id': r['pk_id'],
                'watch_type': r['watch_type'],
                'origin': r['origin'],
                'destination': r['destination'],
                'checkin': r['checkin'].isoformat() if r['checkin'] else None,
                'checkout': r['checkout'].isoformat() if r['checkout'] else None,
                'last_observed_at': (r['last_checked_at'].isoformat()
                                     if r['last_checked_at'] else None),
            }
            for r in rows
        ],
        'allowed_sources_prefix': WATCH_OBS_SOURCE_PREFIX,
    }), 200


@bp.route('/api/opencrab/watch-observations', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_watch_observations():
    """Batch insert OpenClaw-scraped quotes into crab.watch_history.

    Guardrails (manifest §17 + adversarial extras):
      1. Field allowlist — rejects unknown keys per observation
      2. Payload cap — WATCH_OBS_MAX_PER_POST observations, 50KB body
      3. Per-(watch, source) rate cap — refuses if obs <10min ago
      4. Named-column INSERT (record_watch_scan uses bound params)
      5. Kill switch — env CRAB_OPENCRAB_READONLY=on returns 503
      6. Price sanity floor + ceiling
      7. Active-watch check

    Does NOT touch:
      - crab.watch_alerts (alert send ledger — App Engine cron only)
      - crab.watch_alert_skips (decision-side log)
      - any other table
      - any email/SMS path
    """
    import os
    if os.environ.get('CRAB_OPENCRAB_READONLY', '').lower() == 'on':
        return jsonify({'error': 'opencrab writes frozen (CRAB_OPENCRAB_READONLY=on)'}), 503

    # Size check via Content-Length first (cheap, doesn't consume the stream).
    # Fall back to len(get_data(cache=True)) only when header missing.
    cl = request.content_length
    if cl is not None and cl > WATCH_OBS_MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload too large',
                        'max_bytes': WATCH_OBS_MAX_PAYLOAD_BYTES}), 413

    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({'error': f'invalid json: {e}'}), 400

    # Defensive: re-check size from parsed body in case Content-Length was absent.
    if cl is None and len(request.get_data(cache=True)) > WATCH_OBS_MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload too large',
                        'max_bytes': WATCH_OBS_MAX_PAYLOAD_BYTES}), 413

    observations = body.get('observations') or []
    if not isinstance(observations, list):
        return jsonify({'error': "'observations' must be a list"}), 400
    if not observations:
        return jsonify({'error': 'empty observations list'}), 400
    if len(observations) > WATCH_OBS_MAX_PER_POST:
        return jsonify({'error': 'too many observations',
                        'max_per_post': WATCH_OBS_MAX_PER_POST}), 400

    # ── Validate every observation up-front, no partial inserts ──
    cleaned = []
    rejections = []
    for i, obs in enumerate(observations):
        if not isinstance(obs, dict):
            rejections.append({'index': i, 'reason': 'not a dict'})
            continue
        extra = set(obs.keys()) - _WATCH_OBS_KEYS
        if extra:
            rejections.append({'index': i, 'reason': 'unknown keys',
                               'unknown': sorted(extra)})
            continue
        wid = obs.get('watch_id')
        src = obs.get('source')
        price = obs.get('price_usd')
        if not isinstance(wid, int):
            rejections.append({'index': i, 'reason': 'watch_id missing/not int'})
            continue
        if not isinstance(src, str) or not src.startswith(WATCH_OBS_SOURCE_PREFIX):
            rejections.append({'index': i, 'reason': 'source must start with ' + WATCH_OBS_SOURCE_PREFIX})
            continue
        if len(src) > 50:
            rejections.append({'index': i, 'reason': 'source too long'})
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            rejections.append({'index': i, 'reason': 'price_usd not numeric'})
            continue
        if price_f < WATCH_OBS_PRICE_FLOOR_USD:
            rejections.append({'index': i, 'reason': 'price below floor'})
            continue
        deep_link = obs.get('deep_link')
        if deep_link is not None and (not isinstance(deep_link, str) or len(deep_link) > 1000):
            rejections.append({'index': i, 'reason': 'deep_link invalid'})
            continue
        honors = obs.get('deep_link_honors_price')
        if honors is not None and not isinstance(honors, bool):
            rejections.append({'index': i, 'reason': 'deep_link_honors_price not bool'})
            continue
        data = obs.get('data')
        if data is not None and not isinstance(data, dict):
            rejections.append({'index': i, 'reason': 'data must be object'})
            continue
        cleaned.append({
            'watch_id': wid, 'source': src, 'price_usd': price_f,
            'deep_link': deep_link, 'deep_link_honors_price': bool(honors) if honors is not None else False,
            'data': data or {},
        })

    if not cleaned:
        return jsonify({'error': 'no valid observations', 'rejections': rejections}), 400

    # ── DB pass: validate watches active + per-watch ceiling + rate cap + insert ──
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    accepted = []
    skipped = list(rejections)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Load watch metadata in one shot
        watch_ids = sorted({o['watch_id'] for o in cleaned})
        cur.execute("""
            SELECT pk_id, watch_type, status
            FROM crab.member_watches
            WHERE pk_id = ANY(%s)
        """, (watch_ids,))
        watch_meta = {r['pk_id']: dict(r) for r in cur.fetchall()}

        # Recent (watch, source) observations for rate cap
        cur.execute("""
            SELECT watch_id, source, MAX(observed_at) AS last_at
            FROM crab.watch_history
            WHERE watch_id = ANY(%s)
              AND observed_at > NOW() - %s::interval
            GROUP BY watch_id, source
        """, (watch_ids, f'{WATCH_OBS_PER_WATCH_SOURCE_RATE_MIN} minutes'))
        recent = {(r['watch_id'], r['source']): r['last_at'] for r in cur.fetchall()}

        for o in cleaned:
            meta = watch_meta.get(o['watch_id'])
            if not meta:
                skipped.append({'watch_id': o['watch_id'], 'reason': 'watch not found'})
                continue
            if meta['status'] != 'active':
                skipped.append({'watch_id': o['watch_id'], 'reason': f"watch status={meta['status']}"})
                continue
            ceiling = (WATCH_OBS_PRICE_CEILING_FLIGHT if meta['watch_type'] == 'flight'
                       else WATCH_OBS_PRICE_CEILING_HOTEL)
            if o['price_usd'] > ceiling:
                skipped.append({'watch_id': o['watch_id'], 'reason': f"price above ceiling {ceiling}"})
                continue
            if (o['watch_id'], o['source']) in recent:
                skipped.append({'watch_id': o['watch_id'], 'source': o['source'],
                                'reason': 'rate cap (10min)'})
                continue
            accepted.append(o)

        # ── Insert pass — grouped per watch via record_watch_scan ──
        from utilities.postgres_utils import record_watch_scan
        by_watch = {}
        for o in accepted:
            by_watch.setdefault(o['watch_id'], []).append(o)
        inserted = 0
        for wid, group in by_watch.items():
            row = record_watch_scan(wid, group)
            if row:
                inserted += len(group)
    finally:
        conn.close()

    return jsonify({
        'accepted': inserted,
        'rejected': len(skipped),
        'rejections': skipped[:20],  # cap for response size
    }), 200


@bp.route('/api/opencrab/scan-travelpayouts', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_scan_travelpayouts():
    """Server-side Travelpayouts adapter — invoked by OpenClaw watch-scanner.

    Manifest §17 spirit: the TP affiliate key never leaves App Engine. The
    VPS posts route+dates and receives a sanitized observation. Same source
    name as the inline adapter (`openclaw_travelpayouts`) so the rest of
    the pipeline is unchanged.

    Guardrails:
      - Field allowlist on input (rejects unknown keys)
      - Source-prefix enforced server-side ('openclaw_travelpayouts')
      - Active-watch check
      - Price sanity floor/ceiling
      - Kill switch via CRAB_OPENCRAB_READONLY
      - 5KB max body
    """
    import os
    if os.environ.get('CRAB_OPENCRAB_READONLY', '').lower() == 'on':
        return jsonify({'error': 'opencrab writes frozen'}), 503

    cl = request.content_length
    if cl is not None and cl > 5 * 1024:
        return jsonify({'error': 'payload too large'}), 413

    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({'error': f'invalid json: {e}'}), 400

    extra = set(body.keys()) - _SCAN_TP_KEYS
    if extra:
        return jsonify({'error': 'unknown keys', 'unknown': sorted(extra)}), 400

    wid = body.get('watch_id')
    if not isinstance(wid, int):
        return jsonify({'error': 'watch_id missing/not int'}), 400
    wt = body.get('watch_type')
    if wt not in ('flight', 'hotel'):
        return jsonify({'error': 'watch_type must be flight|hotel'}), 400
    origin = body.get('origin') or None
    destination = body.get('destination') or None
    checkin = body.get('checkin') or None
    checkout = body.get('checkout') or None
    if not (destination and checkin):
        return jsonify({'error': 'destination and checkin required'}), 400

    # Verify watch exists + is active + matches the asserted type.
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT watch_type, status FROM crab.member_watches WHERE pk_id = %s
        """, (wid,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'watch not found'}), 400
    if row['status'] != 'active':
        return jsonify({'error': f"watch status={row['status']}"}), 400
    if row['watch_type'] != wt:
        return jsonify({'error': 'watch_type mismatch'}), 400

    # ── Run the server-side adapter ──
    # Hotels are upstream-dead (Hotellook v2 404). Return null observation
    # cleanly so OpenClaw doesn't have to know about deprecation.
    if wt == 'hotel':
        return jsonify({'observation': None, 'reason': 'hotel_api_deprecated'}), 200

    if not (origin and re.match(r'^[A-Z]{3}$', origin) and re.match(r'^[A-Z]{3}$', destination)):
        return jsonify({'observation': None, 'reason': 'non_iata_route'}), 200

    try:
        from utilities.adapters.travelpayouts import TravelpayoutsAdapter
        adapter = TravelpayoutsAdapter()
        flights = adapter.search_flights(
            origin=origin, destination=destination,
            depart_date=checkin, return_date=checkout,
        ) or []
    except Exception as e:
        logger.warning(f"scan-travelpayouts adapter crash for watch {wid}: {e}")
        return jsonify({'observation': None, 'reason': f'adapter_error: {type(e).__name__}'}), 200

    # Pick cheapest flight.
    cheapest = None
    for f in flights:
        p = f.get('price_usd')
        if not p:
            continue
        if cheapest is None or p < cheapest.get('price_usd', float('inf')):
            cheapest = f
    if not cheapest:
        return jsonify({'observation': None, 'reason': 'no_results'}), 200

    price = float(cheapest['price_usd'])
    if price < WATCH_OBS_PRICE_FLOOR_USD or price > WATCH_OBS_PRICE_CEILING_FLIGHT:
        return jsonify({'observation': None, 'reason': 'price_out_of_range'}), 200

    return jsonify({
        'observation': {
            'watch_id': wid,
            'source': 'openclaw_travelpayouts',
            'price_usd': price,
            'deep_link': cheapest.get('deep_link'),
            'deep_link_honors_price': bool(cheapest.get('deep_link_honors_price', True)),
            'data': {
                'airline': cheapest.get('airline'),
                'depart_at': cheapest.get('depart_at'),
                'stops': cheapest.get('stops', 0),
            },
        }
    }), 200


# ─── Area context — independent-source review/event/sentiment ingestion ───

def _compute_content_hash(title, text):
    """Normalized content hash for dedup. Catches crossposts + copy-pastes.

    Computed server-side (not trusting client) — sha256 of:
       lowercase(strip(title)) + '|' + lowercase(strip(first 200 chars of text))
    with whitespace collapsed.
    """
    import hashlib
    import re as _re
    t = (title or '').lower().strip()
    b = (text or '')[:200].lower().strip()
    normalized = _re.sub(r'\s+', ' ', t + '|' + b)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _sanitize_area_observation(o):
    """Strip non-allowlisted fields, enforce caps. Returns None if invalid."""
    if not isinstance(o, dict):
        return None
    clean = {k: v for k, v in o.items() if k in AREA_OBS_ALLOWED_FIELDS}
    # Required
    if clean.get('area_slug') not in AREA_OBS_ALLOWED_AREAS:
        return None
    if clean.get('source') not in AREA_OBS_ALLOWED_SOURCES:
        return None
    if not clean.get('source_id') or not isinstance(clean['source_id'], str):
        return None
    # Type/length normalization
    for k in ('source_id', 'source_url', 'source_sub', 'author', 'title',
             'scraped_by', 'resort_pid'):
        v = clean.get(k)
        if v is not None and not isinstance(v, str):
            return None
        if isinstance(v, str) and len(v) > 500:
            clean[k] = v[:500]
    if 'text' in clean:
        t = clean['text']
        if not isinstance(t, str):
            return None
        if len(t) > AREA_OBS_MAX_TEXT_LEN:
            clean['text'] = t[:AREA_OBS_MAX_TEXT_LEN]
    if 'score' in clean and clean['score'] is not None:
        try:
            clean['score'] = int(clean['score'])
        except (TypeError, ValueError):
            clean['score'] = None
    if 'raw_json' in clean and not isinstance(clean['raw_json'], (dict, list)):
        clean['raw_json'] = None
    # resort_pid must look like 'P<digits>' if present
    rp = clean.get('resort_pid')
    if rp and not (rp.startswith('P') and rp[1:].isdigit()):
        clean['resort_pid'] = None
    return clean


@bp.route('/api/opencrab/area-observations', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_area_observations():
    """VPS posts raw multi-source observations (Reddit threads, TUG posts, etc.)
    for an area's resort context layer.

    Least-privilege contract:
      - INSERT only (UPSERT by (source, source_id) — re-scrapes are idempotent).
      - Allowlisted area_slug (whitelist), allowlisted source.
      - Per-call record cap, per-record text cap, total payload cap.
      - resort_pid validated to 'P<digits>' format if provided.
      - Writes can be frozen globally via CRAB_OPENCRAB_READONLY=on.
    """
    if _readonly_mode():
        return jsonify({'error': 'opencrab writes frozen (CRAB_OPENCRAB_READONLY=on)'}), 503

    if (request.content_length or 0) > AREA_OBS_MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload too large',
                        'max_bytes': AREA_OBS_MAX_PAYLOAD_BYTES}), 413

    body = request.get_json(silent=True) or {}
    obs_in = body.get('observations') or []
    if not isinstance(obs_in, list):
        return jsonify({'error': 'observations must be a list'}), 400
    if len(obs_in) > AREA_OBS_MAX_PER_POST:
        return jsonify({'error': 'too many records',
                        'max_per_post': AREA_OBS_MAX_PER_POST,
                        'received': len(obs_in)}), 413

    clean_obs = []
    dropped = 0
    for o in obs_in:
        c = _sanitize_area_observation(o)
        if c is None:
            dropped += 1
            continue
        clean_obs.append(c)
    if not clean_obs:
        return jsonify({'inserted': 0, 'updated': 0, 'dropped': dropped,
                        'reason': 'all_records_invalid'}), 200

    from utilities.postgres_utils import get_db_connection
    import json as _json
    conn = get_db_connection()
    inserted = 0
    updated = 0
    dedup_skipped = 0
    try:
        cur = conn.cursor()
        for c in clean_obs:
            # Server-side content hash — dedup defense (crosspost / copy-paste / requote)
            content_hash = _compute_content_hash(c.get('title'), c.get('text'))
            # Dedup: if this exact content was already seen under a different (source, source_id),
            # skip the insert (but still allow UPDATE of the matching source_id row).
            cur.execute("""
                SELECT obs_id, source, source_id
                FROM crab.area_observations
                WHERE content_hash = %s AND (source, source_id) != (%s, %s)
                LIMIT 1
            """, (content_hash, c['source'], c['source_id']))
            dup_row = cur.fetchone()
            if dup_row:
                # Identical content already exists under a different source_id — skip
                dedup_skipped += 1
                continue
            cur.execute("""
                INSERT INTO crab.area_observations
                  (area_slug, resort_pid, source, source_id, source_url,
                   source_sub, author, title, text, content_hash, posted_at, score,
                   raw_json, scraped_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (source, source_id) DO UPDATE SET
                  area_slug = EXCLUDED.area_slug,
                  resort_pid = COALESCE(EXCLUDED.resort_pid, crab.area_observations.resort_pid),
                  source_url = EXCLUDED.source_url,
                  source_sub = EXCLUDED.source_sub,
                  author = EXCLUDED.author,
                  title = EXCLUDED.title,
                  text = EXCLUDED.text,
                  content_hash = EXCLUDED.content_hash,
                  posted_at = EXCLUDED.posted_at,
                  score = EXCLUDED.score,
                  raw_json = EXCLUDED.raw_json,
                  scraped_at = now(),
                  scraped_by = EXCLUDED.scraped_by
                RETURNING (xmax = 0) AS inserted
            """, (
                c['area_slug'], c.get('resort_pid'), c['source'], c['source_id'],
                c.get('source_url'), c.get('source_sub'), c.get('author'),
                c.get('title'), c.get('text'), content_hash, c.get('posted_at'),
                c.get('score'),
                _json.dumps(c['raw_json']) if c.get('raw_json') is not None else None,
                c.get('scraped_by', 'openclaw'),
            ))
            was_insert = cur.fetchone()[0]
            if was_insert:
                inserted += 1
            else:
                updated += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': 'db_error', 'detail': str(e)[:200]}), 500
    finally:
        conn.close()

    return jsonify({
        'inserted': inserted,
        'updated': updated,
        'dedup_skipped': dedup_skipped,
        'dropped': dropped,
        'total_processed': len(clean_obs),
    }), 200


@bp.route('/api/opencrab/area-synthesis', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def opencrab_area_synthesis():
    """VPS posts a per-resort LLM synthesis (after running kumori synthesis).

    Least-privilege contract:
      - INSERT only. UI reads the latest row per (area_slug, resort_pid).
      - area_slug + resort_pid allowlisted/validated.
      - summary_json shape soft-validated.
      - Readonly killswitch honored.
    """
    if _readonly_mode():
        return jsonify({'error': 'opencrab writes frozen (CRAB_OPENCRAB_READONLY=on)'}), 503
    if (request.content_length or 0) > AREA_SYN_MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload too large',
                        'max_bytes': AREA_SYN_MAX_PAYLOAD_BYTES}), 413

    body = request.get_json(silent=True) or {}
    area_slug = body.get('area_slug')
    resort_pid = body.get('resort_pid')
    model = body.get('model')
    n_obs = body.get('n_observations')
    summary = body.get('summary')

    if area_slug not in AREA_OBS_ALLOWED_AREAS:
        return jsonify({'error': 'area_slug not allowlisted'}), 400
    if not (isinstance(resort_pid, str) and resort_pid.startswith('P') and resort_pid[1:].isdigit()):
        return jsonify({'error': 'resort_pid must be P<digits>'}), 400
    if not isinstance(summary, dict):
        return jsonify({'error': 'summary must be an object'}), 400
    if not isinstance(model, str) or len(model) > 60:
        return jsonify({'error': 'model required (<=60 chars)'}), 400
    try:
        n_obs = int(n_obs) if n_obs is not None else None
    except (TypeError, ValueError):
        n_obs = None

    import json as _json
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.area_synthesis
              (area_slug, resort_pid, model, n_observations, summary_json)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            RETURNING syn_id
        """, (area_slug, resort_pid, model, n_obs, _json.dumps(summary)))
        syn_id = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': 'db_error', 'detail': str(e)[:200]}), 500
    finally:
        conn.close()

    return jsonify({'syn_id': syn_id, 'area_slug': area_slug, 'resort_pid': resort_pid}), 200
