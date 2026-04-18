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
    """Default ON until an explicit override secret is set to 'off'."""
    try:
        from utilities.google_auth_utils import get_secret
        v = (get_secret('CRAB_OPENCRAB_TEST_MODE') or 'on').strip().lower()
        return v != 'off'
    except Exception:
        return True


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

    raw = request.get_data(cache=False) or b''
    if len(raw) > WATCH_RESULTS_MAX_PAYLOAD_BYTES:
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

        # Also append to price_history if we got a new cheapest
        if cheapest:
            try:
                cur.execute("""
                    INSERT INTO crab.price_history (watch_id, price_usd, source, observed_at)
                    VALUES (%s, %s, %s, %s)
                """, (watch_id, cheapest['price_usd'], cheapest.get('source', 'opencrab'), now))
            except Exception as e:
                logger.debug(f"price_history insert skipped: {e}")

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
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }), 200
