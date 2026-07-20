"""Flight price history — permanent daily rollup + raw-table retention.

PROBLEM: kumori_ops.flight_hunter_observations grows ~49K rows/day (every VPS
scan of every route-date, every adapter) and was never pruned — 1.2M rows /
595MB in 24 days, unbounded, on the shared db-f1-micro all 20 apps share.

SOLUTION (two tables, two lifetimes):
  1. flight_hunter_observations (raw) — fine-grained, every observation. Now
     RETAINED for RAW_RETENTION_DAYS only (live readers need ≤48h: jobs-to-run
     freshness = 6h, /explore = 48h). Capped, no longer unbounded.
  2. kumori_ops.flight_price_daily (this module) — ONE row per
     (origin, destination, depart_date, observed_date) with that day's price
     summary. Kept FOREVER. ~294 route-dates/day → ~107K rows/year, tiny.

The daily rollup collapses the hourly raw obs into a per-day summary BEFORE the
raw rows are pruned, so the long-term seasonality signal survives. From the
permanent table you can answer the questions raw data is too bulky to keep for:

  -- "the 2nd week of June is usually low" (cheapest departure weeks for a route)
  SELECT EXTRACT(week FROM depart_date) AS depart_week,
         ROUND(AVG(min_price_usd)) AS avg_floor, MIN(min_price_usd) AS best
  FROM kumori_ops.flight_price_daily
  WHERE origin='SEA' AND destination='MIA'
  GROUP BY 1 ORDER BY avg_floor;

  -- "February is a good month to book" (booking-month, i.e. observed_date)
  SELECT EXTRACT(month FROM observed_date) AS book_month,
         ROUND(AVG(min_price_usd)) AS avg_floor
  FROM kumori_ops.flight_price_daily
  WHERE origin='SEA' AND destination='MIA'
  GROUP BY 1 ORDER BY avg_floor;

  -- "book N days ahead" (lead time = depart_date - observed_date)
  SELECT (depart_date - observed_date) AS lead_days,
         ROUND(AVG(min_price_usd)) AS avg_floor
  FROM kumori_ops.flight_price_daily
  WHERE origin='SEA' AND destination='MIA' AND depart_date > observed_date
  GROUP BY 1 ORDER BY lead_days;

These shapes are served pre-baked by GET /api/flight-history/route.
"""
import logging
from datetime import datetime, timedelta

import psycopg2.extras
from flask import Blueprint, jsonify, request

from utilities.postgres_utils import get_db_connection
from utilities.ensure_once import ensure_once

logger = logging.getLogger('crab_travel.flight_price_history')

bp = Blueprint('flight_price_history', __name__)

# Raw observations older than this are pruned AFTER they're rolled up. 30 days
# is generous slack over the longest live reader window (48h /explore) while
# capping the raw table at ~1.5M rows / ~730MB steady-state instead of unbounded.
RAW_RETENTION_DAYS = 30


