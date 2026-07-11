"""Smoke tests for the flight-hunter read side (jobs_to_run + /explore source).

Covers the OOM/timeout rewrite (2026-05-30):
  1. active_watch_routes() only emits HOME_ORIGINS origins + valid IATA dests
     (the geographic scope that excludes the ~3.4K bot/junk watches).
  2. The unnest()+LEFT JOIN staleness query (jobs_to_run) returns one row per
     candidate tuple and stays FAR under the 30s statement_timeout that the old
     ANY()×ANY() cross-product scan was hitting.
  3. latest_observations_for_route() returns the right shape, cheapest-first,
     and [] for a route with no fresh data (so /explore degrades gracefully).

Run: python tests/test_flight_hunter_jobs.py
"""

import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flight_hunter.config import ROUTE_PAIRS, HOME_ORIGINS, SCAN_HORIZON_DAYS
from flight_hunter.reader import active_watch_routes, latest_observations_for_route
from utilities.postgres_utils import get_db_connection

PASS, FAIL = "✅", "❌"
IATA = re.compile(r'^[A-Z]{3}$')

# jobs_to_run's statement_timeout is 30s; the old query was hitting it. Anything
# over this ceiling means the rewrite regressed.
STALENESS_QUERY_CEILING_S = 10.0


def ok(cond, label):
    print(f"  {PASS if cond else FAIL} {label}")
    if not cond:
        sys.exit(1)


def test_active_watch_routes_scoped(conn):
    print("test_active_watch_routes_scoped")
    routes = active_watch_routes(conn, HOME_ORIGINS, SCAN_HORIZON_DAYS)
    ok(isinstance(routes, list), "returns a list")
    for o, d, ci in routes:
        ok(o in HOME_ORIGINS, f"origin {o} is a home origin")
        ok(bool(IATA.match(d)), f"destination {d} is valid 3-letter IATA")
        ok(ci is not None and ci >= date.today(), f"{o}->{d} checkin {ci} is future")
    print(f"     ({len(routes)} active home-origin routes)")


def test_staleness_query_bounded(conn):
    print("test_staleness_query_bounded")
    today = date.today()
    earliest, horizon = today + timedelta(days=2), today + timedelta(days=SCAN_HORIZON_DAYS)

    wc = []
    for o, d, ci in active_watch_routes(conn, HOME_ORIGINS, SCAN_HORIZON_DAYS):
        for s in range(-3, 4):
            dt = ci + timedelta(days=s)
            if earliest <= dt <= horizon:
                wc.append((o, d, dt))
    grid_dates = [today + timedelta(days=i) for i in range(2, SCAN_HORIZON_DAYS, 3)]
    gc = [(o, d, dt) for (o, d, _l, _f) in ROUTE_PAIRS for dt in grid_dates]
    seen, cands = set(), []
    for c in wc + gc:
        if c not in seen:
            seen.add(c)
            cands.append(c)
    ok(len(cands) > 0, "built candidate set")

    co = [c[0] for c in cands]
    cd = [c[1] for c in cands]
    cdt = [c[2] for c in cands]
    cur = conn.cursor()
    t = time.time()
    cur.execute("""
        SELECT c.origin, c.destination, c.depart_date, MAX(f.observed_at)
        FROM unnest(%s::text[], %s::text[], %s::date[])
             AS c(origin, destination, depart_date)
        LEFT JOIN kumori_ops.flight_hunter_observations f
               ON f.origin = c.origin
              AND f.destination = c.destination
              AND f.depart_date = c.depart_date
        GROUP BY c.origin, c.destination, c.depart_date
    """, (co, cd, cdt))
    rows = cur.fetchall()
    elapsed = time.time() - t
    cur.close()
    ok(len(rows) == len(cands), f"one row per candidate ({len(rows)}=={len(cands)})")
    ok(elapsed < STALENESS_QUERY_CEILING_S,
       f"ran in {elapsed*1000:.0f}ms (< {STALENESS_QUERY_CEILING_S*1000:.0f}ms ceiling)")


def test_reader_shape(conn):
    print("test_reader_shape")
    today = date.today()
    # A grid route that the scanner has been hitting for weeks → should have data.
    res = latest_observations_for_route(conn, 'HLN', 'SEA', today + timedelta(days=14),
                                        date_shift_days=3, limit=20)
    ok(isinstance(res, list), "returns a list")
    if res:
        prices = [r['price_usd'] for r in res]
        ok(prices == sorted(prices), "results are cheapest-first")
        r0 = res[0]
        for key in ('source', 'price_usd', 'deep_link', 'origin', 'destination',
                    'depart_date', 'canonical_key'):
            ok(key in r0, f"result has '{key}'")
    else:
        print("     (no fresh HLN->SEA obs in window — skipping shape asserts)")

    # A route the scanner has never touched → empty, never an exception.
    empty = latest_observations_for_route(conn, 'ZZZ', 'QQQ', today + timedelta(days=14))
    ok(empty == [], "unknown route returns [] (graceful /explore fallback)")


def main():
    conn = get_db_connection()
    try:
        test_active_watch_routes_scoped(conn)
        test_staleness_query_bounded(conn)
        test_reader_shape(conn)
    finally:
        conn.close()
    print(f"\n{PASS} all flight-hunter jobs smoke tests passed")


if __name__ == "__main__":
    main()
