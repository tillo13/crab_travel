"""
Database schema initialization for crab_travel.
Split from utilities/postgres_utils.py for kumori 1000-line compliance.
"""
import logging
import psycopg2
import psycopg2.extras

from utilities.postgres_utils import db_cursor, get_db_connection

logger = logging.getLogger(__name__)


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
            ('phone_number', 'VARCHAR(20)', None),
            ('sms_notifications', 'BOOLEAN', 'FALSE'),
            ('notify_chat', 'VARCHAR(10)', "'off'"),
            ('notify_updates', 'VARCHAR(10)', "'off'"),
            ('notify_channel', 'VARCHAR(10)', "'email'"),
            ('is_admin', 'BOOLEAN', 'FALSE'),
            ('subscription_tier', 'VARCHAR(20)', "'free'"),
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
            CREATE TABLE IF NOT EXISTS crab.member_tentative_dates (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
                date_start DATE NOT NULL,
                date_end DATE NOT NULL,
                UNIQUE(plan_id, user_id, date_start, date_end)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tentative_plan ON crab.member_tentative_dates(plan_id)")
        try:
            cursor.execute("ALTER TABLE crab.member_tentative_dates ADD COLUMN IF NOT EXISTS preference VARCHAR(20) DEFAULT 'works'")
        except Exception:
            pass

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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.invite_views (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES crab.users(pk_id),
                ip_address VARCHAR(45),
                user_agent TEXT,
                is_authenticated BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invite_views_plan ON crab.invite_views(plan_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.speed_test_runs (
                id SERIAL PRIMARY KEY,
                tested_by INTEGER REFERENCES crab.users(pk_id),
                results JSONB NOT NULL,
                slowest_page VARCHAR(200),
                slowest_time FLOAT,
                all_ok BOOLEAN DEFAULT TRUE,
                tested_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # ── Bot testing tables ──
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.bot_runs (
                run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                started_at TIMESTAMPTZ DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                status TEXT DEFAULT 'running',
                mode TEXT,
                plan_id UUID,
                phases_passed INT DEFAULT 0,
                phases_failed INT DEFAULT 0,
                phases_warned INT DEFAULT 0,
                summary JSONB
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.bot_events (
                event_id BIGSERIAL PRIMARY KEY,
                run_id UUID REFERENCES crab.bot_runs(run_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                phase TEXT,
                bot_name TEXT,
                action TEXT,
                status TEXT,
                detail JSONB
            )
        """)

        # ── LLM Telemetry ──
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.llm_calls (
                pk_id BIGSERIAL PRIMARY KEY,
                backend VARCHAR(30) NOT NULL,
                model VARCHAR(100),
                prompt_length INTEGER,
                response_length INTEGER,
                duration_ms INTEGER,
                success BOOLEAN DEFAULT TRUE,
                error_message TEXT,
                caller VARCHAR(50),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_backend ON crab.llm_calls(backend, created_at)")

        # ── Member Watch Tables ──
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.member_watches (
                pk_id BIGSERIAL PRIMARY KEY,
                plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                member_id INTEGER NOT NULL REFERENCES crab.plan_members(pk_id) ON DELETE CASCADE,
                watch_type VARCHAR(20) NOT NULL,
                origin VARCHAR(10),
                destination VARCHAR(100) NOT NULL,
                checkin DATE,
                checkout DATE,
                budget_max INTEGER,
                status VARCHAR(20) DEFAULT 'active',
                best_price_usd NUMERIC(10,2),
                best_price_at TIMESTAMPTZ,
                last_price_usd NUMERIC(10,2),
                last_checked_at TIMESTAMPTZ,
                alert_threshold_pct INTEGER DEFAULT 10,
                deep_link TEXT,
                data JSONB DEFAULT '{}'
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_member_watches_plan ON crab.member_watches(plan_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_member_watches_status ON crab.member_watches(status)")
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_member_watches_unique
            ON crab.member_watches(plan_id, member_id, watch_type, COALESCE(origin, ''))
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.watch_history (
                pk_id BIGSERIAL PRIMARY KEY,
                watch_id BIGINT NOT NULL REFERENCES crab.member_watches(pk_id) ON DELETE CASCADE,
                price_usd NUMERIC(10,2) NOT NULL,
                source VARCHAR(50) NOT NULL,
                deep_link TEXT,
                data JSONB DEFAULT '{}',
                observed_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_watch_history_watch ON crab.watch_history(watch_id, observed_at)")

        # Notification dedupe — used by vote reminders today, extensible later
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.notifications_sent (
                pk_id SERIAL PRIMARY KEY,
                plan_id UUID REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES crab.users(pk_id) ON DELETE CASCADE,
                notification_type VARCHAR(50) NOT NULL,
                channel VARCHAR(10) NOT NULL,
                sent_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_sent_plan_type_day ON crab.notifications_sent (plan_id, notification_type, (sent_at::date))")

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