@ensure_once
def ensure_flight_price_daily(conn):
    """Permanent per-day price-summary table. Idempotent; once per process.

    crab_app is a guest in the shared kumori_ops schema (full DML, no CREATE).
    Postgres runs the schema ACL_CREATE check BEFORE the IF-NOT-EXISTS
    short-circuit, so even a no-op CREATE ... IF NOT EXISTS makes the engine LOG
    'permission denied for schema kumori_ops' (which the cross-project error
    digest reports). Skip the bootstrap DDL entirely when the table already
    exists — to_regclass is a silent existence check needing only schema USAGE.
    """
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('kumori_ops.flight_price_daily')")
    if cur.fetchone()[0] is None:
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kumori_ops.flight_price_daily (
                  origin           TEXT NOT NULL,
                  destination      TEXT NOT NULL,
                  depart_date      DATE NOT NULL,
                  observed_date    DATE NOT NULL,
                  min_price_usd    NUMERIC(10,2),
                  avg_price_usd    NUMERIC(10,2),
                  p25_price_usd    NUMERIC(10,2),
                  median_price_usd NUMERIC(10,2),
                  n_obs            INTEGER,
                  n_sources        INTEGER,
                  cheapest_source  TEXT,
                  PRIMARY KEY (origin, destination, depart_date, observed_date)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_fpd_route_depart
                  ON kumori_ops.flight_price_daily (origin, destination, depart_date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_fpd_observed
                  ON kumori_ops.flight_price_daily (observed_date)
            """)
            conn.commit()
        except psycopg2.errors.InsufficientPrivilege:
            conn.rollback()  # fresh DB, superuser will provision; one log line until then
    cur.close()
    logger.info("kumori_ops.flight_price_daily ready")


def _rollup_and_prune(conn):
    """Roll every raw observed-day into flight_price_daily, then prune old raw.

    The rollup is a set-based UPSERT over raw rows grouped by (route,
    depart_date, observed_at::date) — incremental AND day-sliced: it walks one
    observed-day per statement, from the last rolled observed_date (that day
    was partial when last rolled, so it re-rolls; ON CONFLICT refreshes it) to
    today. Bounded statements are the point: the original whole-table GROUP BY
    re-rolled all ~1.5M capped raw rows every run and hit the 30s statement
    timeout on the shared f1-micro — and even a multi-day catch-up window can
    blow it, since one raw day is ~30-50K rows. Self-backfilling (empty daily
    table → starts at MIN(observed_at)) and self-healing (cron gaps roll
    forward day by day).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(
                 (SELECT MAX(observed_date) FROM kumori_ops.flight_price_daily),
                 (SELECT MIN(observed_at)::date FROM kumori_ops.flight_hunter_observations)),
               CURRENT_DATE
    """)
    start_day, today = cur.fetchone()
    rolled = 0
    if start_day is not None:
        day = start_day
        while day <= today:
            cur.execute("""
                INSERT INTO kumori_ops.flight_price_daily AS d
                  (origin, destination, depart_date, observed_date,
                   min_price_usd, avg_price_usd, p25_price_usd, median_price_usd,
                   n_obs, n_sources, cheapest_source)
                SELECT origin, destination, depart_date, observed_at::date,
                       MIN(price_usd),
                       ROUND(AVG(price_usd), 2),
                       ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY price_usd)::numeric, 2),
                       ROUND(percentile_cont(0.5)  WITHIN GROUP (ORDER BY price_usd)::numeric, 2),
                       COUNT(*), COUNT(DISTINCT source),
                       (array_agg(source ORDER BY price_usd ASC))[1]
                FROM kumori_ops.flight_hunter_observations
                WHERE price_usd IS NOT NULL
                  AND observed_at >= %s AND observed_at < %s + INTERVAL '1 day'
                GROUP BY origin, destination, depart_date, observed_at::date
                ON CONFLICT (origin, destination, depart_date, observed_date)
                DO UPDATE SET
                   min_price_usd    = EXCLUDED.min_price_usd,
                   avg_price_usd    = EXCLUDED.avg_price_usd,
                   p25_price_usd    = EXCLUDED.p25_price_usd,
                   median_price_usd = EXCLUDED.median_price_usd,
                   n_obs            = EXCLUDED.n_obs,
                   n_sources        = EXCLUDED.n_sources,
                   cheapest_source  = EXCLUDED.cheapest_source
            """, (day, day))
            rolled += cur.rowcount
            conn.commit()   # each day's slice lands independently
            day += timedelta(days=1)

    # Prune in bounded slices for the same reason the rollup is day-sliced: a
    # whole-day DELETE (~30-60K rows x 7 index updates) blew the 30s statement
    # timeout on the shared f1-micro every day at 09:00 PT. Each slice is its
    # own statement + commit, so no single statement can hit the wall.
    pruned = 0
    while True:
        cur.execute("""
            DELETE FROM kumori_ops.flight_hunter_observations
            WHERE id IN (SELECT id FROM kumori_ops.flight_hunter_observations
                         WHERE observed_at < NOW() - make_interval(days => %s)
                         LIMIT 5000)
        """, (RAW_RETENTION_DAYS,))
        pruned += cur.rowcount
        conn.commit()
        if cur.rowcount < 5000:
            break

    cur.execute("SELECT COUNT(*) FROM kumori_ops.flight_price_daily")
    hist_rows = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM kumori_ops.flight_hunter_observations")
    raw_rows = cur.fetchone()[0]
    cur.close()
    return {'rolled_up': rolled, 'pruned_raw': pruned,
            'history_rows': hist_rows, 'raw_rows_remaining': raw_rows}


@bp.route('/cron/rollup-flight-prices')
def cron_rollup_flight_prices():
    """Daily: summarize raw flight observations into the permanent history table,
    then prune raw older than RAW_RETENTION_DAYS. App Engine cron (set-based SQL,
    runs in seconds — not a scrape, safe on the web tier)."""
    if not request.headers.get('X-Appengine-Cron') and request.args.get('force') != '1':
        return jsonify({'error': 'Cron only (append ?force=1 to test manually)'}), 403
    conn = get_db_connection()
    try:
        ensure_flight_price_daily(conn)
        stats = _rollup_and_prune(conn)
    finally:
        conn.close()
    logger.info(f"rollup-flight-prices: {stats}")
    return jsonify({'ok': True, 'retention_days': RAW_RETENTION_DAYS, **stats}), 200


@bp.route('/api/flight-history/route')
def api_flight_history_route():
    """Seasonality rollups for one route from the permanent history table.

    GET /api/flight-history/route?origin=SEA&destination=MIA
    Returns cheapest-by-departure-week, by-departure-month, by-booking-month, and
    by-lead-time — the 'when is it cheap / when should I book' views.
    """
    origin = (request.args.get('origin') or '').strip().upper()
    destination = (request.args.get('destination') or '').strip().upper()
    if not (3 <= len(origin) <= 4) or not (3 <= len(destination) <= 4):
        return jsonify({'error': 'origin and destination required (IATA)'}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        base = ("FROM kumori_ops.flight_price_daily "
                "WHERE origin=%s AND destination=%s")
        args = (origin, destination)

        def rows(sql):
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

        depart_week = rows(
            f"SELECT EXTRACT(week FROM depart_date)::int AS depart_week, "
            f"ROUND(AVG(min_price_usd)) AS avg_floor, MIN(min_price_usd) AS best, "
            f"COUNT(*) AS n {base} GROUP BY 1 ORDER BY 1")
        depart_month = rows(
            f"SELECT EXTRACT(month FROM depart_date)::int AS depart_month, "
            f"ROUND(AVG(min_price_usd)) AS avg_floor, MIN(min_price_usd) AS best, "
            f"COUNT(*) AS n {base} GROUP BY 1 ORDER BY 1")
        booking_month = rows(
            f"SELECT EXTRACT(month FROM observed_date)::int AS book_month, "
            f"ROUND(AVG(min_price_usd)) AS avg_floor, COUNT(*) AS n {base} "
            f"GROUP BY 1 ORDER BY 1")
        lead_time = rows(
            f"SELECT (depart_date - observed_date) AS lead_days, "
            f"ROUND(AVG(min_price_usd)) AS avg_floor, COUNT(*) AS n {base} "
            f"AND depart_date > observed_date GROUP BY 1 ORDER BY 1")
        cur.close()
    finally:
        conn.close()

    return jsonify({
        'origin': origin, 'destination': destination,
        'by_depart_week': depart_week,
        'by_depart_month': depart_month,
        'by_booking_month': booking_month,
        'by_lead_time_days': lead_time,
    }), 200
