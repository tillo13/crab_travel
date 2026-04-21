"""
Group-membership gate for `/timeshare/g/<group_uuid>/*` routes.

404-on-miss (not 403) — non-members don't learn the group exists. Malformed
UUID in the URL raises inside Postgres cast; we swallow and treat as "not
a member" so the user sees a 404 rather than a 500.

Role hierarchy: owner > admin > family > readonly. A view decorated with
`@group_member_required('admin')` accepts admin or owner; everyone else
gets 404.
"""

from functools import wraps

from flask import abort, request

from route_helpers import login_required
from utilities.postgres_utils import get_db_connection

_ROLE_RANK = {'readonly': 1, 'family': 2, 'admin': 3, 'owner': 4}


def _get_membership(group_uuid, user_id):
    """Returns (group_id, role) tuple or None if user is not a member."""
    if not user_id:
        return None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.group_id, gm.role
              FROM crab.timeshare_groups g
              JOIN crab.timeshare_group_members gm ON gm.group_id = g.group_id
             WHERE g.group_id = %s::uuid
               AND gm.user_id = %s
               AND gm.accepted_at IS NOT NULL
               AND g.status = 'active'
        """, (group_uuid, user_id))
        return cur.fetchone()
    except Exception:
        return None
    finally:
        conn.close()


def group_member_required(min_role='family'):
    required = _ROLE_RANK[min_role]

    def decorator(f):
        @wraps(f)
        @login_required
        def decorated(group_uuid, *args, **kwargs):
            from flask import session
            user = session.get('user') or {}
            membership = _get_membership(group_uuid, user.get('id'))
            if not membership:
                abort(404)
            group_id, role = membership
            if _ROLE_RANK.get(role, 0) < required:
                abort(404)
            request.timeshare_group_id = group_id
            request.timeshare_role = role
            return f(group_uuid, *args, **kwargs)
        return decorated
    return decorator


def get_user_timeshare_groups(user_id):
    """List groups the user is an accepted member of. Powers the nav context processor."""
    if not user_id:
        return []
    conn = get_db_connection()
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT g.group_id, g.name, gm.role
              FROM crab.timeshare_groups g
              JOIN crab.timeshare_group_members gm ON gm.group_id = g.group_id
             WHERE gm.user_id = %s
               AND gm.accepted_at IS NOT NULL
               AND g.status = 'active'
             ORDER BY g.created_at ASC
        """, (user_id,))
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
