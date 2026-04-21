"""
II (Interval International) catalog queries + per-group shortlist ops.

The catalog tables (`crab.ii_regions`, `crab.ii_areas`, `crab.ii_resorts`)
are a SHARED travel resource — every timeshare group gets the same
underlying catalog, same as every crab plan gets the same destinations
cache. Reads are not group-scoped.

The shortlist table (`crab.timeshare_group_shortlist`) IS group-scoped
and honors the same 404-on-miss invisibility as the rest of the subsystem.

Writeback from the OpenCrab-style VPS scraper goes through the bearer-
authed endpoint in `timeshare_routes.py`. This module never runs scraping
itself — it's the consumer side.
"""

import logging
from typing import Optional

logger = logging.getLogger('crab_travel.timeshare_catalog')


def _fetchall(sql, params):
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _fetchone(sql, params):
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── Read ────────────────────────────────────────────────────────────

def list_regions():
    return _fetchall("""
        SELECT r.pk_id, r.ii_code, r.name, r.scraped_at,
               (SELECT COUNT(*) FROM crab.ii_areas a WHERE a.region_id = r.pk_id) AS area_count
          FROM crab.ii_regions r
         ORDER BY r.name ASC
    """, ())


def get_region(ii_code):
    return _fetchone("""
        SELECT pk_id, ii_code, name, scraped_at
          FROM crab.ii_regions
         WHERE ii_code = %s
    """, (ii_code,))


def list_areas_in_region(region_pk_id):
    return _fetchall("""
        SELECT a.pk_id, a.ii_code, a.name, a.country,
               (SELECT COUNT(*) FROM crab.ii_resorts rs WHERE rs.area_id = a.pk_id) AS resort_count
          FROM crab.ii_areas a
         WHERE a.region_id = %s
         ORDER BY a.name ASC
    """, (region_pk_id,))


def list_resorts_in_area(area_pk_id, limit=200):
    return _fetchall("""
        SELECT pk_id, ii_code, name, address, rating_overall, rating_response_count,
               sleeping_capacity, nearest_airport, photo_urls
          FROM crab.ii_resorts
         WHERE area_id = %s
         ORDER BY rating_overall DESC NULLS LAST, name ASC
         LIMIT %s
    """, (area_pk_id, limit))


def get_resort(ii_code):
    """Return one resort by II code + denormalized region/area context."""
    return _fetchone("""
        SELECT rs.*,
               a.ii_code  AS area_code, a.name AS area_name, a.country,
               r.ii_code  AS region_code, r.name AS region_name
          FROM crab.ii_resorts rs
          LEFT JOIN crab.ii_areas a ON a.pk_id = rs.area_id
          LEFT JOIN crab.ii_regions r ON r.pk_id = a.region_id
         WHERE rs.ii_code = %s
    """, (ii_code,))


def search_resorts_rich(q=None, country=None, tier=None, min_sleeps=None, limit=300):
    """Richer search for the /api/ii-resorts/search endpoint — matches on
    name, area, country, or address substring; filters by country, tier,
    min bedroom count. Returns fields the search UI needs."""
    params = []
    clauses = ["rs.status = 'active'"]
    if q:
        clauses.append(
            "(rs.name ILIKE %s OR a.name ILIKE %s OR a.country ILIKE %s OR rs.nearest_airport ILIKE %s)"
        )
        w = f"%{q}%"
        params.extend([w, w, w, w])
    if country:
        clauses.append("a.country ILIKE %s")
        params.append(country)
    if tier:
        clauses.append("rs.tier = %s")
        params.append(tier)
    if min_sleeps is not None:
        clauses.append("(rs.sleeping_capacity->>'total')::int >= %s")
        params.append(min_sleeps)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT rs.ii_code, rs.name, rs.tier, rs.nearest_airport,
               rs.check_in_day, rs.sleeping_capacity, rs.photo_urls,
               rs.description,
               a.name AS area_name, a.country,
               r.name AS region_name,
               g.map_lat, g.map_lng, g.google_rating, g.google_user_ratings_total
          FROM crab.ii_resorts rs
          LEFT JOIN crab.ii_areas a ON a.pk_id = rs.area_id
          LEFT JOIN crab.ii_regions r ON r.pk_id = a.region_id
          LEFT JOIN crab.ii_resort_google g ON g.resort_ii_code = rs.ii_code
         WHERE {where}
         ORDER BY
            CASE WHEN rs.tier = 'Premier_Boutique' THEN 0
                 WHEN rs.tier = 'Premier' THEN 1
                 WHEN rs.tier = 'Select' THEN 2
                 ELSE 3 END,
            COALESCE(g.google_rating, 0) DESC,
            rs.name ASC
         LIMIT %s
    """
    params.append(limit)
    return _fetchall(sql, tuple(params))


def country_counts():
    """Return [{country, resort_count, region_name}, ...] for the search
    sidebar's 'Browse by country' chips and the country-centroid map."""
    return _fetchall("""
        SELECT a.country, COUNT(*) AS resort_count,
               MIN(r.name) AS region_name
          FROM crab.ii_resorts rs
          JOIN crab.ii_areas a ON a.pk_id = rs.area_id
          LEFT JOIN crab.ii_regions r ON r.pk_id = a.region_id
         WHERE rs.status = 'active' AND a.country IS NOT NULL
         GROUP BY a.country
         ORDER BY resort_count DESC
    """, ())


