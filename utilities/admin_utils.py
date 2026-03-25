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


def get_admin_dashboard_data(user_page=1, plan_page=1, per_page=50):
    """Fetch admin dashboard data with pagination for users/plans."""
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

    # Active users in last 24h (sent messages or voted)
    cur.execute("""
        SELECT COUNT(DISTINCT user_id) as count FROM (
            SELECT user_id FROM crab.messages WHERE created_at > NOW() - INTERVAL '24 hours'
            UNION
            SELECT user_id FROM crab.votes WHERE created_at > NOW() - INTERVAL '24 hours'
        ) active
    """)
    active_users_today = cur.fetchone()['count']

    # Watches with booked status
    cur.execute("SELECT COUNT(*) as count FROM crab.member_watches WHERE status = 'booked'")
    watches_booked_count = cur.fetchone()['count']

    # Bot plans count
    cur.execute("SELECT COUNT(*) as count FROM crab.plans WHERE title LIKE '[BOT]%'")
    bot_plans_count = cur.fetchone()['count']

    # --- Users table (paginated, first page) ---
    offset = (user_page - 1) * per_page
    cur.execute("""
        SELECT u.pk_id, u.email, u.full_name, u.home_airport, u.home_location,
               u.phone_number, u.notify_chat, u.notify_updates, u.notify_channel,
               u.is_admin, u.created_at, u.updated_at,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.user_id = u.pk_id) as plan_count,
               (SELECT COUNT(*) FROM crab.messages c WHERE c.user_id = u.pk_id) as message_count
        FROM crab.users u ORDER BY u.created_at DESC
        LIMIT %s OFFSET %s
    """, (per_page, offset))
    users = cur.fetchall()

    # --- Plans table (paginated, first page) ---
    offset = (plan_page - 1) * per_page
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
        LIMIT %s OFFSET %s
    """, (per_page, offset))
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

    # --- Recent votes (for activity tab) ---
    cur.execute("""
        SELECT v.created_at, u.full_name, p.title as plan_title,
               d.destination_name, v.vote_type
        FROM crab.votes v
        JOIN crab.users u ON u.pk_id = v.user_id
        JOIN crab.plans p ON p.plan_id = v.plan_id
        LEFT JOIN crab.destination_suggestions d ON d.suggestion_id = v.suggestion_id
        ORDER BY v.created_at DESC LIMIT 20
    """)
    recent_votes = cur.fetchall()

    # --- Recent joins (for activity tab) ---
    cur.execute("""
        SELECT pm.joined_at as created_at, u.full_name, p.title as plan_title
        FROM crab.plan_members pm
        JOIN crab.users u ON u.pk_id = pm.user_id
        JOIN crab.plans p ON p.plan_id = pm.plan_id
        ORDER BY pm.joined_at DESC LIMIT 20
    """)
    recent_joins = cur.fetchall()

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
        'active_users_today': active_users_today,
        'watches_booked_count': watches_booked_count,
        'bot_plans_count': bot_plans_count,
        'users': users,
        'total_users_count': total_users,
        'plans': plans,
        'total_plans_count': total_plans,
        'recent_messages': recent_messages,
        'recent_votes': recent_votes,
        'recent_joins': recent_joins,
        'messages_24h': messages_24h,
        'votes_24h': votes_24h,
        'joins_24h': joins_24h,
        'invite_stats': invite_stats,
        'per_page': per_page,
    }


def get_admin_users(search=None, page=1, per_page=50, sort_by='created_at', sort_dir='desc'):
    """Paginated user list with search."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Whitelist sort columns
    allowed_sorts = {'created_at', 'full_name', 'email', 'pk_id', 'plan_count', 'message_count'}
    if sort_by not in allowed_sorts:
        sort_by = 'created_at'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'

    where_clause = ""
    params = []
    if search:
        where_clause = "WHERE u.full_name ILIKE %s OR u.email ILIKE %s"
        params = [f'%{search}%', f'%{search}%']

    # Count
    cur.execute(f"SELECT COUNT(*) as count FROM crab.users u {where_clause}", params)
    total = cur.fetchone()['count']

    # Sort by subquery columns needs wrapping
    order_col = f"u.{sort_by}" if sort_by not in ('plan_count', 'message_count') else sort_by
    offset = (page - 1) * per_page

    cur.execute(f"""
        SELECT u.pk_id, u.email, u.full_name, u.home_airport, u.home_location,
               u.phone_number, u.notify_chat, u.notify_updates, u.notify_channel,
               u.is_admin, u.created_at, u.updated_at,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.user_id = u.pk_id) as plan_count,
               (SELECT COUNT(*) FROM crab.messages c WHERE c.user_id = u.pk_id) as message_count
        FROM crab.users u {where_clause}
        ORDER BY {order_col} {sort_dir}
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])
    users = cur.fetchall()

    cur.close()
    conn.close()

    pages = max(1, (total + per_page - 1) // per_page)
    return {
        'users': [dict(u) for u in users],
        'total': total,
        'page': page,
        'pages': pages,
        'per_page': per_page,
    }


def get_admin_plans(search=None, status=None, page=1, per_page=50, sort_by='created_at', sort_dir='desc'):
    """Paginated plan list with search and status filter."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    allowed_sorts = {'created_at', 'title', 'member_count', 'msg_count', 'vote_count'}
    if sort_by not in allowed_sorts:
        sort_by = 'created_at'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'

    conditions = []
    params = []
    if search:
        conditions.append("(p.title ILIKE %s OR u.full_name ILIKE %s)")
        params += [f'%{search}%', f'%{search}%']
    if status:
        conditions.append("COALESCE(p.status, 'voting') = %s")
        params.append(status)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Count
    cur.execute(f"""
        SELECT COUNT(*) as count FROM crab.plans p
        LEFT JOIN crab.users u ON u.pk_id = p.organizer_id
        {where_clause}
    """, params)
    total = cur.fetchone()['count']

    order_col = f"p.{sort_by}" if sort_by not in ('member_count', 'msg_count', 'vote_count') else sort_by
    offset = (page - 1) * per_page

    cur.execute(f"""
        SELECT p.pk_id, p.plan_id, p.title, p.invite_token, p.status,
               p.created_at,
               u.full_name as organizer_name,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.plan_id = p.plan_id) as member_count,
               (SELECT COUNT(*) FROM crab.destination_suggestions d WHERE d.plan_id = p.plan_id) as dest_count,
               (SELECT COUNT(*) FROM crab.messages c WHERE c.plan_id = p.plan_id) as msg_count,
               (SELECT COUNT(*) FROM crab.votes v WHERE v.plan_id = p.plan_id) as vote_count
        FROM crab.plans p
        LEFT JOIN crab.users u ON u.pk_id = p.organizer_id
        {where_clause}
        ORDER BY {order_col} {sort_dir}
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])
    plans = cur.fetchall()

    cur.close()
    conn.close()

    pages = max(1, (total + per_page - 1) // per_page)
    return {
        'plans': [dict(p) for p in plans],
        'total': total,
        'page': page,
        'pages': pages,
        'per_page': per_page,
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
