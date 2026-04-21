"""
Destination-level narrative: Claude generates a short "why this place is
interesting" blurb cached per-country. Beats II's generic marketing copy
because it speaks to what families actually care about — when to go,
what to eat, what island/region for what vibe, how to spend a week.
"""
import hashlib
import json
import logging
import time

import requests

from utilities.claude_utils import _get_api_key, log_api_usage

logger = logging.getLogger('crab_travel.timeshare_destination')
MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"


SYSTEM = """You write concise, grounded destination briefings for a timeshare
exchange portal. Your audience is American families (50s-70s) weighing
whether to swap their home resort week for this place.

Rules:
- 2 short paragraphs max, ~120 words total.
- Paragraph 1: what the place IS — geography, vibe, why it's worth a week.
- Paragraph 2: when to go + one concrete "watch for" tip (hurricane season, festivals, transfer time from airport, etc.)
- No marketing fluff. No "discover the wonder of...". No lists with bullets.
- Use plain language — name the airport, name the beach, name the month.
- If you don't know, say so. Never invent facts."""


def fetch_blurb(country: str, areas: list[str] = None) -> str:
    """One Claude call per country. Cached in crab.ii_country_blurb (via the
    caller). Returns clean text — no formatting."""
    areas_hint = ''
    if areas:
        areas_hint = f"\n\nResort areas in this destination: {', '.join(areas[:12])}"
    body = {
        'model': MODEL,
        'max_tokens': 400,
        'system': SYSTEM,
        'messages': [{'role': 'user', 'content':
            f"Destination: {country}.{areas_hint}\n\nWrite the briefing."}],
    }
    t0 = time.time()
    try:
        r = requests.post(
            API_URL,
            headers={'x-api-key': _get_api_key(),
                     'anthropic-version': '2023-06-01',
                     'content-type': 'application/json'},
            json=body, timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        log_api_usage(MODEL, data.get('usage', {}),
                      feature='timeshare_destination_blurb',
                      duration_ms=int((time.time() - t0) * 1000))
        blocks = data.get('content', [])
        text = '\n\n'.join(b.get('text', '') for b in blocks if b.get('type') == 'text').strip()
        return text
    except Exception as e:
        logger.warning(f"destination blurb for {country!r} failed: {e}")
        return ''


def ensure_country_blurb_table(cur):
    """Idempotent DDL — tiny side table so we don't re-hit Claude every
    time a user opens a destination page."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crab.ii_country_blurb (
            country VARCHAR(200) PRIMARY KEY,
            blurb TEXT,
            model VARCHAR(100),
            cost_usd NUMERIC(8,5),
            generated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def get_or_generate_blurb(country: str, areas: list[str] = None) -> str:
    """Read cache, generate if missing, return the blurb string."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure_country_blurb_table(cur)
        cur.execute("SELECT blurb FROM crab.ii_country_blurb WHERE country = %s", (country,))
        row = cur.fetchone()
        if row and row[0]:
            return row[0]

        blurb = fetch_blurb(country, areas=areas)
        if not blurb:
            return ''
        cur.execute("""
            INSERT INTO crab.ii_country_blurb (country, blurb, model, generated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (country) DO UPDATE SET
                blurb = EXCLUDED.blurb,
                model = EXCLUDED.model,
                generated_at = NOW()
        """, (country, blurb, MODEL))
        conn.commit()
        return blurb
    finally:
        conn.close()