def search_resorts(location=None, min_rating=None, min_sleeps=None, limit=25):
    """Free-text + filter search over the catalog. Used by the chatbot's
    search_resort_catalog tool AND any future /timeshare/catalog?q= form."""
    params = []
    clauses = []
    if location:
        clauses.append(
            "(rs.name ILIKE %s OR a.name ILIKE %s OR a.country ILIKE %s OR rs.address ILIKE %s)"
        )
        wildcard = f"%{location}%"
        params.extend([wildcard, wildcard, wildcard, wildcard])
    if min_rating is not None:
        clauses.append("rs.rating_overall >= %s")
        params.append(min_rating)
    if min_sleeps is not None:
        # sleeping_capacity is JSONB {unit_type: count}; match if any value ≥ min
        clauses.append("""
            EXISTS (
                SELECT 1 FROM jsonb_each_text(COALESCE(rs.sleeping_capacity, '{}'::jsonb)) AS kv(k, v)
                 WHERE v ~ '^[0-9]+$' AND v::int >= %s
            )
        """)
        params.append(min_sleeps)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT rs.pk_id, rs.ii_code, rs.name, rs.address,
               rs.rating_overall, rs.rating_response_count,
               rs.sleeping_capacity, rs.nearest_airport,
               a.name AS area_name, a.country,
               r.name AS region_name
          FROM crab.ii_resorts rs
          LEFT JOIN crab.ii_areas a ON a.pk_id = rs.area_id
          LEFT JOIN crab.ii_regions r ON r.pk_id = a.region_id
          {where}
         ORDER BY rs.rating_overall DESC NULLS LAST, rs.name ASC
         LIMIT %s
    """
    params.append(limit)
    return _fetchall(sql, tuple(params))


# ── Shortlist ops (group-scoped) ───────────────────────────────────

def list_shortlist(group_id):
    """Return the group's shortlisted resorts with full catalog context."""
    return _fetchall("""
        SELECT sl.pk_id, sl.network, sl.resort_code, sl.notes, sl.priority,
               sl.created_at, sl.added_by,
               rs.name AS resort_name, rs.rating_overall, rs.photo_urls,
               a.name AS area_name, a.country,
               r.name AS region_name
          FROM crab.timeshare_group_shortlist sl
          LEFT JOIN crab.ii_resorts rs ON rs.ii_code = sl.resort_code
                                       AND sl.network = 'interval_international'
          LEFT JOIN crab.ii_areas a ON a.pk_id = rs.area_id
          LEFT JOIN crab.ii_regions r ON r.pk_id = a.region_id
         WHERE sl.group_id = %s::uuid
         ORDER BY sl.priority DESC, sl.created_at ASC
    """, (group_id,))


def is_shortlisted(group_id, network, resort_code):
    row = _fetchone("""
        SELECT 1 FROM crab.timeshare_group_shortlist
         WHERE group_id = %s::uuid AND network = %s AND resort_code = %s
    """, (group_id, network, resort_code))
    return row is not None


def toggle_shortlist(group_id, network, resort_code, added_by=None, notes=None):
    """Add if missing, remove if present. Returns ('added' | 'removed')."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM crab.timeshare_group_shortlist
             WHERE group_id = %s::uuid AND network = %s AND resort_code = %s
        """, (group_id, network, resort_code))
        if cur.rowcount > 0:
            conn.commit()
            return 'removed'
        cur.execute("""
            INSERT INTO crab.timeshare_group_shortlist
                (group_id, network, resort_code, added_by, notes)
            VALUES (%s::uuid, %s, %s, %s, %s)
        """, (group_id, network, resort_code, added_by, notes))
        conn.commit()
        return 'added'
    finally:
        conn.close()


