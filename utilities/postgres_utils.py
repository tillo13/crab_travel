import logging
import os
import threading
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
        pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=10,
            dbname=creds['dbname'], user=creds['user'],
            password=creds['password'], host=host
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
                self._pool.putconn(self._conn)
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
    conn = pool.getconn()
    return PooledConnection(conn, pool)


# ── Schema + Tables ──────────────────────────────────────────

def init_database():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("CREATE SCHEMA IF NOT EXISTS crab")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.users (
                pk_id SERIAL PRIMARY KEY,
                google_id VARCHAR(255) UNIQUE NOT NULL,
                email VARCHAR(255) NOT NULL,
                full_name VARCHAR(255),
                picture_url TEXT,
                home_location VARCHAR(500),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.user_profiles (
                pk_id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES crab.users(pk_id) ON DELETE CASCADE,
                interests JSONB DEFAULT '[]',
                dietary_needs TEXT,
                mobility_notes TEXT,
                travel_style VARCHAR(50),
                accommodation_preference VARCHAR(50),
                budget_comfort VARCHAR(20),
                bio TEXT,
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.plans (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                organizer_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
                plan_type VARCHAR(30) NOT NULL DEFAULT 'trip',
                title VARCHAR(255) NOT NULL,
                destination VARCHAR(500),
                start_date DATE,
                end_date DATE,
                headcount INTEGER,
                description TEXT,
                invite_token VARCHAR(64) UNIQUE NOT NULL,
                status VARCHAR(20) DEFAULT 'planning',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plans_organizer ON crab.plans(organizer_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plans_invite_token ON crab.plans(invite_token)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.plan_members (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES crab.users(pk_id),
                display_name VARCHAR(255) NOT NULL,
                email VARCHAR(255),
                member_token VARCHAR(64) UNIQUE NOT NULL,
                role VARCHAR(20) DEFAULT 'member',
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plan_members_plan ON crab.plan_members(plan_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plan_members_token ON crab.plan_members(member_token)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.plan_preferences (
                pk_id SERIAL PRIMARY KEY,
                member_id INTEGER NOT NULL UNIQUE REFERENCES crab.plan_members(pk_id) ON DELETE CASCADE,
                budget_min INTEGER,
                budget_max INTEGER,
                accommodation_style VARCHAR(50),
                dietary_needs TEXT,
                interests JSONB DEFAULT '[]',
                mobility_notes TEXT,
                room_preference VARCHAR(50),
                notes TEXT,
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.recommendations (
                pk_id SERIAL PRIMARY KEY,
                recommendation_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                category VARCHAR(50) NOT NULL,
                title VARCHAR(500) NOT NULL,
                description TEXT,
                price_estimate VARCHAR(100),
                compatibility_score INTEGER,
                ai_reasoning TEXT,
                status VARCHAR(20) DEFAULT 'suggested',
                generated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_plan ON crab.recommendations(plan_id)")

        conn.commit()
        logger.info("✅ Database initialized")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Database init failed: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── User CRUD ────────────────────────────────────────────────

def upsert_user(google_userinfo):
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
            RETURNING pk_id, google_id, email, full_name, picture_url
        """, (
            google_userinfo.get('sub'),
            google_userinfo.get('email'),
            google_userinfo.get('name'),
            google_userinfo.get('picture'),
        ))
        user = cursor.fetchone()
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
            SELECT u.*, p.interests, p.dietary_needs, p.mobility_notes,
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
        # Update home_location on users table
        if data.get('home_location'):
            cursor.execute("""
                UPDATE crab.users SET home_location = %s, updated_at = NOW()
                WHERE pk_id = %s
            """, (data['home_location'], user_id))
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


# ── Plan CRUD ────────────────────────────────────────────────

def create_plan(organizer_id, data, invite_token):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.plans (organizer_id, plan_type, title, destination,
                start_date, end_date, headcount, description, invite_token)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING plan_id, title, invite_token
        """, (
            organizer_id,
            data.get('plan_type', 'trip'),
            data['title'],
            data.get('destination'),
            data.get('start_date') or None,
            data.get('end_date') or None,
            data.get('headcount') or None,
            data.get('description'),
            invite_token,
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
            SELECT p.*, COUNT(m.pk_id) as member_count
            FROM crab.plans p
            LEFT JOIN crab.plan_members m ON m.plan_id = p.plan_id
            WHERE p.organizer_id = %s
            GROUP BY p.pk_id
            ORDER BY p.created_at DESC
        """, (user_id,))
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


def get_plan_members(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.plan_members
            WHERE plan_id = %s ORDER BY joined_at
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
            SELECT m.display_name, m.pk_id as member_id, m.role,
                   p.budget_min, p.budget_max, p.accommodation_style,
                   p.dietary_needs, p.interests, p.mobility_notes,
                   p.room_preference, p.notes, p.completed
            FROM crab.plan_members m
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


# ── Recommendation CRUD ─────────────────────────────────────

def save_recommendations(plan_id, recs):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for r in recs:
            cursor.execute("""
                INSERT INTO crab.recommendations (plan_id, category, title, description,
                    price_estimate, compatibility_score, ai_reasoning)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                plan_id, r['category'], r['title'], r.get('description'),
                r.get('price_estimate'), r.get('compatibility_score'),
                r.get('ai_reasoning'),
            ))
        conn.commit()
        logger.info(f"💾 Saved {len(recs)} recommendations for plan {plan_id}")
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save recommendations failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_recommendations(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.recommendations
            WHERE plan_id = %s
            ORDER BY category, compatibility_score DESC
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get recommendations failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_recommendation_status(recommendation_id, status):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.recommendations SET status = %s
            WHERE recommendation_id = %s
        """, (status, recommendation_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update recommendation status failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_recommendations_for_plan(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.recommendations WHERE plan_id = %s", (plan_id,))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete recommendations failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
