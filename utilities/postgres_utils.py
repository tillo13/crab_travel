import logging
import os
import threading
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import psycopg2.pool
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

GCP_PROJECT_ID = "kumori-404602"

_credentials_cache = {}
_connection_pools = {}
_pool_lock = threading.Lock()


def _get_credentials():
    global _credentials_cache
    if GCP_PROJECT_ID in _credentials_cache:
        return _credentials_cache[GCP_PROJECT_ID]
    creds = {
        'host': get_secret('CRAB_POSTGRES_IP'),
        'dbname': get_secret('CRAB_POSTGRES_DB_NAME'),
        'user': get_secret('CRAB_POSTGRES_USERNAME'),
        'password': get_secret('CRAB_POSTGRES_PASSWORD'),
        'connection_name': get_secret('CRAB_POSTGRES_CONNECTION_NAME'),
    }
    _credentials_cache[GCP_PROJECT_ID] = creds
    return creds


def _get_connection_pool():
    global _connection_pools
    with _pool_lock:
        if GCP_PROJECT_ID in _connection_pools:
            return _connection_pools[GCP_PROJECT_ID]
        creds = _get_credentials()
        is_gcp = os.environ.get('GAE_ENV', '').startswith('standard')
        if is_gcp:
            db_socket_dir = os.environ.get("DB_SOCKET_DIR", "/cloudsql")
            host = f"{db_socket_dir}/{creds['connection_name']}"
        else:
            host = creds['host']
        # Budget: 50 max_connections shared across 12 apps on kumori Cloud SQL
        # Crab gets 6 max (active app with bots + watches + users)
        pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=6,
            dbname=creds['dbname'], user=creds['user'],
            password=creds['password'], host=host,
            connect_timeout=10,
            options='-c statement_timeout=30000'  # 30s query timeout to prevent stuck connections
        )
        _connection_pools[GCP_PROJECT_ID] = pool
        logger.info("🔌 Database connection pool created")
        return pool


class PooledConnection:
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool

    def close(self):
        if self._conn:
            try:
                # Always rollback before returning — prevents "idle in transaction (aborted)" leaks
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                self._pool.putconn(self._conn)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        self.close()
        return False


def get_db_connection():
    pool = _get_connection_pool()
    try:
        conn = pool.getconn()
    except psycopg2.pool.PoolError:
        # Pool corrupted (all connections permanently "checked out" after startup failures).
        # Nuke and recreate it.
        logger.warning("⚠️ Pool exhausted — recreating connection pool")
        with _pool_lock:
            try:
                pool.closeall()
            except Exception:
                pass
            _connection_pools.pop(GCP_PROJECT_ID, None)
        pool = _get_connection_pool()
        conn = pool.getconn()
    # Test if connection is alive — Cloud SQL kills idle connections
    try:
        conn.cursor().execute("SELECT 1")
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        logger.warning("Stale DB connection detected, reconnecting")
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        _connection_pools.pop(GCP_PROJECT_ID, None)
        pool = _get_connection_pool()
        conn = pool.getconn()
    return PooledConnection(conn, pool)


