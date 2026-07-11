"""
Area context schema bootstrap — idempotent DDL called at Flask app startup.

Stores raw multi-source observations + LLM-synthesized per-resort summaries
for the /area/<slug> explorer pages.

Sources fed in by OpenClaw VPS skills via /api/opencrab/area-observations
(guardrailed, bearer-auth, allowlisted, rate-capped — see opencrab_routes.py).
"""

import logging
from utilities.postgres_utils import get_db_connection
from utilities.ensure_once import ensure_once

logger = logging.getLogger('crab_travel.area_schema')


@ensure_once
def _ensure_area_observations(cur):
    """Raw scraped snippets from independent sources (Reddit, TUG, YouTube, news).

    One row per (source, source_id) — natural-key dedup so we can re-scrape
    safely. Resort linkage is fuzzy at scrape time (LLM-validated later)
    so resort_pid is nullable.
    """
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crab.area_observations (
          obs_id         BIGSERIAL PRIMARY KEY,
          area_slug      VARCHAR(40)  NOT NULL,        -- 'hawaii'
          resort_pid     VARCHAR(10),                  -- e.g. 'P4793' or NULL
          source         VARCHAR(20)  NOT NULL,        -- 'reddit'/'tug'/'youtube'/'news'
          source_id      VARCHAR(120) NOT NULL,        -- thread id, video id, article id
          source_url     TEXT,
          source_sub     VARCHAR(60),                  -- subreddit, forum section, channel
          author         VARCHAR(80),
          title          TEXT,
          text           TEXT,
          content_hash   VARCHAR(64),                  -- sha256(norm title + first 200 of text) — dedup key
          posted_at      TIMESTAMPTZ,
          score          INTEGER,                       -- upvotes / views / etc.
          raw_json       JSONB,
          scraped_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          scraped_by     VARCHAR(40),                  -- which VPS skill emitted
          UNIQUE (source, source_id)
        )
    """)
    cur.execute("""
        ALTER TABLE crab.area_observations
          ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_area_obs_content_hash
          ON crab.area_observations (content_hash) WHERE content_hash IS NOT NULL
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_area_obs_area_resort
          ON crab.area_observations (area_slug, resort_pid)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_area_obs_scraped_at
          ON crab.area_observations (scraped_at DESC)
    """)


@ensure_once
def _ensure_area_synthesis(cur):
    """LLM-synthesized per-resort summaries.

    One row per (area_slug, resort_pid, model_version). Latest row per
    (area_slug, resort_pid) is what the UI surfaces.
    """
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crab.area_synthesis (
          syn_id         BIGSERIAL PRIMARY KEY,
          area_slug      VARCHAR(40)  NOT NULL,
          resort_pid     VARCHAR(10)  NOT NULL,
          model          VARCHAR(60),                  -- 'claude-haiku-4-5' etc.
          n_observations INTEGER,                       -- how many snippets fed in
          summary_json   JSONB NOT NULL,                -- {praise, complaints, avoid_weeks, nearby, best_for}
          computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (area_slug, resort_pid, model, computed_at)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_area_syn_area_resort
          ON crab.area_synthesis (area_slug, resort_pid, computed_at DESC)
    """)


def init_area_schema():
    """Idempotent — safe to call at every app startup."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_area_observations(cur)
        _ensure_area_synthesis(cur)
        conn.commit()
        logger.info("crab.area_observations + crab.area_synthesis tables ready")
        return True
    except Exception as e:
        logger.error(f"Error ensuring area schema: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()
