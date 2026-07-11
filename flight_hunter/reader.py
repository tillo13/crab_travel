"""Flight Hunter — read side.

The OpenClaw VPS is the ONLY thing that scrapes flights. It writes every
observation to ``kumori_ops.flight_hunter_observations``. App Engine never
scrapes — it *decides* what's due (``jobs_to_run``) and *reads* what the VPS
already found.

This module is that read side, shared by:
  - ``flight_hunter_routes.jobs_to_run`` — uses :func:`active_watch_routes` to add
    Andy's live watch routes to the static ROUTE_PAIRS grid.
  - ``watches_routes.api_watch_explore`` — uses :func:`latest_observations_for_route`
    to answer "what are the current prices for this watch?" from the table instead
    of fanning out live scrapers on the 256 MB F1 web instance (the old OOM source).

Both queries are bounded index lookups (``idx_fho_route_date_obs`` =
``(origin, destination, depart_date, observed_at DESC)``). No full scans, no
scraping, nothing heavy on the web tier.
"""
import logging

import psycopg2.extras

logger = logging.getLogger(__name__)

# Freshness window for a price to count as "current" on the deal board. The VPS
# re-scans each route every MIN_HOURS_BETWEEN_OBS (6h), so 48h leaves comfortable
# slack for a missed cycle without ever surfacing a stale fare.
OBS_DISPLAY_FRESH_HOURS = 48


def _iata(v):
    return str(v).strip().upper() if v else None


def latest_observations_for_route(conn, origin, destination, depart_date,
                                  date_shift_days=0, fresh_hours=OBS_DISPLAY_FRESH_HOURS,
                                  limit=50):
    """Freshest observation per (source, depart_date) for one route, near a date.

    Reads ``kumori_ops.flight_hunter_observations`` (filled by the VPS scanner).
    Returns a list of result dicts shaped for ``/api/opencrab/watch-results`` and
    the plan-page deal board — sorted cheapest-first, capped at ``limit``. Empty
    list if nothing fresh exists yet (caller falls back to cached price).

    ``date_shift_days`` widens the depart-date window by ±N days (mirrors the old
    /explore ``date_shift_days`` knob). The route + narrow date window keeps this an
    index range scan, not a table scan.
    """
    origin = _iata(origin)
    destination = _iata(destination)
    if not origin or not destination or depart_date is None:
        return []
    shift = max(0, int(date_shift_days or 0))

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # DISTINCT ON (source, depart_date) + observed_at DESC → the most recent
        # observation per source per date in the window. idx_fho_route_date_obs
        # serves the (origin, destination, depart_date) range directly.
        cur.execute(
            """
            SELECT DISTINCT ON (source, depart_date)
                   source, origin, destination, depart_date, return_date,
                   price_usd, currency, airline, stops, duration_minutes,
                   deep_link, raw, observed_at
            FROM kumori_ops.flight_hunter_observations
            WHERE origin = %s
              AND destination = %s
              AND depart_date BETWEEN (%s::date - %s::int) AND (%s::date + %s::int)
              AND observed_at >= NOW() - make_interval(hours => %s)
            ORDER BY source, depart_date, observed_at DESC
            """,
            (origin, destination, depart_date, shift, depart_date, shift, int(fresh_hours)),
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    results = []
    for r in rows:
        raw = r.get('raw') if isinstance(r.get('raw'), dict) else {}
        depart_date_str = r['depart_date'].isoformat() if r.get('depart_date') else None
        results.append({
            'source': r['source'],
            'price_usd': round(float(r['price_usd']), 2),
            'currency': r.get('currency') or 'USD',
            'airline': r.get('airline'),
            'stops': r.get('stops'),
            'duration_minutes': r.get('duration_minutes'),
            'deep_link': r.get('deep_link'),
            'origin': r['origin'],
            'destination': r['destination'],
            'depart_date': depart_date_str,
            # depart_at/arrive_at aren't columns — surface them if the adapter
            # stashed them in raw (keeps the old /explore response shape intact).
            'depart_at': raw.get('depart_at') or raw.get('departure_time'),
            'arrive_at': raw.get('arrive_at') or raw.get('arrival_time'),
            'observed_at': r['observed_at'].isoformat() if r.get('observed_at') else None,
            # Stable identity for transport_options UPSERT dedup downstream.
            'canonical_key': f"{r['source']}:{r['origin']}-{r['destination']}:{depart_date_str}:{r.get('airline') or ''}",
        })

    results.sort(key=lambda x: x['price_usd'])
    return results[:limit]


def active_watch_routes(conn, home_origins, horizon_days):
    """Routes Andy is actively watching, scoped to home origins.

    Returns a list of ``(origin, destination, checkin)`` for active, non-bot flight
    watches whose trip is within ``horizon_days`` and whose origin is one Andy/Mom
    actually fly from (``home_origins``). Destinations must be valid 3-letter IATA —
    this skips the ~1.3K junk watches with raw city strings like ``'Scottsdale AZ'``.

    ``jobs_to_run`` prepends these ahead of the static ROUTE_PAIRS grid so real
    watches are scanned first, never starved behind Mom's proactive grid.
    """
    origins = sorted({_iata(o) for o in home_origins if o})
    if not origins:
        return []
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT DISTINCT mw.origin, mw.destination, mw.checkin
            FROM crab.member_watches mw
            JOIN crab.plans p ON p.plan_id = mw.plan_id
            WHERE mw.watch_type = 'flight'
              AND mw.status = 'active'
              AND COALESCE(p.title, '') NOT LIKE '[BOT]%%'
              AND mw.checkin IS NOT NULL
              AND mw.checkin BETWEEN CURRENT_DATE AND CURRENT_DATE + make_interval(days => %s)
              AND upper(mw.origin) = ANY(%s)
              AND mw.destination ~ '^[A-Z]{3}$'
            ORDER BY mw.checkin
            """,
            (int(horizon_days), origins),
        )
        return [(_iata(o), _iata(d), c) for (o, d, c) in cur.fetchall()]
    finally:
        cur.close()
