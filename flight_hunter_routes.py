"""Flight Hunter — narrow bearer-auth API for OpenClaw fleet.

Two endpoints, both gated by CRAB_OPENCRAB_BEARER_TOKEN (same as other
/api/opencrab/* endpoints):

  POST /api/opencrab/flight-hunter/jobs-to-run
      → returns list of {origin, destination, depart_date, adapters}
        for routes/dates that haven't been observed in the last N hours.
        OpenClaw obeys this list and never picks its own work.

  POST /api/opencrab/flight-hunter/observations
      → accepts list of observation dicts from OpenClaw, sanitizes,
        applies allowlist + caps + named-column INSERT, writes to
        kumori_ops.flight_hunter_observations.

Follows the discovery-vs-decision pattern from
_infrastructure/openclaw/MANIFEST.md: crab.travel decides what is due;
OpenClaw fleet executes; results POST back through this guardrailed
write surface.
"""
import json
import logging
from datetime import date, timedelta

import psycopg2.extras
from flask import Blueprint, jsonify, request

from route_helpers import bearer_auth_required
from utilities.postgres_utils import get_db_connection

logger = logging.getLogger(__name__)

bp = Blueprint('flight_hunter', __name__)


# ── Least-privilege caps ────────────────────────────────────────────────
MAX_PAYLOAD_BYTES = 200_000
MAX_OBSERVATIONS_PER_POST = 500
PER_SOURCE_HOURLY_CAP = 2000        # generous; ~21 routes × 90 dates = 1890
JOBS_TO_RUN_MAX = 200
OBS_ALLOWED_FIELDS = {
    'origin', 'destination', 'depart_date', 'return_date',
    'price_usd', 'currency', 'airline', 'stops', 'duration_minutes',
    'source', 'deep_link', 'raw',
}

# ── Scan window + cadence ───────────────────────────────────────────────
SCAN_HORIZON_DAYS = 90
DATE_STRIDE_DAYS = 3                 # sample every 3rd date in the window
MIN_HOURS_BETWEEN_OBS = 6            # re-crawl threshold per (source, route, date)
WATCH_DATE_SHIFT_DAYS = 3           # for each active watch, also scan checkin ±N days
                                    # so /explore (which reads ±N) always has coverage


# ── Helpers ─────────────────────────────────────────────────────────────
def _readonly_mode():
    """Kill switch: CRAB_OPENCRAB_READONLY='on' blocks all writes."""
    try:
        from utilities.google_auth_utils import get_secret
        return (get_secret('CRAB_OPENCRAB_READONLY') or 'off').strip().lower() == 'on'
    except Exception:
        return False


def _sanitize_observation(o):
    """Apply allowlist + coerce types. Returns None if unusable."""
    if not isinstance(o, dict):
        return None
    clean = {k: v for k, v in o.items() if k in OBS_ALLOWED_FIELDS}

    # Required fields
    for req in ('origin', 'destination', 'depart_date', 'price_usd', 'source'):
        if not clean.get(req):
            return None

    # IATA codes — 3 or 4 chars, alpha
    for k in ('origin', 'destination'):
        v = str(clean[k]).strip().upper()
        if not (3 <= len(v) <= 4) or not v.isalpha():
            return None
        clean[k] = v

    # Dates
    for k in ('depart_date', 'return_date'):
        v = clean.get(k)
        if v is None:
            clean[k] = None
            continue
        try:
            d = date.fromisoformat(str(v)[:10])
        except (TypeError, ValueError):
            if k == 'depart_date':
                return None
            clean[k] = None
            continue
        # sanity: 1 year past → 2 years future
        if d < (date.today() - timedelta(days=365)) or d > (date.today() + timedelta(days=730)):
            if k == 'depart_date':
                return None
            clean[k] = None
        else:
            clean[k] = d

    # Price
    try:
        p = float(clean['price_usd'])
    except (TypeError, ValueError):
        return None
    if p < 0 or p > 100_000:
        return None
    clean['price_usd'] = round(p, 2)

    # Currency
    cur = str(clean.get('currency') or 'USD')[:3].upper()
    clean['currency'] = cur if len(cur) == 3 and cur.isalpha() else 'USD'

    # Source (free string, capped)
    clean['source'] = str(clean['source'])[:40]

    # Optional strings
    for k in ('airline',):
        if clean.get(k) is not None:
            clean[k] = str(clean[k])[:80]

    # Optional ints
    for k in ('stops', 'duration_minutes'):
        v = clean.get(k)
        if v is None:
            clean[k] = None
            continue
        try:
            clean[k] = int(v)
        except (TypeError, ValueError):
            clean[k] = None
    if clean.get('stops') is not None and not (0 <= clean['stops'] <= 5):
        clean['stops'] = None

    # Deep link
    url = clean.get('deep_link')
    if url is not None:
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')) or len(url) > 2000:
            clean['deep_link'] = None

    # Raw — must be dict, capped
    raw = clean.get('raw')
    if raw is None or not isinstance(raw, dict):
        clean['raw'] = {}
    else:
        if len(json.dumps(raw, default=str)) > 8000:
            clean['raw'] = {}

    return clean


