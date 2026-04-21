"""
Timeshare schema bootstrap — idempotent DDL called at Flask app startup.

Matches the kumori ensure_table_exists() pattern (§1.6 of the plan doc):
every CREATE is `IF NOT EXISTS`, safe to re-run on every deploy, self-
healing on a fresh DB, no-op on a live one. No separate migration tool.

Phase 1 scope: the two group-core tables. Phases 2+ will extend this
module with the remaining 19 tables + `crab.plans.timeshare_group_id`
FK + the `crab.ii_*` catalog tables.
"""

import logging

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.timeshare_schema')


def _ensure_timeshare_groups():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.timeshare_groups (
                pk_id SERIAL PRIMARY KEY,
                group_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                name VARCHAR(200) NOT NULL,
                created_by INTEGER NOT NULL REFERENCES crab.users(pk_id),
                is_public BOOLEAN DEFAULT FALSE,
                drive_folder_url TEXT,
                status VARCHAR(20) DEFAULT 'active',
                settings JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeshare_groups_created_by
                ON crab.timeshare_groups(created_by)
        """)
        conn.commit()
    finally:
        conn.close()


def _ensure_timeshare_group_members():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.timeshare_group_members (
                pk_id SERIAL PRIMARY KEY,
                group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES crab.users(pk_id),
                email VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'family',
                invite_token VARCHAR(64) UNIQUE,
                invited_by INTEGER REFERENCES crab.users(pk_id),
                invited_at TIMESTAMPTZ DEFAULT NOW(),
                accepted_at TIMESTAMPTZ,
                chat_daily_count INTEGER DEFAULT 0,
                chat_daily_reset_at TIMESTAMPTZ DEFAULT NOW(),
                settings JSONB DEFAULT '{}'::jsonb,
                UNIQUE(group_id, email)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeshare_gmembers_group
                ON crab.timeshare_group_members(group_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeshare_gmembers_user
                ON crab.timeshare_group_members(user_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeshare_gmembers_invite
                ON crab.timeshare_group_members(invite_token)
        """)
        conn.commit()
    finally:
        conn.close()


def init_timeshare_schema():
    """Idempotent — safe to call at every app startup."""
    try:
        _ensure_timeshare_groups()
        _ensure_timeshare_group_members()
        logger.info("crab.timeshare_* (Phase 1) tables ready")
        return True
    except Exception as e:
        logger.error(f"Error ensuring timeshare schema: {e}")
        return False