@contextmanager
def db_cursor(dict_cursor=True, commit=False):
    """Context manager for database operations — guarantees connection returns to pool.
    Usage:
        with db_cursor() as cur:
            cur.execute("SELECT ...")
            rows = cur.fetchall()
        # connection auto-returned, auto-rolled-back on error
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if dict_cursor else conn.cursor()
    try:
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

# init_database moved to utilities/postgres_schema.py

def upsert_user(google_userinfo, access_token=None, refresh_token=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.users (google_id, email, full_name, picture_url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (google_id) DO UPDATE SET
                email = EXCLUDED.email,
                full_name = EXCLUDED.full_name,
                picture_url = EXCLUDED.picture_url,
                updated_at = NOW()
            RETURNING pk_id, google_id, email, full_name, picture_url, home_airport, is_admin
        """, (
            google_userinfo.get('sub'),
            google_userinfo.get('email'),
            google_userinfo.get('name'),
            google_userinfo.get('picture'),
        ))
        user = cursor.fetchone()
        # Store OAuth tokens if provided (for Calendar API)
        if access_token:
            token_sql = "UPDATE crab.users SET google_access_token = %s, updated_at = NOW()"
            token_params = [access_token]
            if refresh_token:
                token_sql += ", google_refresh_token = %s"
                token_params.append(refresh_token)
            token_sql += " WHERE pk_id = %s"
            token_params.append(user['pk_id'])
            cursor.execute(token_sql, token_params)
        # Create profile row if it doesn't exist
        cursor.execute("""
            INSERT INTO crab.user_profiles (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user['pk_id'],))
        conn.commit()
        return dict(user)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Upsert user failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_user_profile(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT u.pk_id, u.google_id, u.email, u.full_name, u.picture_url,
                   u.home_location, u.home_airport, u.calendar_synced,
                   p.interests, p.dietary_needs, p.mobility_notes,
                   p.travel_style, p.accommodation_preference, p.budget_comfort,
                   p.bio, p.completed as profile_completed
            FROM crab.users u
            LEFT JOIN crab.user_profiles p ON p.user_id = u.pk_id
            WHERE u.pk_id = %s
        """, (user_id,))
        return cursor.fetchone()
    except Exception as e:
        logger.error(f"❌ Get profile failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_user_profile(user_id, data):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.user_profiles SET
                interests = %s, dietary_needs = %s, mobility_notes = %s,
                travel_style = %s, accommodation_preference = %s,
                budget_comfort = %s, bio = %s, completed = TRUE, updated_at = NOW()
            WHERE user_id = %s
        """, (
            psycopg2.extras.Json(data.get('interests', [])),
            data.get('dietary_needs'),
            data.get('mobility_notes'),
            data.get('travel_style'),
            data.get('accommodation_preference'),
            data.get('budget_comfort'),
            data.get('bio'),
            user_id,
        ))
        # Update fields on users table
        update_parts = []
        update_vals = []
        if data.get('home_location'):
            update_parts.append("home_location = %s")
            update_vals.append(data['home_location'])
        if 'home_airport' in data:
            update_parts.append("home_airport = %s")
            update_vals.append(data['home_airport'].upper().strip() if data['home_airport'] else None)
        if 'phone_number' in data:
            update_parts.append("phone_number = %s")
            update_vals.append(data['phone_number'])
        if 'sms_notifications' in data:
            update_parts.append("sms_notifications = %s")
            update_vals.append(bool(data['sms_notifications']))
        for col in ('notify_chat', 'notify_updates', 'notify_channel'):
            if col in data:
                update_parts.append(f"{col} = %s")
                update_vals.append(data[col])
        if update_parts:
            update_parts.append("updated_at = NOW()")
            update_vals.append(user_id)
            cursor.execute(f"UPDATE crab.users SET {', '.join(update_parts)} WHERE pk_id = %s", update_vals)
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update profile failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_user_tokens(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT google_access_token, google_refresh_token, calendar_synced
            FROM crab.users WHERE pk_id = %s
        """, (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get user tokens failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_user_tokens(user_id, access_token, refresh_token=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if refresh_token:
            cursor.execute("""
                UPDATE crab.users SET google_access_token = %s, google_refresh_token = %s, updated_at = NOW()
                WHERE pk_id = %s
            """, (access_token, refresh_token, user_id))
        else:
            cursor.execute("""
                UPDATE crab.users SET google_access_token = %s, updated_at = NOW()
                WHERE pk_id = %s
            """, (access_token, user_id))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update user tokens failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def set_user_calendar_synced(user_id, synced=True):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.users SET calendar_synced = %s, updated_at = NOW()
            WHERE pk_id = %s
        """, (synced, user_id))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Set calendar synced failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Plan CRUD ────────────────────────────────────────────────

def create_plan(organizer_id, data, invite_token):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.plans (organizer_id, title, description, timeframe, invite_token,
                travel_window_start, travel_window_end, group_vibes, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'voting')
            RETURNING *
        """, (
            organizer_id,
            data['title'],
            data.get('description'),
            data.get('timeframe'),
            invite_token,
            data.get('travel_window_start'),
            data.get('travel_window_end'),
            data.get('group_vibes'),
        ))
        plan = cursor.fetchone()
        conn.commit()
        return dict(plan)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Create plan failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plans_for_user(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT DISTINCT p.*, u.full_name as organizer_name, COUNT(m2.pk_id) as member_count
            FROM crab.plans p
            LEFT JOIN crab.users u ON u.pk_id = p.organizer_id
            LEFT JOIN crab.plan_members m ON m.plan_id = p.plan_id AND m.user_id = %s
            LEFT JOIN crab.plan_members m2 ON m2.plan_id = p.plan_id
            WHERE p.organizer_id = %s OR m.user_id = %s
            GROUP BY p.pk_id, u.full_name
            ORDER BY p.created_at DESC
        """, (user_id, user_id, user_id))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get plans failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_by_id(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT p.*, u.full_name as organizer_name
            FROM crab.plans p
            JOIN crab.users u ON u.pk_id = p.organizer_id
            WHERE p.plan_id = %s
        """, (plan_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get plan failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_by_invite_token(invite_token):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT p.*, u.full_name as organizer_name
            FROM crab.plans p
            JOIN crab.users u ON u.pk_id = p.organizer_id
            WHERE p.invite_token = %s
        """, (invite_token,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get plan by token failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Member CRUD ──────────────────────────────────────────────

def add_plan_member(plan_id, display_name, member_token, email=None, user_id=None, role='member'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.plan_members (plan_id, user_id, display_name, email, member_token, role)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING pk_id, plan_id, display_name, member_token, role
        """, (plan_id, user_id, display_name, email, member_token, role))
        member = cursor.fetchone()
        conn.commit()
        return dict(member)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Add member failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def remove_plan_member(plan_id, user_id):
    """Remove a member from a plan by user_id. Cascades to watches, availability, etc."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM crab.plan_members
            WHERE plan_id = %s AND user_id = %s AND role != 'organizer'
            RETURNING pk_id
        """, (plan_id, user_id))
        deleted = cursor.fetchone()
        conn.commit()
        return deleted is not None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Remove member failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_members(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT m.*, u.home_airport as user_home_airport
            FROM crab.plan_members m
            LEFT JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s ORDER BY m.joined_at
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get members failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_member_by_token(member_token):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.plan_members WHERE member_token = %s
        """, (member_token,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get member by token failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_member_for_plan(plan_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.plan_members
            WHERE plan_id = %s AND user_id = %s
        """, (plan_id, user_id))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get member for plan failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Preference CRUD ─────────────────────────────────────────

def get_plan_preferences(member_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.plan_preferences WHERE member_id = %s
        """, (member_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get preferences failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def upsert_plan_preferences(member_id, data):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab.plan_preferences (member_id, budget_min, budget_max,
                accommodation_style, dietary_needs, interests, mobility_notes,
                room_preference, notes, completed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (member_id) DO UPDATE SET
                budget_min = EXCLUDED.budget_min,
                budget_max = EXCLUDED.budget_max,
                accommodation_style = EXCLUDED.accommodation_style,
                dietary_needs = EXCLUDED.dietary_needs,
                interests = EXCLUDED.interests,
                mobility_notes = EXCLUDED.mobility_notes,
                room_preference = EXCLUDED.room_preference,
                notes = EXCLUDED.notes,
                completed = TRUE,
                updated_at = NOW()
        """, (
            member_id,
            data.get('budget_min'),
            data.get('budget_max'),
            data.get('accommodation_style'),
            data.get('dietary_needs'),
            psycopg2.extras.Json(data.get('interests', [])),
            data.get('mobility_notes'),
            data.get('room_preference'),
            data.get('notes'),
        ))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Upsert preferences failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_all_plan_preferences(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT COALESCE(u.full_name, m.display_name) as display_name,
                   m.pk_id as member_id, m.role, m.user_id,
                   u.home_airport,
                   p.budget_min, p.budget_max, p.accommodation_style,
                   p.dietary_needs, p.interests, p.mobility_notes,
                   p.room_preference, p.notes, p.completed
            FROM crab.plan_members m
            LEFT JOIN crab.users u ON u.pk_id = m.user_id
            LEFT JOIN crab.plan_preferences p ON p.member_id = m.pk_id
            WHERE m.plan_id = %s
            ORDER BY m.joined_at
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get all preferences failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Re-export splits for backwards-compatible imports ===
# Downstream code imports many things from utilities.postgres_utils — these
# re-exports keep the public surface intact while splits live in separate
# files for kumori 1000-line compliance.

from utilities.postgres_schema import *      # noqa: E402,F401,F403
from utilities.postgres_plans import *       # noqa: E402,F401,F403
from utilities.postgres_search import *      # noqa: E402,F401,F403
from utilities.postgres_analytics import *   # noqa: E402,F401,F403
