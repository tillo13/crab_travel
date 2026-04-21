"""
Short URL Service — ported from kumori/utilities/shorturl_utils.py (Apr 21 2026).

Purpose: shrink crab.travel share links (invite URLs, group dashboards,
timeshare UUIDs) down to `crab.travel/s/<code>`. Any feature emitting a
shareable link can shorten for free.

Table lives in the crab schema (crab.short_urls) on the shared kumori
Cloud SQL instance — matching crab's subsystem-prefix pattern.

Key differences from kumori port:
- Table: crab.short_urls (schema-qualified)
- Domain guard enforced at the route layer (only crab.travel URLs shortenable)
- Uses crab's get_db_connection() (no project-id arg)
- Normalizes crab.travel URLs to HTTPS on insert/lookup
"""

import logging
import random
from typing import Optional

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.shorturl')

ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
MIN_CODE_LENGTH = 2
MAX_CODE_LENGTH = 8


def _generate_random_code(length: int) -> str:
    return ''.join(random.choice(ALPHABET) for _ in range(length))


def _code_exists(cursor, code: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM crab.short_urls WHERE short_code = %s",
        (code,)
    )
    return cursor.fetchone() is not None


def _count_codes_at_length(cursor, length: int) -> int:
    cursor.execute(
        "SELECT COUNT(*) FROM crab.short_urls WHERE LENGTH(short_code) = %s",
        (length,)
    )
    result = cursor.fetchone()
    return result[0] if result else 0


def _max_codes_at_length(length: int) -> int:
    return len(ALPHABET) ** length


def generate_short_code(cursor, max_attempts: int = 50) -> Optional[str]:
    """Start at 2 chars; grow to 3+ when current length is ~80% full."""
    current_length = MIN_CODE_LENGTH
    while current_length <= MAX_CODE_LENGTH:
        used = _count_codes_at_length(cursor, current_length)
        max_possible = _max_codes_at_length(current_length)
        if used < max_possible * 0.8:
            break
        current_length += 1

    if current_length > MAX_CODE_LENGTH:
        logger.error("Short URL space exhausted!")
        return None

    for _ in range(max_attempts):
        code = _generate_random_code(current_length)
        if not _code_exists(cursor, code):
            return code

    if current_length < MAX_CODE_LENGTH:
        code = _generate_random_code(current_length + 1)
        if not _code_exists(cursor, code):
            return code

    logger.error(f"Failed to generate unique code after {max_attempts} attempts")
    return None


def create_short_url(long_url: str) -> Optional[str]:
    """Returns a short code (not the full URL). Idempotent — same long URL
    returns the same code on repeat calls."""
    try:
        if 'crab.travel' in long_url and long_url.startswith('http://'):
            long_url = long_url.replace('http://', 'https://', 1)

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT short_code FROM crab.short_urls WHERE long_url = %s",
            (long_url,)
        )
        existing = cursor.fetchone()
        if existing:
            cursor.close()
            conn.close()
            logger.info(f"URL already shortened: {existing[0]}")
            return existing[0]

        code = generate_short_code(cursor)
        if not code:
            cursor.close()
            conn.close()
            return None

        cursor.execute(
            """
            INSERT INTO crab.short_urls (short_code, long_url, created_at)
            VALUES (%s, %s, NOW())
            """,
            (code, long_url)
        )
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Created short URL: {code} -> {long_url[:80]}...")
        return code

    except Exception as e:
        logger.error(f"Error creating short URL: {e}")
        return None


def get_long_url(short_code: str) -> Optional[str]:
    """Lookup + fire-and-forget click count increment."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT long_url FROM crab.short_urls WHERE short_code = %s",
            (short_code,)
        )
        result = cursor.fetchone()

        if result:
            try:
                cursor.execute(
                    """
                    UPDATE crab.short_urls
                       SET click_count = COALESCE(click_count, 0) + 1
                     WHERE short_code = %s
                    """,
                    (short_code,)
                )
                conn.commit()
            except Exception:
                pass

        cursor.close()
        conn.close()
        return result[0] if result else None

    except Exception as e:
        logger.error(f"Error looking up short URL: {e}")
        return None


def ensure_table_exists():
    """Idempotent DDL — safe to call at every app startup. Matches kumori's
    ensure_table_exists() pattern."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crab.short_urls (
                id SERIAL PRIMARY KEY,
                short_code VARCHAR(10) UNIQUE NOT NULL,
                long_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                click_count INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_short_urls_code
                ON crab.short_urls(short_code)
        """)
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("crab.short_urls table ready")
        return True

    except Exception as e:
        logger.error(f"Error ensuring crab.short_urls table: {e}")
        return False
