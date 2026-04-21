"""
Timeshare schema bootstrap — idempotent DDL called at Flask app startup.

Matches the kumori ensure_table_exists() pattern (§1.6 of the plan doc):
every CREATE is `IF NOT EXISTS`, every ALTER is `ADD COLUMN IF NOT EXISTS`,
safe to re-run on every deploy, self-healing on a fresh DB, no-op on a live
one. No separate migration tool.

Scope:
- Phase 1: groups + group_members (shipped 2026-04-21)
- Phase 2: properties, contracts, people, financial history, trips/exchanges,
  portals/contacts/document_refs/timeline, II catalog, group shortlist,
  ingest_jobs, chat conv/messages, audit log, plus ALTER crab.plans for
  the timeshare_cycle bridge FK.
"""

import logging

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.timeshare_schema')


def _run(cur, sql):
    cur.execute(sql)


# ── Phase 1: groups + members ────────────────────────────────────────

def _ensure_groups(cur):
    _run(cur, """
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
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_groups_created_by ON crab.timeshare_groups(created_by)")


def _ensure_group_members(cur):
    _run(cur, """
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
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_gmembers_group ON crab.timeshare_group_members(group_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_gmembers_user ON crab.timeshare_group_members(user_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_gmembers_invite ON crab.timeshare_group_members(invite_token)")


# ── Phase 2: property / contract / people ────────────────────────────

def _ensure_properties(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_properties (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            name VARCHAR(300) NOT NULL,
            developer VARCHAR(200),
            unit_number VARCHAR(50),
            unit_configuration VARCHAR(100),
            week_number INTEGER,
            usage_pattern VARCHAR(50),
            trust_expiry_date DATE,
            exchange_network VARCHAR(30),
            country VARCHAR(100),
            city VARCHAR(200),
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_props_group ON crab.timeshare_properties(group_id)")


def _ensure_contracts(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_contracts (
            pk_id SERIAL PRIMARY KEY,
            property_id INTEGER NOT NULL REFERENCES crab.timeshare_properties(pk_id) ON DELETE CASCADE,
            contract_number VARCHAR(100),
            purchase_date DATE,
            purchase_price_usd NUMERIC(10,2),
            down_payment_usd NUMERIC(10,2),
            financing_terms TEXT,
            co_owners TEXT,
            contract_external_url VARCHAR(1000),
            notes TEXT,
            source_ingest_job_id INTEGER
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_contracts_prop ON crab.timeshare_contracts(property_id)")


def _ensure_people(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_people (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            full_name VARCHAR(200) NOT NULL,
            preferred_name VARCHAR(100),
            relationship VARCHAR(200),
            email VARCHAR(255),
            phone VARCHAR(50),
            birth_date DATE,
            user_id INTEGER REFERENCES crab.users(pk_id),
            notes TEXT,
            source_ingest_job_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_people_group ON crab.timeshare_people(group_id)")


# ── Phase 2: financial ───────────────────────────────────────────────

def _ensure_maintenance_fees(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_maintenance_fees (
            pk_id SERIAL PRIMARY KEY,
            property_id INTEGER NOT NULL REFERENCES crab.timeshare_properties(pk_id) ON DELETE CASCADE,
            year INTEGER NOT NULL,
            billed_amount_usd NUMERIC(10,2),
            paid_amount_usd NUMERIC(10,2),
            billed_date DATE,
            paid_date DATE,
            late_fees_usd NUMERIC(10,2) DEFAULT 0,
            discount_usd NUMERIC(10,2) DEFAULT 0,
            components JSONB,
            currency CHAR(3) DEFAULT 'USD',
            notes TEXT,
            source_ingest_job_id INTEGER,
            UNIQUE(property_id, year)
        )
    """)


def _ensure_loan_payments(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_loan_payments (
            pk_id SERIAL PRIMARY KEY,
            contract_id INTEGER NOT NULL REFERENCES crab.timeshare_contracts(pk_id) ON DELETE CASCADE,
            payment_date DATE,
            amount_usd NUMERIC(10,2),
            principal_usd NUMERIC(10,2),
            interest_usd NUMERIC(10,2),
            balance_after_usd NUMERIC(10,2),
            method VARCHAR(50),
            notes TEXT,
            source_ingest_job_id INTEGER
        )
    """)


# ── Phase 2: trips + exchanges ───────────────────────────────────────

def _ensure_trips(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_trips (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            property_id INTEGER REFERENCES crab.timeshare_properties(pk_id),
            trip_date_start DATE,
            trip_date_end DATE,
            resort_name VARCHAR(300),
            resort_ii_code VARCHAR(10),
            resort_rci_code VARCHAR(10),
            location VARCHAR(300),
            trip_type VARCHAR(30),
            exchange_number VARCHAR(50),
            ii_member_confirmed BOOLEAN DEFAULT FALSE,
            rci_member_confirmed BOOLEAN DEFAULT FALSE,
            cost_usd NUMERIC(10,2),
            notes TEXT,
            uncertainty_level VARCHAR(20) DEFAULT 'confirmed',
            source_ingest_job_id INTEGER
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_trips_group ON crab.timeshare_trips(group_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_trips_date ON crab.timeshare_trips(trip_date_start)")


def _ensure_trip_participants(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_trip_participants (
            pk_id SERIAL PRIMARY KEY,
            trip_id INTEGER NOT NULL REFERENCES crab.timeshare_trips(pk_id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES crab.timeshare_people(pk_id) ON DELETE CASCADE,
            UNIQUE(trip_id, person_id)
        )
    """)


def _ensure_exchanges(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_exchanges (
            pk_id SERIAL PRIMARY KEY,
            property_id INTEGER NOT NULL REFERENCES crab.timeshare_properties(pk_id) ON DELETE CASCADE,
            network VARCHAR(30),
            deposit_date DATE,
            week_deposited INTEGER,
            exchange_date DATE,
            exchange_fee_usd NUMERIC(10,2),
            destination_resort VARCHAR(300),
            destination_resort_code VARCHAR(10),
            trip_id INTEGER REFERENCES crab.timeshare_trips(pk_id),
            status VARCHAR(30),
            notes TEXT,
            source_ingest_job_id INTEGER
        )
    """)


# ── Phase 2: portals / contacts / documents / timeline ───────────────

def _ensure_portals(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_portals (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            portal_name VARCHAR(200),
            url VARCHAR(500),
            username VARCHAR(200),
            encrypted_password_ref VARCHAR(200),
            member_number VARCHAR(100),
            support_phone VARCHAR(50),
            last_rotated DATE,
            notes TEXT,
            source_ingest_job_id INTEGER
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_portals_group ON crab.timeshare_portals(group_id)")


def _ensure_contacts(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_contacts (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            full_name VARCHAR(200),
            role VARCHAR(200),
            organization VARCHAR(200),
            email VARCHAR(255),
            phone VARCHAR(50),
            last_contacted DATE,
            notes TEXT,
            source_ingest_job_id INTEGER
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_contacts_group ON crab.timeshare_contacts(group_id)")


def _ensure_document_refs(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_document_refs (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            doc_type VARCHAR(50),
            title VARCHAR(500),
            external_url VARCHAR(1000),
            external_provider VARCHAR(30),
            external_id VARCHAR(200),
            date_on_document DATE,
            related_property_id INTEGER REFERENCES crab.timeshare_properties(pk_id),
            related_trip_id INTEGER REFERENCES crab.timeshare_trips(pk_id),
            notes TEXT,
            source_ingest_job_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_doc_refs_group ON crab.timeshare_document_refs(group_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_doc_refs_external ON crab.timeshare_document_refs(external_provider, external_id)")


def _ensure_timeline_events(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_timeline_events (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            event_date DATE,
            event_type VARCHAR(50),
            title VARCHAR(500),
            description TEXT,
            related_person_id INTEGER REFERENCES crab.timeshare_people(pk_id),
            related_property_id INTEGER REFERENCES crab.timeshare_properties(pk_id),
            related_contact_id INTEGER REFERENCES crab.timeshare_contacts(pk_id),
            source_ingest_job_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_timeline_group ON crab.timeshare_timeline_events(group_id)")


# ── Phase 2: II catalog + shortlist ──────────────────────────────────

def _ensure_ii_catalog(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.ii_regions (
            pk_id SERIAL PRIMARY KEY,
            ii_code INTEGER UNIQUE NOT NULL,
            name VARCHAR(200) NOT NULL,
            scraped_at TIMESTAMPTZ
        )
    """)
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.ii_areas (
            pk_id SERIAL PRIMARY KEY,
            region_id INTEGER REFERENCES crab.ii_regions(pk_id),
            ii_code INTEGER UNIQUE NOT NULL,
            name VARCHAR(300) NOT NULL,
            country VARCHAR(100),
            scraped_at TIMESTAMPTZ
        )
    """)
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.ii_resorts (
            pk_id SERIAL PRIMARY KEY,
            area_id INTEGER REFERENCES crab.ii_areas(pk_id),
            ii_code VARCHAR(10) UNIQUE NOT NULL,
            name VARCHAR(300) NOT NULL,
            address TEXT,
            phone VARCHAR(50),
            website VARCHAR(500),
            nearest_airport VARCHAR(200),
            check_in_day VARCHAR(20),
            sleeping_capacity JSONB,
            tdi_score INTEGER,
            rating_overall DECIMAL(2,1),
            rating_services DECIMAL(2,1),
            rating_property DECIMAL(2,1),
            rating_accommodations DECIMAL(2,1),
            rating_experience DECIMAL(2,1),
            rating_response_count INTEGER,
            description TEXT,
            amenities JSONB,
            photo_urls JSONB,
            map_lat DECIMAL(9,6),
            map_lng DECIMAL(9,6),
            scraped_at TIMESTAMPTZ
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_ii_resort_area ON crab.ii_resorts(area_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_ii_resort_rating ON crab.ii_resorts(rating_overall)")


def _ensure_group_shortlist(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_group_shortlist (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            network VARCHAR(30),
            resort_code VARCHAR(10),
            added_by INTEGER REFERENCES crab.users(pk_id),
            notes TEXT,
            priority INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(group_id, network, resort_code)
        )
    """)


# ── Phase 2: ingest + chat + audit ───────────────────────────────────

def _ensure_ingest_jobs(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_ingest_jobs (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            source_type VARCHAR(30),
            source_ref VARCHAR(500),
            source_snapshot_hash VARCHAR(64),
            source_content TEXT,
            status VARCHAR(30),
            extracted_facts JSONB,
            review_notes TEXT,
            rejected_rows JSONB,
            created_by INTEGER REFERENCES crab.users(pk_id),
            committed_by INTEGER REFERENCES crab.users(pk_id),
            committed_at TIMESTAMPTZ,
            claude_cost_usd NUMERIC(8,5),
            error_message TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_ingest_group ON crab.timeshare_ingest_jobs(group_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_ingest_status ON crab.timeshare_ingest_jobs(status, created_at)")


def _ensure_chat(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_chat_conversations (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES crab.users(pk_id),
            title VARCHAR(500),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_chat_messages (
            pk_id SERIAL PRIMARY KEY,
            conversation_id INTEGER REFERENCES crab.timeshare_chat_conversations(pk_id) ON DELETE CASCADE,
            role VARCHAR(20),
            content TEXT,
            model VARCHAR(100),
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd NUMERIC(8,5),
            cited_fact_refs JSONB,
            tool_calls JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_chat_conv ON crab.timeshare_chat_messages(conversation_id)")


def _ensure_audit_log(cur):
    _run(cur, """
        CREATE TABLE IF NOT EXISTS crab.timeshare_audit_log (
            pk_id SERIAL PRIMARY KEY,
            group_id UUID REFERENCES crab.timeshare_groups(group_id) ON DELETE SET NULL,
            user_id INTEGER REFERENCES crab.users(pk_id) ON DELETE SET NULL,
            action VARCHAR(100),
            resource_type VARCHAR(50),
            resource_id VARCHAR(200),
            metadata JSONB,
            ip_address VARCHAR(45),
            user_agent TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_audit_group ON crab.timeshare_audit_log(group_id)")
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_timeshare_audit_action ON crab.timeshare_audit_log(action)")


# ── Phase 2: bridge FK on crab.plans ─────────────────────────────────

def _ensure_plans_bridge(cur):
    _run(cur, """
        ALTER TABLE crab.plans
          ADD COLUMN IF NOT EXISTS timeshare_group_id UUID
          REFERENCES crab.timeshare_groups(group_id)
    """)
    _run(cur, "CREATE INDEX IF NOT EXISTS idx_plans_timeshare_group ON crab.plans(timeshare_group_id)")


# ── Entrypoint ───────────────────────────────────────────────────────

def init_timeshare_schema():
    """Idempotent — safe to call at every app startup."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Phase 1
        _ensure_groups(cur)
        _ensure_group_members(cur)
        # Phase 2 — in FK-dependency order
        _ensure_properties(cur)
        _ensure_contracts(cur)
        _ensure_people(cur)
        _ensure_maintenance_fees(cur)
        _ensure_loan_payments(cur)
        _ensure_trips(cur)
        _ensure_trip_participants(cur)
        _ensure_exchanges(cur)
        _ensure_portals(cur)
        _ensure_contacts(cur)
        _ensure_document_refs(cur)
        _ensure_timeline_events(cur)
        _ensure_ii_catalog(cur)
        _ensure_group_shortlist(cur)
        _ensure_ingest_jobs(cur)
        _ensure_chat(cur)
        _ensure_audit_log(cur)
        _ensure_plans_bridge(cur)
        conn.commit()
        logger.info("crab.timeshare_* (Phase 1+2) + crab.ii_* tables ready")
        return True
    except Exception as e:
        logger.error(f"Error ensuring timeshare schema: {e}")
        return False
    finally:
        conn.close()
