"""
travel_agent schema — the 5 tables + thin writers.

Cloud (shared kumori Cloud SQL, crab schema; ta_ prefix keeps ownership obvious
next to the crab app's own tables):
    crab.ta_trips, crab.ta_bookings, crab.ta_receipts

LOCAL ONLY — raw card data never leaves this machine (personal_finance rule):
    travel_agent/_private/cards.db (SQLite): card_transactions, reconciliations
Only matched/sanitized results may ever sync up; raw statements never do.

Run from repo root:  venv_crab/bin/python -m travel_agent.schema
"""

import json
import logging
import os
import sqlite3

from utilities.ensure_once import ensure_once
from utilities.postgres_utils import get_db_connection

logger = logging.getLogger(__name__)

_PRIVATE_DIR = os.path.join(os.path.dirname(__file__), "_private")
LOCAL_DB_PATH = os.path.join(_PRIVATE_DIR, "cards.db")

PAYERS = ("client_wrapped", "llc", "personal")
CATEGORIES = ("meals", "lodging", "air", "ground", "other")


# ── Cloud tables ──────────────────────────────────────────────

@ensure_once
def ensure_cloud_tables():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.ta_trips (
                pk_id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                destination_city VARCHAR(120),
                origin_airport VARCHAR(8),
                dest_airport VARCHAR(8),
                meeting_address TEXT,
                date_start DATE NOT NULL,
                date_end DATE NOT NULL,
                home_metro VARCHAR(60),
                payer VARCHAR(30) NOT NULL DEFAULT 'personal'
                    CHECK (payer IN ('client_wrapped','llc','personal')),
                status VARCHAR(30) DEFAULT 'planning',
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.ta_bookings (
                pk_id SERIAL PRIMARY KEY,
                trip_id INTEGER NOT NULL REFERENCES crab.ta_trips(pk_id) ON DELETE CASCADE,
                kind VARCHAR(20) NOT NULL,
                vendor VARCHAR(120),
                conf_number VARCHAR(60),
                amount_native NUMERIC(12,2),
                currency VARCHAR(8) DEFAULT 'USD',
                amount_usd NUMERIC(12,2),
                source_email_id VARCHAR(64),
                details JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ta_bookings_trip ON crab.ta_bookings(trip_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.ta_receipts (
                pk_id SERIAL PRIMARY KEY,
                trip_id INTEGER NOT NULL REFERENCES crab.ta_trips(pk_id) ON DELETE CASCADE,
                source VARCHAR(20) DEFAULT 'email',
                gcs_path TEXT,
                merchant VARCHAR(200),
                amount_native NUMERIC(12,2),
                currency VARCHAR(8) DEFAULT 'USD',
                amount_usd NUMERIC(12,2),
                category VARCHAR(20) DEFAULT 'other'
                    CHECK (category IN ('meals','lodging','air','ground','other')),
                captured_at TIMESTAMPTZ,
                extracted_json JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ta_receipts_trip ON crab.ta_receipts(trip_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.ta_boards (
                pk_id SERIAL PRIMARY KEY,
                token VARCHAR(32) NOT NULL UNIQUE,
                trip_id INTEGER REFERENCES crab.ta_trips(pk_id) ON DELETE SET NULL,
                title VARCHAR(200),
                html TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        logger.info("travel_agent cloud tables ready (crab.ta_trips / ta_bookings / ta_receipts)")
    finally:
        conn.close()


# ── Local card-side tables (SQLite, never leaves the machine) ─

_local_ddl_done = False


def _connect_local():
    os.makedirs(_PRIVATE_DIR, exist_ok=True)
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def local_db():
    # SQLite on the local machine (NOT the shared Cloud SQL); DDL once per process.
    global _local_ddl_done
    if _local_ddl_done:
        return _connect_local()
    _local_ddl_done = True
    conn = _connect_local()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS card_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_source TEXT DEFAULT 'card',
            date TEXT NOT NULL,
            merchant_raw TEXT NOT NULL,
            merchant_clean TEXT,
            city TEXT,
            state TEXT,
            amount REAL NOT NULL,
            trip_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_txn_id INTEGER REFERENCES card_transactions(id),
            receipt_id INTEGER,
            match_confidence REAL,
            status TEXT CHECK (status IN
                ('matched','orphan_txn','orphan_receipt','excluded_home_metro'))
        );
    """)
    return conn


# ── Writers / readers ─────────────────────────────────────────

def create_trip(name, destination_city, date_start, date_end, dest_airport=None,
                origin_airport=None, meeting_address=None, payer="personal", notes=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.ta_trips
                (name, destination_city, origin_airport, dest_airport,
                 meeting_address, date_start, date_end, payer, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING pk_id
        """, (name, destination_city, origin_airport, dest_airport,
              meeting_address, date_start, date_end, payer, notes))
        trip_id = cur.fetchone()[0]
        conn.commit()
        return trip_id
    finally:
        conn.close()


def add_booking(trip_id, kind, vendor=None, conf_number=None, amount_native=None,
                currency="USD", amount_usd=None, source_email_id=None, details=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.ta_bookings
                (trip_id, kind, vendor, conf_number, amount_native, currency,
                 amount_usd, source_email_id, details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING pk_id
        """, (trip_id, kind, vendor, conf_number, amount_native, currency,
              amount_usd, source_email_id, json.dumps(details or {})))
        booking_id = cur.fetchone()[0]
        conn.commit()
        return booking_id
    finally:
        conn.close()


def add_receipt(trip_id, merchant, amount_native, currency="USD", amount_usd=None,
                category="other", source="email", gcs_path=None, captured_at=None,
                extracted_json=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.ta_receipts
                (trip_id, source, gcs_path, merchant, amount_native, currency,
                 amount_usd, category, captured_at, extracted_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING pk_id
        """, (trip_id, source, gcs_path, merchant, amount_native, currency,
              amount_usd, category, captured_at, json.dumps(extracted_json or {})))
        receipt_id = cur.fetchone()[0]
        conn.commit()
        return receipt_id
    finally:
        conn.close()


def get_trip(trip_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM crab.ta_trips WHERE pk_id = %s", (trip_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))
    finally:
        conn.close()


def list_bookings(trip_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM crab.ta_bookings WHERE trip_id = %s ORDER BY pk_id", (trip_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def list_receipts(trip_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM crab.ta_receipts WHERE trip_id = %s ORDER BY captured_at NULLS LAST, pk_id", (trip_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def publish_board(title, html, trip_id=None, token=None):
    """Store a shareable trip board; content lives in the DB (never the public
    repo). Returns the token. Re-publish with the same token to update in place.
    Write path is this local tool only — there is deliberately NO web endpoint
    that accepts HTML."""
    import secrets
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if token:
            cur.execute("""UPDATE crab.ta_boards SET html=%s, title=%s, updated_at=NOW()
                           WHERE token=%s RETURNING token""", (html, title, token))
            row = cur.fetchone()
            if row:
                conn.commit()
                return row[0]
        token = token or secrets.token_urlsafe(12)
        cur.execute("""INSERT INTO crab.ta_boards (token, trip_id, title, html)
                       VALUES (%s,%s,%s,%s) RETURNING token""", (token, trip_id, title, html))
        token = cur.fetchone()[0]
        conn.commit()
        return token
    finally:
        conn.close()


def get_board(token):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, html FROM crab.ta_boards WHERE token = %s", (token,))
        row = cur.fetchone()
        return {"title": row[0], "html": row[1]} if row else None
    finally:
        conn.close()


def list_trips():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.pk_id, t.name, t.destination_city, t.date_start, t.date_end,
                   t.payer, t.status,
                   (SELECT COUNT(*) FROM crab.ta_bookings b WHERE b.trip_id = t.pk_id) AS bookings,
                   (SELECT COUNT(*) FROM crab.ta_receipts r WHERE r.trip_id = t.pk_id) AS receipts
            FROM crab.ta_trips t ORDER BY t.date_start DESC
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_cloud_tables()
    local_db().close()
    print(f"cloud tables ensured; local card DB at {LOCAL_DB_PATH}")
    for t in list_trips():
        print(t)