# ── Endpoint 1: discovery ───────────────────────────────────────────────
@bp.route('/api/opencrab/flight-hunter/jobs-to-run', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def jobs_to_run():
    """Return list of (origin, destination, depart_date) jobs that need a fresh
    observation. Crab decides; OpenClaw obeys.

    Job source = Andy's live watches first, then Mom's standing grid:
      1. active_watch_routes() — every active non-bot flight watch whose origin is
         one Andy/Mom actually fly from (HOME_ORIGINS), scanned at checkin ±N days.
         These come FIRST so real watches are never starved behind the grid.
      2. ROUTE_PAIRS — Mom's proactive home→family grid, sampled every Nth date.

    Anything observed within MIN_HOURS_BETWEEN_OBS is fresh and skipped.
    """
    from flight_hunter.config import ROUTE_PAIRS, HOME_ORIGINS
    from flight_hunter.reader import active_watch_routes
    from datetime import datetime, timezone

    body = request.get_json(silent=True) or {}
    limit = min(int(body.get('limit') or 50), JOBS_TO_RUN_MAX)
    adapters_filter = body.get('adapters')  # optional ['fast_flights', ...]

    today = date.today()
    earliest = today + timedelta(days=2)        # +2: today/tomorrow 401 on Google Flights
    horizon = today + timedelta(days=SCAN_HORIZON_DAYS)

    conn = get_db_connection()
    try:
        # 1) Andy's active watches → (origin, dest, checkin ±N), clamped to the window.
        watch_candidates = []
        for o, d_iata, checkin in active_watch_routes(conn, HOME_ORIGINS, SCAN_HORIZON_DAYS):
            for s in range(-WATCH_DATE_SHIFT_DAYS, WATCH_DATE_SHIFT_DAYS + 1):
                dt = checkin + timedelta(days=s)
                if earliest <= dt <= horizon:
                    watch_candidates.append((o, d_iata, dt))

        # 2) Mom's standing grid → ROUTE_PAIRS × strided dates.
        grid_dates = [today + timedelta(days=i)
                      for i in range(2, SCAN_HORIZON_DAYS, DATE_STRIDE_DAYS)]
        grid_candidates = [(o, d_iata, dt)
                           for (o, d_iata, _label, _fb) in ROUTE_PAIRS
                           for dt in grid_dates]

        # Watches first, then grid; dedup preserving priority order.
        seen = set()
        candidates = []
        for cand in watch_candidates + grid_candidates:
            if cand not in seen:
                seen.add(cand)
                candidates.append(cand)

        if not candidates:
            return jsonify({'jobs': [], 'count': 0}), 200

        # Staleness lookup — latest observed_at per candidate tuple. A correlated
        # LIMIT-1 subquery against idx_fho_route_date_obs (…, observed_at DESC)
        # returns each route-date's freshest row in ONE index touch.
        #
        # The earlier MAX()+GROUP BY form looked index-friendly but forced Postgres
        # to read EVERY observation per route-date (~1.6K rows each, the table never
        # dedups) into a GroupAggregate + external disk sort — ~403K rows scanned for
        # 254 candidates, 1.3s warm and climbing ~daily as the table grows. That was
        # the 30s statement_timeout. The correlated LIMIT 1 is ~5ms and stays flat as
        # the table grows. (Pruning kumori_ops.flight_hunter_observations is still a
        # separate follow-up — 1.2M rows / 595 MB and growing ~49K/day, never pruned.)
        c_origins = [c[0] for c in candidates]
        c_dests = [c[1] for c in candidates]
        c_dates = [c[2] for c in candidates]
        last_seen = {}
        cur = conn.cursor()
        cur.execute("""
            SELECT c.origin, c.destination, c.depart_date,
                   (SELECT f.observed_at
                      FROM kumori_ops.flight_hunter_observations f
                     WHERE f.origin = c.origin
                       AND f.destination = c.destination
                       AND f.depart_date = c.depart_date
                     ORDER BY f.observed_at DESC
                     LIMIT 1)
            FROM unnest(%s::text[], %s::text[], %s::date[])
                 AS c(origin, destination, depart_date)
        """, (c_origins, c_dests, c_dates))
        for o, d_iata, depart, last in cur.fetchall():
            last_seen[(o, d_iata, depart)] = last
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=MIN_HOURS_BETWEEN_OBS)
    jobs = []
    for (o, d_iata, depart) in candidates:
        last = last_seen.get((o, d_iata, depart))
        if last is not None and (now - last) < threshold:
            continue
        job = {
            'origin': o,
            'destination': d_iata,
            'depart_date': depart.isoformat(),
        }
        if adapters_filter:
            job['adapters'] = list(adapters_filter)
        jobs.append(job)
        if len(jobs) >= limit:
            break

    return jsonify({'jobs': jobs, 'count': len(jobs)}), 200