# ── Writeback (called from the bearer-authed route) ─────────────────

def upsert_catalog(regions=None, areas=None, resorts=None):
    """Idempotent upsert for a catalog sync. Each input is a list of dicts
    with the fields matching the DB columns. Runs in a single transaction.
    Returns a summary dict."""
    from utilities.postgres_utils import get_db_connection
    counts = {'regions': 0, 'areas': 0, 'resorts': 0}
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Regions: UPSERT by ii_code
        for r in (regions or []):
            cur.execute("""
                INSERT INTO crab.ii_regions (ii_code, name, scraped_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (ii_code) DO UPDATE SET
                    name = EXCLUDED.name,
                    scraped_at = NOW()
            """, (r['ii_code'], r['name']))
            counts['regions'] += 1

        # Areas: UPSERT by ii_code; region_id resolved from region_ii_code
        for a in (areas or []):
            cur.execute("""
                INSERT INTO crab.ii_areas
                    (ii_code, name, country, region_id, scraped_at)
                VALUES (%s, %s, %s,
                        (SELECT pk_id FROM crab.ii_regions WHERE ii_code = %s),
                        NOW())
                ON CONFLICT (ii_code) DO UPDATE SET
                    name = EXCLUDED.name,
                    country = EXCLUDED.country,
                    region_id = EXCLUDED.region_id,
                    scraped_at = NOW()
            """, (a['ii_code'], a['name'], a.get('country'), a.get('region_ii_code')))
            counts['areas'] += 1

        # Resorts: UPSERT by ii_code; area_id resolved from area_ii_code
        import json as _json
        for rs in (resorts or []):
            cur.execute("""
                INSERT INTO crab.ii_resorts (
                    ii_code, name, address, phone, website,
                    nearest_airport, check_in_day, sleeping_capacity,
                    tdi_score, rating_overall, rating_services, rating_property,
                    rating_accommodations, rating_experience, rating_response_count,
                    description, amenities, photo_urls, map_lat, map_lng,
                    area_id, scraped_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s::jsonb, %s::jsonb, %s, %s,
                    (SELECT pk_id FROM crab.ii_areas WHERE ii_code = %s),
                    NOW()
                )
                ON CONFLICT (ii_code) DO UPDATE SET
                    name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    phone = EXCLUDED.phone,
                    website = EXCLUDED.website,
                    nearest_airport = EXCLUDED.nearest_airport,
                    check_in_day = EXCLUDED.check_in_day,
                    sleeping_capacity = EXCLUDED.sleeping_capacity,
                    tdi_score = EXCLUDED.tdi_score,
                    rating_overall = EXCLUDED.rating_overall,
                    rating_services = EXCLUDED.rating_services,
                    rating_property = EXCLUDED.rating_property,
                    rating_accommodations = EXCLUDED.rating_accommodations,
                    rating_experience = EXCLUDED.rating_experience,
                    rating_response_count = EXCLUDED.rating_response_count,
                    description = EXCLUDED.description,
                    amenities = EXCLUDED.amenities,
                    photo_urls = EXCLUDED.photo_urls,
                    map_lat = EXCLUDED.map_lat,
                    map_lng = EXCLUDED.map_lng,
                    area_id = EXCLUDED.area_id,
                    scraped_at = NOW()
            """, (
                rs['ii_code'], rs['name'], rs.get('address'), rs.get('phone'),
                rs.get('website'), rs.get('nearest_airport'), rs.get('check_in_day'),
                _json.dumps(rs.get('sleeping_capacity')) if rs.get('sleeping_capacity') else None,
                rs.get('tdi_score'), rs.get('rating_overall'),
                rs.get('rating_services'), rs.get('rating_property'),
                rs.get('rating_accommodations'), rs.get('rating_experience'),
                rs.get('rating_response_count'),
                rs.get('description'),
                _json.dumps(rs.get('amenities')) if rs.get('amenities') else None,
                _json.dumps(rs.get('photo_urls')) if rs.get('photo_urls') else None,
                rs.get('map_lat'), rs.get('map_lng'),
                rs.get('area_ii_code'),
            ))
            counts['resorts'] += 1

        conn.commit()
        logger.info(
            f"ii catalog upsert: {counts['regions']}R / {counts['areas']}A / {counts['resorts']}S"
        )
        return counts
    except Exception as e:
        conn.rollback()
        logger.exception(f"ii catalog upsert failed: {e}")
        raise
    finally:
        conn.close()
