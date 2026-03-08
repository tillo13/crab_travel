"""Admin utilities for crab.travel — dashboard data, user mimicking, admin checks."""

import logging
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
               COALESCE(p.invite_views, 0) as views,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.plan_id = p.plan_id) as joins
        FROM crab.plans p
        ORDER BY COALESCE(p.invite_views, 0) DESC
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
