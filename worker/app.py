"""
Crab Scraper — Cloud Run worker for the II catalog + any future heavy job.

Lives OFF the App Engine web tier (crab.travel) so long-running scrapes
don't starve user requests. Same Cloud SQL database — writes commit
directly. Same GCP Secret Manager credentials — auth is identical to
App Engine via the `utilities/` shared modules.

Endpoints:
  GET  /health                 → liveness probe
  POST /scrape/seed            → start a new scrape run (all regions pending)
  POST /scrape/one             → crawl the next pending region (2-5min)
  POST /scrape/drain           → drain the whole queue (up to 60min timeout)

All endpoints are bearer-authed with CRAB_TIMESHARE_BEARER_TOKEN so
Cloud Scheduler + admin curl calls can trigger them but randoms can't.
"""
import logging
import os
import sys

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utilities.postgres_utils import get_db_connection
from utilities.timeshare_ii_scraper import _crawl_region, fetch_regions, start_run

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(name)s %(levelname)s %(message)s')
logger = logging.getLogger('crab_scraper_worker')

app = Flask(__name__)


# ── auth ────────────────────────────────────────────────────────────

def _authed():
    from utilities.google_auth_utils import get_secret
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return False
    supplied = header[len('Bearer '):].strip()
    try:
        expected = get_secret('CRAB_TIMESHARE_BEARER_TOKEN')
    except Exception as e:
        logger.error(f"bearer token lookup failed: {e}")
        return False
    return bool(expected) and supplied == expected


def _reject():
    return jsonify({'error': 'unauthorized'}), 401


# ── queue ops (copied minimal from timeshare_ii_scraper.process_next) ──

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
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id, run_id, region_code, region_name
              FROM crab.ii_scrape_queue
             WHERE status = 'pending'
             ORDER BY pk_id ASC LIMIT 1
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


def _finish(queue_pk, run_id, new, updated, unchanged, errors):
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
    row = _claim_one()
    if not row:
        return None
    queue_pk, run_id, region_code, region_name = row
    import time
    t0 = time.time()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        new, updated, unchanged, errors = _crawl_region(
            cur, region_code, region_name, run_id)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.exception(f"region {region_code} ({region_name}) crashed: {e}")
        new = updated = unchanged = 0
        errors = 1
    finally:
        conn.close()
    _finish(queue_pk, run_id, new, updated, unchanged, errors)
    return {
        'region_code': region_code, 'name': region_name,
        'new': new, 'updated': updated, 'unchanged': unchanged,
        'errors': errors, 'elapsed_s': round(time.time() - t0, 1),
    }


# ── endpoints ───────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': 'crab-scraper'})


@app.route('/scrape/seed', methods=['POST'])
def scrape_seed():
    if not _authed():
        return _reject()
    try:
        run_id = start_run(triggered_by='cloud_run')
    except Exception as e:
        logger.exception(f"seed failed: {e}")
        return jsonify({'error': str(e)[:200]}), 500
    return jsonify({'ok': True, 'run_id': run_id,
                    'pending': _pending_count()})


@app.route('/scrape/one', methods=['POST'])
def scrape_one():
    if not _authed():
        return _reject()
    result = _crawl_one()
    if not result:
        return jsonify({'ok': True, 'note': 'queue empty'})
    return jsonify({'ok': True, 'region': result,
                    'pending': _pending_count()})


@app.route('/scrape/drain', methods=['POST'])
def scrape_drain():
    """Drain the whole queue in one call. Cloud Run's 60min ceiling is plenty
    for a 31-region crawl (~60s each)."""
    if not _authed():
        return _reject()
    import time
    t0 = time.time()
    regions = []
    while True:
        r = _crawl_one()
        if r is None:
            break
        regions.append(r)
    return jsonify({
        'ok': True,
        'regions_processed': len(regions),
        'total_new': sum(r['new'] for r in regions),
        'total_updated': sum(r['updated'] for r in regions),
        'total_unchanged': sum(r['unchanged'] for r in regions),
        'total_errors': sum(r['errors'] for r in regions),
        'elapsed_s': round(time.time() - t0, 1),
        'regions': regions,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
