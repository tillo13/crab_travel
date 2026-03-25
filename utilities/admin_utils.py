"""Admin utilities for crab.travel — dashboard data, user mimicking, admin checks."""

import logging
from datetime import datetime, timezone, timedelta
from utilities.postgres_utils import get_db_connection
import psycopg2.extras

logger = logging.getLogger(__name__)


def is_admin(user_id):
    """Check if user has admin privileges."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT is_admin FROM crab.users WHERE pk_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row and row[0] is True
    except Exception:
        return False


def get_admin_dashboard_data():
    """Fetch all admin dashboard data in one call."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # --- Aggregate stats ---
    cur.execute("SELECT COUNT(*) as count FROM crab.users")
    total_users = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM crab.plans")
    total_plans = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM crab.messages")
    total_messages = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM crab.plan_members")
    total_memberships = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM crab.destination_suggestions")
    total_destinations = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM crab.votes")
    total_votes = cur.fetchone()['count']

    # Plans by stage
    cur.execute("""
        SELECT COALESCE(status, 'voting') as stage, COUNT(*) as count
        FROM crab.plans GROUP BY COALESCE(status, 'voting')
    """)
    plans_by_stage = {r['stage']: r['count'] for r in cur.fetchall()}

    # --- Users table ---
    cur.execute("""
        SELECT u.pk_id, u.email, u.full_name, u.home_airport, u.home_location,
               u.phone_number, u.notify_chat, u.notify_updates, u.notify_channel,
               u.is_admin, u.created_at, u.updated_at,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.user_id = u.pk_id) as plan_count,
               (SELECT COUNT(*) FROM crab.messages c WHERE c.user_id = u.pk_id) as message_count
        FROM crab.users u ORDER BY u.pk_id
    """)
    users = cur.fetchall()

    # --- Plans table ---
    cur.execute("""
        SELECT p.pk_id, p.plan_id, p.title, p.invite_token, p.status,
               p.created_at,
               u.full_name as organizer_name,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.plan_id = p.plan_id) as member_count,
               (SELECT COUNT(*) FROM crab.destination_suggestions d WHERE d.plan_id = p.plan_id) as dest_count,
               (SELECT COUNT(*) FROM crab.messages c WHERE c.plan_id = p.plan_id) as msg_count,
               (SELECT COUNT(*) FROM crab.votes v WHERE v.plan_id = p.plan_id) as vote_count
        FROM crab.plans p
        LEFT JOIN crab.users u ON u.pk_id = p.organizer_id
        ORDER BY p.created_at DESC
    """)
    plans = cur.fetchall()

    # --- Recent messages ---
    cur.execute("""
        SELECT c.content, c.created_at, u.full_name, p.title as plan_title
        FROM crab.messages c
        JOIN crab.users u ON u.pk_id = c.user_id
        JOIN crab.plans p ON p.plan_id = c.plan_id
        ORDER BY c.created_at DESC LIMIT 20
    """)
    recent_messages = cur.fetchall()

    # --- Recent activity (last 24h) ---
    cur.execute("""
        SELECT COUNT(*) as count FROM crab.messages
        WHERE created_at > NOW() - INTERVAL '24 hours'
    """)
    messages_24h = cur.fetchone()['count']

    cur.execute("""
        SELECT COUNT(*) as count FROM crab.votes
        WHERE created_at > NOW() - INTERVAL '24 hours'
    """)
    votes_24h = cur.fetchone()['count']

    cur.execute("""
        SELECT COUNT(*) as count FROM crab.plan_members
        WHERE joined_at > NOW() - INTERVAL '24 hours'
    """)
    joins_24h = cur.fetchone()['count']

    # --- Invite link stats ---
    cur.execute("""
        SELECT p.title, p.invite_token,
               (SELECT COUNT(*) FROM crab.invite_views iv WHERE iv.plan_id = p.plan_id) as views,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.plan_id = p.plan_id) as joins
        FROM crab.plans p
        ORDER BY views DESC
    """)
    invite_stats = cur.fetchall()

    cur.close()
    conn.close()

    return {
        'total_users': total_users,
        'total_plans': total_plans,
        'total_messages': total_messages,
        'total_memberships': total_memberships,
        'total_destinations': total_destinations,
        'total_votes': total_votes,
        'plans_by_stage': plans_by_stage,
        'users': users,
        'plans': plans,
        'recent_messages': recent_messages,
        'messages_24h': messages_24h,
        'votes_24h': votes_24h,
        'joins_24h': joins_24h,
        'invite_stats': invite_stats,
    }


def handle_mimic_action(session, action, target_user_id, real_user_id):
    """Handle mimic start/stop."""
    if action == 'stop':
        session['user'] = _get_user_session_data(real_user_id)
        session.pop('_real_uid', None)
        return True, "Stopped mimicking"
    elif action == 'mimic' and target_user_id:
        session['_real_uid'] = real_user_id
        target_data = _get_user_session_data(int(target_user_id))
        if target_data:
            session['user'] = target_data
            return True, f"Now mimicking {target_data['name']}"
        return False, "User not found"
    return False, "Invalid action"


