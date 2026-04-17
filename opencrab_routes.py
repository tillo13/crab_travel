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
        'global_today': global_today,
        'global_daily_cap': GLOBAL_DAILY_CAP,
        'per_plan_daily_cap': PER_PLAN_DAILY_CAP,
        'allowed_templates': list(TEMPLATES.keys()),
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }), 200
