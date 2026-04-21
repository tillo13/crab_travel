"""
Google Places enrichment for II resort catalog.

Phase 8c of the timeshare buildout — II's internal member ratings are
marketing-controlled and intentionally skipped by the scraper. Real
quality signal comes from Google Places: rating, review count, phone,
street address, lat/lng (which unlocks per-resort map pins).

Caching: all results land in `crab.ii_resort_google` (created in
Phase 8a schema). Re-fetched if older than 30 days. No refetch on
rate_limit / not_found results (we honor the negative cache too).

Cost posture: ~$0.02 per textSearch call at "Basic + Atmosphere" field
masks. With 2500 resorts fetched lazily on open, ~$50 for full coverage
spread over real user browsing.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from utilities.google_auth_utils import get_secret

logger = logging.getLogger('crab_travel.timeshare_google')

PLACES_TEXTSEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Basic + Atmosphere tier — skip Pro (reviews full text) to keep costs down.
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.location,places.nationalPhoneNumber,places.internationalPhoneNumber,"
    "places.rating,places.userRatingCount,places.priceLevel,"
    "places.websiteUri,places.googleMapsUri,places.types,places.photos"
)
CACHE_TTL_DAYS = 30


def _api_key():
    return get_secret('CRAB_YOUTUBE_API_KEY')


def fetch_place(resort_name: str, area_hint: str = None):
    """Return a Google Place match for a resort. None if no hit."""
    q = resort_name.strip()
    if area_hint:
        q = f"{q} {area_hint}"
    try:
        r = requests.post(
            PLACES_TEXTSEARCH_URL,
            headers={
                'Content-Type': 'application/json',
                'X-Goog-Api-Key': _api_key(),
                'X-Goog-FieldMask': FIELD_MASK,
            },
            json={'textQuery': q, 'maxResultCount': 1},
            timeout=15,
        )
        if r.status_code >= 400:
            logger.warning(f"Places textSearch {r.status_code}: {r.text[:200]}")
            return None
        places = r.json().get('places') or []
        return places[0] if places else None
    except Exception as e:
        logger.warning(f"Places textSearch failed for {q!r}: {e}")
        return None


def get_or_fetch_google(ii_code: str, resort_name: str, area_hint: str = None):
    """Read cache first, refresh if stale, return whatever we have.
    Never raises — on any API failure returns the cached row (or None)
    so the UI never breaks because Google is down.
    """
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM crab.ii_resort_google WHERE resort_ii_code = %s
        """, (ii_code,))
        cached = cur.fetchone()

        fresh_cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_TTL_DAYS)
        if cached and cached['fetched_at'] and cached['fetched_at'] > fresh_cutoff:
            return dict(cached)

        # Fetch from Google
        place = fetch_place(resort_name, area_hint)
        if not place:
            # Negative cache — don't re-call for a month
            cur.execute("""
                INSERT INTO crab.ii_resort_google (resort_ii_code, fetched_at, error_message)
                VALUES (%s, NOW(), 'no_match')
                ON CONFLICT (resort_ii_code) DO UPDATE SET
                    fetched_at = NOW(),
                    error_message = 'no_match'
            """, (ii_code,))
            conn.commit()
            return dict(cached) if cached else None

        cur.execute("""
            INSERT INTO crab.ii_resort_google (
                resort_ii_code, place_id, google_name, google_formatted_address,
                google_rating, google_user_ratings_total, google_price_level,
                google_phone, google_website, google_photos, google_types,
                map_lat, map_lng, fetched_at, error_message
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, NOW(), NULL)
            ON CONFLICT (resort_ii_code) DO UPDATE SET
                place_id = EXCLUDED.place_id,
                google_name = EXCLUDED.google_name,
                google_formatted_address = EXCLUDED.google_formatted_address,
                google_rating = EXCLUDED.google_rating,
                google_user_ratings_total = EXCLUDED.google_user_ratings_total,
                google_price_level = EXCLUDED.google_price_level,
                google_phone = EXCLUDED.google_phone,
                google_website = EXCLUDED.google_website,
                google_photos = EXCLUDED.google_photos,
                google_types = EXCLUDED.google_types,
                map_lat = EXCLUDED.map_lat,
                map_lng = EXCLUDED.map_lng,
                fetched_at = NOW(),
                error_message = NULL
        """, (
            ii_code,
            place.get('id'),
            (place.get('displayName') or {}).get('text'),
            place.get('formattedAddress'),
            place.get('rating'),
            place.get('userRatingCount'),
            place.get('priceLevel'),
            place.get('nationalPhoneNumber') or place.get('internationalPhoneNumber'),
            place.get('websiteUri'),
            json.dumps(place.get('photos')) if place.get('photos') else None,
            json.dumps(place.get('types')) if place.get('types') else None,
            (place.get('location') or {}).get('latitude'),
            (place.get('location') or {}).get('longitude'),
        ))
        conn.commit()
        cur.execute("""
            SELECT * FROM crab.ii_resort_google WHERE resort_ii_code = %s
        """, (ii_code,))
        return dict(cur.fetchone())
    except Exception as e:
        logger.exception(f"get_or_fetch_google({ii_code}) failed: {e}")
        return dict(cached) if cached else None
    finally:
        conn.close()


def resorts_with_coords(limit: int = 2000):
    """For the dashboard map — return resorts that have cached Google coords."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT rs.ii_code, rs.name, rs.tier,
                   a.name AS area_name, a.country,
                   g.map_lat, g.map_lng, g.google_rating, g.google_user_ratings_total
              FROM crab.ii_resorts rs
              JOIN crab.ii_resort_google g ON g.resort_ii_code = rs.ii_code
              LEFT JOIN crab.ii_areas a ON a.pk_id = rs.area_id
             WHERE rs.status = 'active'
               AND g.map_lat IS NOT NULL AND g.map_lng IS NOT NULL
             LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