def _get_user_session_data(user_id):
    """Get user data formatted for session storage."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT pk_id, email, full_name, picture_url FROM crab.users WHERE pk_id = %s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        return None
    return {
        'id': user['pk_id'],
        'email': user['email'],
        'name': user['full_name'],
        'picture': user['picture_url'],
    }


def get_ops_data():
    """Fetch operations dashboard data — LLM health, watches, cron, API stats."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    data = {}

    # ── LLM Backend Health (last 24h) ──
    cur.execute("""
        SELECT backend,
               COUNT(*) as total_calls,
               SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
               SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as failures,
               ROUND(AVG(duration_ms)) as avg_ms,
               ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)) as p95_ms,
               SUM(prompt_length) as total_prompt_bytes,
               SUM(response_length) as total_response_bytes
        FROM crab.llm_calls
        WHERE created_at > NOW() - INTERVAL '24 hours'
        GROUP BY backend ORDER BY total_calls DESC
    """)
    data['llm_24h'] = [dict(r) for r in cur.fetchall()]

    # LLM error breakdown (last 24h)
    cur.execute("""
        SELECT backend, error_type, COUNT(*) as count
        FROM crab.llm_calls
        WHERE created_at > NOW() - INTERVAL '24 hours' AND NOT success
        GROUP BY backend, error_type ORDER BY count DESC
    """)
    data['llm_errors'] = [dict(r) for r in cur.fetchall()]

    # LLM calls by caller (what's using LLMs)
    cur.execute("""
        SELECT caller, COUNT(*) as calls,
               SUM(CASE WHEN success THEN 1 ELSE 0 END) as ok
        FROM crab.llm_calls
        WHERE created_at > NOW() - INTERVAL '24 hours'
        GROUP BY caller ORDER BY calls DESC
    """)
    data['llm_by_caller'] = [dict(r) for r in cur.fetchall()]

    # LLM hourly volume (last 24h for chart)
    cur.execute("""
        SELECT date_trunc('hour', created_at) as hour,
               COUNT(*) as calls,
               SUM(CASE WHEN success THEN 1 ELSE 0 END) as ok,
               SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail
        FROM crab.llm_calls
        WHERE created_at > NOW() - INTERVAL '24 hours'
        GROUP BY hour ORDER BY hour
    """)
    data['llm_hourly'] = [dict(r) for r in cur.fetchall()]

    # ── Watch Engine Stats ──
    cur.execute("""
        SELECT status, COUNT(*) as count FROM crab.member_watches GROUP BY status
    """)
    data['watch_status'] = {r['status']: r['count'] for r in cur.fetchall()}

    cur.execute("SELECT COUNT(*) as total FROM crab.member_watches")
    data['watch_total'] = cur.fetchone()['total']

    cur.execute("SELECT COUNT(*) as c FROM crab.member_watches WHERE recommendation IS NOT NULL")
    data['watch_with_recs'] = cur.fetchone()['c']

    cur.execute("""
        SELECT COUNT(DISTINCT watch_id) as watches_with_history,
               COUNT(*) as total_observations
        FROM crab.watch_history
    """)
    wh = cur.fetchone()
    data['watches_with_history'] = wh['watches_with_history']
    data['total_price_observations'] = wh['total_observations']

    # Recent price checks (last 24h)
    cur.execute("""
        SELECT COUNT(*) as checks_24h,
               COUNT(DISTINCT watch_id) as unique_watches_24h
        FROM crab.watch_history WHERE observed_at > NOW() - INTERVAL '24 hours'
    """)
    data['watch_checks_24h'] = dict(cur.fetchone())

    # Watch recommendations breakdown
    cur.execute("""
        SELECT recommendation->>'verdict' as verdict, COUNT(*) as count
        FROM crab.member_watches
        WHERE recommendation IS NOT NULL AND status = 'active'
        GROUP BY verdict ORDER BY count DESC
    """)
    data['watch_verdicts'] = [dict(r) for r in cur.fetchall()]

    # ── Plan Pipeline Stats ──
    cur.execute("""
        SELECT status, COUNT(*) as count FROM crab.plans GROUP BY status ORDER BY count DESC
    """)
    data['plan_stages'] = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT COUNT(*) as total_members FROM crab.plan_members
    """)
    data['total_members'] = cur.fetchone()['total_members']

    cur.execute("""
        SELECT COUNT(*) as plans_with_watches
        FROM (SELECT DISTINCT plan_id FROM crab.member_watches) sub
    """)
    data['plans_with_watches'] = cur.fetchone()['plans_with_watches']

    # ── Bot Run Stats ──
    cur.execute("""
        SELECT COUNT(*) as total_runs,
               SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) as passed,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
               SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running
        FROM crab.bot_runs
        WHERE started_at > NOW() - INTERVAL '24 hours'
    """)
    data['bot_runs_24h'] = dict(cur.fetchone())

    # Recent bot runs
    cur.execute("""
        SELECT run_id, status, mode, started_at, finished_at,
               phases_passed, phases_failed, phases_warned
        FROM crab.bot_runs
        ORDER BY started_at DESC LIMIT 10
    """)
    data['recent_bot_runs'] = [dict(r) for r in cur.fetchall()]

    # ── LLM Daily Caps (today) ──
    cur.execute("""
        SELECT backend,
               SUM(CASE WHEN success THEN 1 ELSE 0 END) as used_today
        FROM crab.llm_calls
        WHERE created_at::date = CURRENT_DATE
        GROUP BY backend ORDER BY used_today DESC
    """)
    data['llm_daily_usage'] = [dict(r) for r in cur.fetchall()]

    # ── Recent LLM Errors (last 10) ──
    cur.execute("""
        SELECT backend, model, caller, error_type, status_code,
               LEFT(error_message, 120) as error_msg,
               duration_ms, created_at
        FROM crab.llm_calls
        WHERE NOT success AND created_at > NOW() - INTERVAL '24 hours'
        ORDER BY created_at DESC LIMIT 15
    """)
    data['recent_llm_errors'] = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()
    return data
