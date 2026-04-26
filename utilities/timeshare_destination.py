"""
Destination-level narrative: a short "why this place is interesting" blurb
cached per-country. Beats II's generic marketing copy because it speaks to
what families actually care about — when to go, what to eat, what
island/region for what vibe, how to spend a week.

Routed through the kumori free-LLM router (no paid Anthropic).
"""
import hashlib
import json
import logging

logger = logging.getLogger('crab_travel.timeshare_destination')


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
    """One free-LLM call per country. Cached in crab.ii_country_blurb (via the
    caller). Returns clean text — no formatting. Returns '' on free-pool failure."""
    areas_hint = ''
    if areas:
        areas_hint = f"\n\nResort areas in this destination: {', '.join(areas[:12])}"
    full_prompt = (
        f"{SYSTEM}\n\n"
        f"Destination: {country}.{areas_hint}\n\nWrite the briefing."
    )
    try:
        from utilities.kumori_free_llms import generate as _kfl_generate
        text, _backend = _kfl_generate(full_prompt, max_tokens=400,
                                        temperature=0.7,
                                        caller='timeshare_destination_blurb')
        return (text or '').strip()
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
