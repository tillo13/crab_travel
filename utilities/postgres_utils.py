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
        # Budget: 50 max_connections shared across 8+ apps on db-f1-micro
        pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=3,
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
                home_airport VARCHAR(10),
                google_access_token TEXT,
                google_refresh_token TEXT,
                calendar_synced BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migrate existing tables — add new columns if missing
        for col, col_type, default in [
            ('home_airport', 'VARCHAR(10)', None),
            ('google_access_token', 'TEXT', None),
            ('google_refresh_token', 'TEXT', None),
            ('calendar_synced', 'BOOLEAN', 'FALSE'),
        ]:
            try:
                default_clause = f" DEFAULT {default}" if default else ""
                cursor.execute(f"ALTER TABLE crab.users ADD COLUMN IF NOT EXISTS {col} {col_type}{default_clause}")
            except Exception:
                pass

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
                timeframe VARCHAR(30),
                locked_destination VARCHAR(500),
                locked_start_date DATE,
                locked_end_date DATE,
                invite_token VARCHAR(64) UNIQUE NOT NULL,
                status VARCHAR(20) DEFAULT 'planning',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        for col, col_type in [
            ('timeframe', 'VARCHAR(30)'),
            ('locked_destination', 'VARCHAR(500)'),
            ('locked_start_date', 'DATE'),
            ('locked_end_date', 'DATE'),
        ]:
            try:
                cursor.execute(f"ALTER TABLE crab.plans ADD COLUMN IF NOT EXISTS {col} {col_type}")
            except Exception:
                pass
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
            CREATE TABLE IF NOT EXISTS crab.member_availability (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
                available_start DATE NOT NULL,
                available_end DATE NOT NULL,
                source VARCHAR(20) DEFAULT 'calendar',
                UNIQUE(plan_id, user_id, available_start, available_end)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_member_avail_plan ON crab.member_availability(plan_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.destination_suggestions (
                pk_id SERIAL PRIMARY KEY,
                suggestion_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                suggested_by INTEGER REFERENCES crab.users(pk_id),
                destination_name VARCHAR(500) NOT NULL,
                destination_data JSONB DEFAULT '{}',
                avg_flight_cost INTEGER,
                avg_hotel_cost INTEGER,
                avg_total_cost INTEGER,
                compatibility_score INTEGER,
                status VARCHAR(20) DEFAULT 'researching',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dest_suggestions_plan ON crab.destination_suggestions(plan_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.votes (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
                target_type VARCHAR(30) NOT NULL,
                target_id VARCHAR(100) NOT NULL,
                vote SMALLINT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(plan_id, user_id, target_type, target_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_votes_plan ON crab.votes(plan_id)")

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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.search_results (
                pk_id BIGSERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                result_type VARCHAR(20) NOT NULL,
                source VARCHAR(50) NOT NULL,
                canonical_key TEXT,
                title TEXT,
                price_usd NUMERIC(10,2),
                deep_link TEXT,
                data JSONB NOT NULL DEFAULT '{}',
                found_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_results_plan ON crab.search_results(plan_id, result_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_results_found ON crab.search_results(plan_id, found_at)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.price_history (
                pk_id BIGSERIAL PRIMARY KEY,
                result_type VARCHAR(20) NOT NULL,
                canonical_key TEXT NOT NULL,
                source VARCHAR(50) NOT NULL,
                price_usd NUMERIC(10,2) NOT NULL,
                travel_date DATE,
                observed_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_key ON crab.price_history(canonical_key, result_type, observed_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_date ON crab.price_history(travel_date, canonical_key)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.deals_cache (
                deal_key        TEXT PRIMARY KEY,
                source          VARCHAR(50) NOT NULL,
                deal_type       VARCHAR(20) NOT NULL,
                origin          VARCHAR(10),
                destination     VARCHAR(100),
                destination_name TEXT,
                title           TEXT,
                airline         VARCHAR(100),
                price_per_person NUMERIC(10,2) NOT NULL,
                lowest_price_seen NUMERIC(10,2) NOT NULL,
                price_unit      VARCHAR(20) DEFAULT 'person',
                depart_date     TEXT,
                deep_link       TEXT,
                bookable        BOOLEAN DEFAULT FALSE,
                first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
                seen_count      INTEGER DEFAULT 1
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deals_cache_source ON crab.deals_cache(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deals_cache_origin ON crab.deals_cache(origin)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deals_cache_price ON crab.deals_cache(price_per_person)")

        # ── Phase 0 migrations: blackouts, member airport/flexible, travel window ──
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.member_blackouts (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
                blackout_start DATE NOT NULL,
                blackout_end DATE NOT NULL,
                UNIQUE(plan_id, user_id, blackout_start, blackout_end)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_blackouts_plan ON crab.member_blackouts(plan_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.messages (
                pk_id SERIAL PRIMARY KEY,
                message_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES crab.users(pk_id),
                parent_id UUID REFERENCES crab.messages(message_id) ON DELETE CASCADE,
                display_name VARCHAR(200) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_plan ON crab.messages(plan_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_parent ON crab.messages(parent_id)")

        for col, col_type, default in [
            ('home_airport', 'VARCHAR(10)', None),
            ('is_flexible', 'BOOLEAN', 'FALSE'),
        ]:
            try:
                default_clause = f" DEFAULT {default}" if default else ""
                cursor.execute(f"ALTER TABLE crab.plan_members ADD COLUMN IF NOT EXISTS {col} {col_type}{default_clause}")
            except Exception:
                pass

        for col, col_type in [
            ('travel_window_start', 'DATE'),
            ('travel_window_end', 'DATE'),
            ('group_vibes', 'TEXT'),
        ]:
            try:
                cursor.execute(f"ALTER TABLE crab.plans ADD COLUMN IF NOT EXISTS {col} {col_type}")
            except Exception:
                pass

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
            RETURNING pk_id, google_id, email, full_name, picture_url, home_airport
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
        # Update home_location and home_airport on users table
        if data.get('home_location') or data.get('home_airport'):
            update_parts = []
            update_vals = []
            if data.get('home_location'):
                update_parts.append("home_location = %s")
                update_vals.append(data['home_location'])
            if data.get('home_airport'):
                update_parts.append("home_airport = %s")
                update_vals.append(data['home_airport'].upper().strip())
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
                travel_window_start, travel_window_end, group_vibes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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


# ── Availability CRUD ──────────────────────────────────────

def save_member_availability(plan_id, user_id, windows, source='calendar'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Clear old entries for this source
        cursor.execute("""
            DELETE FROM crab.member_availability
            WHERE plan_id = %s AND user_id = %s AND source = %s
        """, (plan_id, user_id, source))
        for w in windows:
            cursor.execute("""
                INSERT INTO crab.member_availability (plan_id, user_id, available_start, available_end, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (plan_id, user_id, w['start'], w['end'], source))
        conn.commit()
        logger.info(f"💾 Saved {len(windows)} availability windows for user {user_id} in plan {plan_id}")
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save availability failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_availability(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT a.*, u.full_name, u.home_airport
            FROM crab.member_availability a
            JOIN crab.users u ON u.pk_id = a.user_id
            WHERE a.plan_id = %s
            ORDER BY a.available_start
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get availability failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_availability_overlap(plan_id):
    """Find date ranges where the most members are available."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Get all availability windows
        cursor.execute("""
            SELECT user_id, available_start, available_end
            FROM crab.member_availability
            WHERE plan_id = %s
        """, (plan_id,))
        rows = cursor.fetchall()
        if not rows:
            return []

        # Get total member count
        cursor.execute("SELECT COUNT(DISTINCT user_id) as total FROM crab.plan_members WHERE plan_id = %s", (plan_id,))
        total = cursor.fetchone()['total']

        # Build date→user count map
        from datetime import timedelta, date as date_type
        date_users = {}
        for r in rows:
            d = r['available_start']
            while d <= r['available_end']:
                if d not in date_users:
                    date_users[d] = set()
                date_users[d].add(r['user_id'])
                d += timedelta(days=1)

        if not date_users:
            return []

        # Find contiguous windows with counts
        sorted_dates = sorted(date_users.keys())
        windows = []
        window_start = sorted_dates[0]
        prev_date = sorted_dates[0]
        prev_count = len(date_users[sorted_dates[0]])

        for d in sorted_dates[1:]:
            count = len(date_users[d])
            if d == prev_date + timedelta(days=1) and count == prev_count:
                prev_date = d
            else:
                windows.append({
                    'start': window_start.isoformat(),
                    'end': prev_date.isoformat(),
                    'days': (prev_date - window_start).days + 1,
                    'available_count': prev_count,
                    'total_members': total,
                })
                window_start = d
                prev_date = d
                prev_count = count

        windows.append({
            'start': window_start.isoformat(),
            'end': prev_date.isoformat(),
            'days': (prev_date - window_start).days + 1,
            'available_count': prev_count,
            'total_members': total,
        })

        # Sort by most people available, then longest duration
        windows.sort(key=lambda w: (-w['available_count'], -w['days']))
        return windows
    except Exception as e:
        logger.error(f"❌ Get availability overlap failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Destination Suggestion CRUD ─────────────────────────────

def create_destination_suggestion(plan_id, user_id, destination_name):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.destination_suggestions (plan_id, suggested_by, destination_name)
            VALUES (%s, %s, %s)
            RETURNING suggestion_id, destination_name, status
        """, (plan_id, user_id, destination_name))
        row = cursor.fetchone()
        conn.commit()
        return dict(row)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Create destination suggestion failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_destination_suggestion(suggestion_id, data):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.destination_suggestions SET
                destination_data = %s, avg_flight_cost = %s, avg_hotel_cost = %s,
                avg_total_cost = %s, compatibility_score = %s, status = %s
            WHERE suggestion_id = %s
        """, (
            psycopg2.extras.Json(data.get('destination_data', {})),
            data.get('avg_flight_cost'),
            data.get('avg_hotel_cost'),
            data.get('avg_total_cost'),
            data.get('compatibility_score'),
            data.get('status', 'ready'),
            suggestion_id,
        ))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update destination suggestion failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_destination_suggestions(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT d.*, u.full_name as suggested_by_name
            FROM crab.destination_suggestions d
            LEFT JOIN crab.users u ON u.pk_id = d.suggested_by
            WHERE d.plan_id = %s
            ORDER BY d.created_at
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get destination suggestions failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_destination_suggestion_by_id(suggestion_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM crab.destination_suggestions WHERE suggestion_id = %s", (suggestion_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get suggestion failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_destination_data(suggestion_id, destination_data):
    """Update only the destination_data JSONB field without touching other columns."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE crab.destination_suggestions SET destination_data = %s WHERE suggestion_id = %s",
            (psycopg2.extras.Json(destination_data), suggestion_id),
        )
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update destination data failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_destination_suggestion(suggestion_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Delete related votes first
        cursor.execute("DELETE FROM crab.votes WHERE target_type = 'destination' AND target_id = %s", (str(suggestion_id),))
        cursor.execute("DELETE FROM crab.destination_suggestions WHERE suggestion_id = %s", (suggestion_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete destination suggestion failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Vote CRUD ──────────────────────────────────────────────

def upsert_vote(plan_id, user_id, target_type, target_id, vote):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab.votes (plan_id, user_id, target_type, target_id, vote)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (plan_id, user_id, target_type, target_id) DO UPDATE SET
                vote = EXCLUDED.vote, created_at = NOW()
        """, (plan_id, user_id, target_type, target_id, vote))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Upsert vote failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_vote_tallies(plan_id, target_type=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = """
            SELECT target_type, target_id,
                   SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END) as upvotes,
                   SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END) as downvotes,
                   SUM(vote) as score
            FROM crab.votes
            WHERE plan_id = %s
        """
        params = [plan_id]
        if target_type:
            sql += " AND target_type = %s"
            params.append(target_type)
        sql += " GROUP BY target_type, target_id ORDER BY score DESC"
        cursor.execute(sql, params)
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get vote tallies failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_user_votes(plan_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT target_type, target_id, vote
            FROM crab.votes
            WHERE plan_id = %s AND user_id = %s
        """, (plan_id, user_id))
        return {f"{r['target_type']}:{r['target_id']}": r['vote'] for r in cursor.fetchall()}
    except Exception as e:
        logger.error(f"❌ Get user votes failed: {e}")
        return {}
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def lock_plan(plan_id, destination, start_date=None, end_date=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.plans SET
                locked_destination = %s, locked_start_date = %s, locked_end_date = %s,
                status = 'locked', updated_at = NOW()
            WHERE plan_id = %s
        """, (destination, start_date, end_date, plan_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Lock plan failed: {e}")
        return False
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


# ── Search Results CRUD ──────────────────────────────────────

def save_search_result(plan_id, result_type, source, data, canonical_key=None, title=None, price_usd=None, deep_link=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.search_results
                (plan_id, result_type, source, canonical_key, title, price_usd, deep_link, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING pk_id, found_at
        """, (
            plan_id, result_type, source, canonical_key,
            title, price_usd, deep_link,
            psycopg2.extras.Json(data),
        ))
        row = cursor.fetchone()
        conn.commit()
        return dict(row)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save search result failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_search_results(plan_id, result_type=None, since_id=0, limit=200):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = """
            SELECT pk_id, result_type, source, canonical_key, title, price_usd, deep_link, data, found_at
            FROM crab.search_results
            WHERE plan_id = %s AND pk_id > %s
        """
        params = [plan_id, since_id]
        if result_type:
            sql += " AND result_type = %s"
            params.append(result_type)
        sql += " ORDER BY pk_id ASC LIMIT %s"
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d['found_at'] = d['found_at'].isoformat() if d['found_at'] else None
            d['price_usd'] = float(d['price_usd']) if d['price_usd'] else None
            results.append(d)
        return results
    except Exception as e:
        logger.error(f"❌ Get search results failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def clear_search_results(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.search_results WHERE plan_id = %s", (plan_id,))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Clear search results failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def save_price_history(result_type, canonical_key, source, price_usd, travel_date=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab.price_history (result_type, canonical_key, source, price_usd, travel_date)
            VALUES (%s, %s, %s, %s, %s)
        """, (result_type, canonical_key, source, price_usd, travel_date))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save price history failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def upsert_deals_cache(deals):
    """
    Upsert a list of deal dicts into crab.deals_cache.
    deal_key = source:deal_type:origin:destination (unique per route/service).
    Tracks lowest_price_seen, last_seen_at, seen_count automatically.
    """
    if not deals:
        return 0
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        count = 0
        for d in deals:
            key = f"{d.get('source')}:{d.get('deal_type')}:{d.get('origin', '')}:{d.get('destination', '')}"
            cursor.execute("""
                INSERT INTO crab.deals_cache (
                    deal_key, source, deal_type, origin, destination, destination_name,
                    title, airline, price_per_person, lowest_price_seen, price_unit,
                    depart_date, deep_link, bookable
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (deal_key) DO UPDATE SET
                    price_per_person  = EXCLUDED.price_per_person,
                    lowest_price_seen = LEAST(crab.deals_cache.lowest_price_seen, EXCLUDED.price_per_person),
                    depart_date       = EXCLUDED.depart_date,
                    deep_link         = EXCLUDED.deep_link,
                    title             = EXCLUDED.title,
                    airline           = EXCLUDED.airline,
                    last_seen_at      = NOW(),
                    seen_count        = crab.deals_cache.seen_count + 1
            """, (
                key,
                d.get('source'), d.get('deal_type'),
                d.get('origin'), d.get('destination'), d.get('destination_name'),
                d.get('title'), d.get('airline'),
                d.get('price_per_person'), d.get('price_per_person'),
                d.get('price_unit', 'person'),
                d.get('depart_date'), d.get('deep_link'),
                d.get('bookable', False),
            ))
            count += 1
        conn.commit()
        logger.info(f"💾 Upserted {count} deals to cache")
        return count
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ upsert_deals_cache failed: {e}")
        return 0
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_deals_cache_grouped(origin=None):
    """
    Read deals from cache grouped by source, sorted by price.
    If origin provided, filter flight deals to that origin (hotels/activities are global).
    Returns list of tab dicts matching the deals_engine grouped format.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if origin:
            cursor.execute("""
                SELECT *, (NOW() - last_seen_at) AS age
                FROM crab.deals_cache
                WHERE (origin = %s OR origin IS NULL)
                ORDER BY source, price_per_person ASC
            """, (origin.upper(),))
        else:
            cursor.execute("""
                SELECT *, (NOW() - last_seen_at) AS age
                FROM crab.deals_cache
                ORDER BY source, price_per_person ASC
            """)

        rows = cursor.fetchall()
        buckets = {}
        latest_seen_at = None
        for r in rows:
            d = dict(r)
            d['price_per_person'] = float(d['price_per_person'])
            d['lowest_price_seen'] = float(d['lowest_price_seen'])
            lsa = d.pop('age', None)  # remove timedelta — not JSON serializable
            # track the most recent last_seen_at across all rows
            if d.get('last_seen_at'):
                ts = d['last_seen_at'].isoformat() if hasattr(d['last_seen_at'], 'isoformat') else str(d['last_seen_at'])
                if latest_seen_at is None or ts > latest_seen_at:
                    latest_seen_at = ts
                d['last_seen_at'] = ts
            buckets.setdefault(d['source'], []).append(d)

        TAB_ORDER = [
            ("travelpayouts",       "✈️ Aviasales Specials"),
            ("travelpayouts_cheap", "✈️ Aviasales All Flights"),
            ("duffel",              "✈️ Duffel Flights"),
            ("liteapi",             "🏨 LiteAPI Hotels"),
            ("viator",              "🎟️ Viator Activities"),
        ]
        tabs = []
        for src_key, label in TAB_ORDER:
            deals = buckets.get(src_key, [])
            if deals:
                tabs.append({"key": src_key, "label": f"{label} ({len(deals)})", "deals": deals})
        return {"tabs": tabs, "last_updated": latest_seen_at}
    except Exception as e:
        logger.error(f"❌ get_deals_cache_grouped failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_price_average(canonical_key, result_type, days=90):
    """90-day average price for a route/property — the deal detection baseline."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT AVG(price_usd) as avg_price, COUNT(*) as sample_count
            FROM crab.price_history
            WHERE canonical_key = %s
              AND result_type = %s
              AND observed_at > NOW() - INTERVAL '%s days'
        """, (canonical_key, result_type, days))
        row = cursor.fetchone()
        if row and row[0]:
            return {'avg_price': float(row[0]), 'sample_count': row[1]}
        return None
    except Exception as e:
        logger.error(f"❌ Get price average failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Blackout CRUD ──────────────────────────────────────────

def save_member_blackouts(plan_id, user_id, blackouts):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.member_blackouts WHERE plan_id = %s AND user_id = %s", (plan_id, user_id))
        for b in blackouts:
            cursor.execute("""
                INSERT INTO crab.member_blackouts (plan_id, user_id, blackout_start, blackout_end)
                VALUES (%s, %s, %s, %s)
            """, (plan_id, user_id, b['start'], b['end']))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save blackouts failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_blackouts(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT b.*, u.full_name
            FROM crab.member_blackouts b
            JOIN crab.users u ON u.pk_id = b.user_id
            WHERE b.plan_id = %s
            ORDER BY b.blackout_start
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get blackouts failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_member_blackouts(plan_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT blackout_start, blackout_end
            FROM crab.member_blackouts
            WHERE plan_id = %s AND user_id = %s
            ORDER BY blackout_start
        """, (plan_id, user_id))
        return [{'start': r['blackout_start'].isoformat(), 'end': r['blackout_end'].isoformat()} for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get member blackouts failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_plan(plan_id, organizer_id):
    """Delete a plan and all related data (CASCADE handles children)."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM crab.plans WHERE plan_id = %s AND organizer_id = %s",
            (plan_id, organizer_id)
        )
        deleted = cursor.rowcount
        conn.commit()
        logger.info(f"🗑️ Plan deleted: {plan_id} (rows={deleted})")
        return deleted > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete plan failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_member_details(member_id, home_airport=None, is_flexible=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        parts = []
        vals = []
        if home_airport is not None:
            parts.append("home_airport = %s")
            vals.append(home_airport.upper().strip() if home_airport else None)
        if is_flexible is not None:
            parts.append("is_flexible = %s")
            vals.append(is_flexible)
        if not parts:
            return True
        vals.append(member_id)
        cursor.execute(f"UPDATE crab.plan_members SET {', '.join(parts)} WHERE pk_id = %s", vals)
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update member details failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Messages CRUD ──────────────────────────────────────────

def create_message(plan_id, user_id, display_name, content, parent_id=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.messages (plan_id, user_id, display_name, content, parent_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (plan_id, user_id, display_name, content, parent_id))
        msg = cursor.fetchone()
        conn.commit()
        return dict(msg) if msg else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Create message failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_messages(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT m.*, u.picture as user_picture
            FROM crab.messages m
            LEFT JOIN crab.users u ON m.user_id = u.pk_id
            WHERE m.plan_id = %s
            ORDER BY m.created_at ASC
        """, (plan_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"❌ Get messages failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_message(message_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.messages WHERE message_id = %s AND user_id = %s", (message_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete message failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
