#!/usr/bin/env python3
"""
Local II catalog drainer — runs ENTIRELY off-App-Engine.

Writes directly to Cloud SQL via psycopg2 (same DB App Engine uses),
reusing utilities/timeshare_ii_scraper.py's HTTP-fetch + parsing helpers
but bypassing the Flask task endpoints. This decouples the crawl from
the web tier so heavy scraping can't starve user requests.

Usage:
    source venv_crab/bin/activate
    python dev/drain_ii.py                  # drain until queue empty
    python dev/drain_ii.py --one             # do exactly one region
    python dev/drain_ii.py --seed            # seed a new run if none pending

DB creds: pulled from GCP Secret Manager (same pattern as prod).
Scraper code: /Users/at/Desktop/code/crab_travel/utilities/timeshare_ii_scraper.py
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Swap get_db_connection out BEFORE the scraper module imports it — it loads
# creds from the crab venv's google.cloud.secretmanager which is fine here.
from utilities.timeshare_ii_scraper import (
    _crawl_region, fetch_regions, start_run,
)
from utilities.postgres_utils import get_db_connection


def _pending_count():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM crab.ii_scrape_queue
             WHERE status IN ('pending', 'running')
        """)
        return cur.fetchone()[0]
    finally:
        conn.close()


def _claim_one():
    """Pick next pending queue row. Returns (queue_pk, run_id, region_code, region_name)
    or None."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id, run_id, region_code, region_name
              FROM crab.ii_scrape_queue
             WHERE status = 'pending'
             ORDER BY pk_id ASC
             LIMIT 1
             FOR UPDATE SKIP LOCKED
        """)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        cur.execute("""
            UPDATE crab.ii_scrape_queue
               SET status = 'running', started_at = NOW()
             WHERE pk_id = %s
        """, (row[0],))
        conn.commit()
        return row
    finally:
        conn.close()


def _finish_row(queue_pk, run_id, new, updated, unchanged, errors):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE crab.ii_scrape_queue
               SET status = %s, finished_at = NOW(),
                   resorts_scraped = %s
             WHERE pk_id = %s
        """, ('done' if errors == 0 else 'done_with_errors',
              new + updated + unchanged, queue_pk))
        cur.execute("""
            UPDATE crab.ii_scrape_runs
               SET regions_done = regions_done + 1,
                   resorts_new = resorts_new + %s,
                   resorts_updated = resorts_updated + %s,
                   resorts_unchanged = resorts_unchanged + %s,
                   error_count = error_count + %s,
                   finished_at = CASE WHEN regions_done + 1 >= regions_total THEN NOW() ELSE finished_at END,
                   status = CASE WHEN regions_done + 1 >= regions_total THEN 'done' ELSE status END
             WHERE pk_id = %s
        """, (new, updated, unchanged, errors, run_id))
        conn.commit()
    finally:
        conn.close()


def _crawl_one():
    """Claim + crawl + finish one region. Returns summary or None if queue empty."""
    row = _claim_one()
    if not row:
        return None
    queue_pk, run_id, region_code, region_name = row
    t0 = time.time()

    # The scraper module's _crawl_region wants a single cursor with savepoints
    # threaded through area+resort loops. Use one conn for the whole region.
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        new, updated, unchanged, errors = _crawl_region(
            cur, region_code, region_name, run_id,
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  ❌ region {region_code} ({region_name}) crashed: {e}")
        new = updated = unchanged = 0
        errors = 1
    finally:
        conn.close()

    _finish_row(queue_pk, run_id, new, updated, unchanged, errors)
    elapsed = time.time() - t0
    return {
        'region_code': region_code, 'name': region_name,
        'new': new, 'updated': updated, 'unchanged': unchanged,
        'errors': errors, 'elapsed': elapsed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--one', action='store_true', help='Crawl exactly one region then exit.')
    ap.add_argument('--seed', action='store_true',
                    help='Seed a fresh run if nothing is pending.')
    args = ap.parse_args()

    if args.seed and _pending_count() == 0:
        run_id = start_run(triggered_by='local_drainer')
        print(f"seeded run {run_id}")

    total = {'new': 0, 'updated': 0, 'unchanged': 0, 'errors': 0, 'regions': 0}
    while True:
        summary = _crawl_one()
        if summary is None:
            break
        total['regions'] += 1
        for k in ('new', 'updated', 'unchanged', 'errors'):
            total[k] += summary[k]
        left = _pending_count()
        print(
            f"  ✓ {summary['name']:<30} "
            f"new={summary['new']:>3} upd={summary['updated']:>3} "
            f"unch={summary['unchanged']:>3} err={summary['errors']:>2} "
            f"({summary['elapsed']:.0f}s) · {left} left"
        )
        if args.one:
            break

    print(
        f"\ndone. regions={total['regions']} new={total['new']} "
        f"updated={total['updated']} unchanged={total['unchanged']} "
        f"errors={total['errors']}"
    )


if __name__ == '__main__':
    main()