# ── Endpoint 2: write-back ──────────────────────────────────────────────
@bp.route('/api/opencrab/flight-hunter/observations', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def post_observations():
    """Accept a list of observation dicts from OpenClaw. Sanitize + insert."""
    if _readonly_mode():
        return jsonify({'error': 'readonly_mode'}), 503

    if request.content_length and request.content_length > MAX_PAYLOAD_BYTES:
        return jsonify({'error': 'payload_too_large', 'max': MAX_PAYLOAD_BYTES}), 413

    body = request.get_json(silent=True) or {}
    raw_obs = body.get('observations') or []
    if not isinstance(raw_obs, list):
        return jsonify({'error': 'observations must be a list'}), 400
    if len(raw_obs) > MAX_OBSERVATIONS_PER_POST:
        return jsonify({'error': 'too_many_observations', 'max': MAX_OBSERVATIONS_PER_POST}), 400

    cleaned = []
    rejected = 0
    for o in raw_obs:
        c = _sanitize_observation(o)
        if c is None:
            rejected += 1
        else:
            cleaned.append(c)

    if not cleaned:
        return jsonify({'inserted': 0, 'rejected': rejected, 'reason': 'no_valid_observations'}), 200

    # Per-source hourly rate cap — fail closed if a runaway adapter floods.
    sources_in_payload = {c['source'] for c in cleaned}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for src in sources_in_payload:
            cur.execute("""
                SELECT COUNT(*) FROM kumori_ops.flight_hunter_observations
                WHERE source = %s AND observed_at >= NOW() - INTERVAL '1 hour'
            """, (src,))
            recent = cur.fetchone()[0]
            if recent >= PER_SOURCE_HOURLY_CAP:
                return jsonify({
                    'error': 'per_source_hourly_cap_exceeded',
                    'source': src,
                    'recent': recent,
                    'cap': PER_SOURCE_HOURLY_CAP,
                }), 429

        # Named-column INSERT — no dynamic SQL, only allowlisted fields hit columns.
        rows = [(
            c['origin'], c['destination'], c['depart_date'], c.get('return_date'),
            c['price_usd'], c['currency'], c.get('airline'), c.get('stops'),
            c.get('duration_minutes'), c['source'], c.get('deep_link'),
            psycopg2.extras.Json(c.get('raw') or {}),
        ) for c in cleaned]

        psycopg2.extras.execute_values(cur, """
            INSERT INTO kumori_ops.flight_hunter_observations
              (origin, destination, depart_date, return_date,
               price_usd, currency, airline, stops,
               duration_minutes, source, deep_link, raw)
            VALUES %s
        """, rows)
        conn.commit()
        inserted = len(rows)
    finally:
        conn.close()

    return jsonify({
        'inserted': inserted,
        'rejected': rejected,
        'sources': sorted(sources_in_payload),
    }), 200


# ── Endpoint 3: digest ──────────────────────────────────────────────────
@bp.route('/api/opencrab/flight-hunter/digest', methods=['POST'])
@bearer_auth_required('CRAB_OPENCRAB_BEARER_TOKEN')
def send_digest():
    """Render and email the daily flight-hunter digest to the admin.

    Recipient is hard-coded to CRAB_OPENCRAB_ADMIN_RECIPIENT — no per-request
    targeting (OpenClaw can only ever address the admin). Body is rendered
    server-side from kumori_ops.flight_hunter_observations.
    """
    # RETIRED 2026-06-04 — the standalone flight_hunter deal digest email was
    # consolidated into the daily heartbeat (one exception-first crab email).
    # The flight scanner's liveness now shows up there as the 'flight_hunter'
    # health row (obs count / freshness). The VPS cron may still POST here;
    # it's a harmless no-op. dry_run still renders for debugging. Flip
    # CRAB_FLIGHT_DIGEST_EMAIL='on' to revive the standalone send.
    from flight_hunter.digest import build_and_render
    from utilities.gmail_utils import send_simple_email
    from utilities.google_auth_utils import get_secret

    body_in = request.get_json(silent=True) or {}
    digest_email_on = (get_secret('CRAB_FLIGHT_DIGEST_EMAIL') or 'off').strip().lower() == 'on'
    if not digest_email_on and not body_in.get('dry_run'):
        return jsonify({'ok': True, 'suppressed': 'consolidated_into_heartbeat'}), 200

    admin = (get_secret('CRAB_OPENCRAB_ADMIN_RECIPIENT') or '').strip()
    if not admin:
        return jsonify({'error': 'CRAB_OPENCRAB_ADMIN_RECIPIENT not set'}), 500

    conn = get_db_connection()
    try:
        subject, plain_body, html_body = build_and_render(conn)
    finally:
        conn.close()

    dry_run = bool(body_in.get('dry_run'))
    if dry_run:
        return jsonify({
            'dry_run': True,
            'recipient': admin,
            'subject': subject,
            'plain_body_chars': len(plain_body),
            'html_body_chars': len(html_body),
            'plain_preview': plain_body[:1200],
        }), 200

    try:
        ok = bool(send_simple_email(subject, plain_body, admin, html=html_body))
    except Exception as e:
        logger.error(f"flight-hunter digest send failed: {e}")
        return jsonify({'error': 'send failed', 'detail': str(e)}), 500
    return jsonify({'ok': ok, 'delivered_to': admin, 'subject': subject}), 200
