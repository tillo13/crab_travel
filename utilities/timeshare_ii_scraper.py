"""
Interval International catalog scraper — anonymous, polite, incremental.

Endpoints (all public, no auth, verified against the
_antiquated_code/timeshare/intervalworld_endpoints.json dictionary):

  GET /web/cs?a=1501                          → region directory (31 regions)
  GET /web/cs?a=1501&regionCode=<N>           → areas in region N
  GET /web/cs?a=1502&areaCode=<code>          → resorts in area
  GET /web/cs?a=1503&resortCode=<code>        → resort detail page

We only extract STRUCTURAL facts from II (name/address/phone/airport/
check-in-days/sleeping-capacity/photos/tier). II's internal "member
ratings" are marketing-controlled and intentionally skipped — the
resort-detail view enriches with real Google Places reviews on open.

Diff tracking: every row on ii_regions/areas/resorts carries a
content_hash (sha256 of the normalized fields). An upsert that finds the
same hash touches last_seen_at only; a differing hash also updates the
row and bumps the `updated` counter on the current scrape run.
"""

import hashlib
import json
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger('crab_travel.timeshare_ii_scraper')

BASE_URL = "https://www.intervalworld.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 crab.travel/timeshare"
SLEEP_BETWEEN_REQUESTS = 1.5   # polite default: ~40 req/min
HTTP_TIMEOUT = 30


def _http_get(path: str, params: dict = None) -> str:
    r = requests.get(
        f"{BASE_URL}{path}",
        params=params or {},
        headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,*/*'},
        timeout=HTTP_TIMEOUT,
        allow_redirects=True,
    )
    r.raise_for_status()
    time.sleep(SLEEP_BETWEEN_REQUESTS)
    return r.text


def _hash(*parts) -> str:
    joined = '\x1f'.join('' if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


# ── Parsers ─────────────────────────────────────────────────────────

def fetch_regions():
    """Returns [{ii_code, name}, ...] — the 31 regions."""
    html = _http_get('/web/cs', {'a': '1501'})
    soup = BeautifulSoup(html, 'html.parser')
    out = {}
    for a in soup.find_all('a', href=re.compile(r'regionCode=\d+')):
        m = re.search(r'regionCode=(\d+)', a['href'])
        if not m:
            continue
        code = int(m.group(1))
        name = a.get_text(strip=True)
        if not name:
            continue
        # Dedupe: same code may have multiple links; pick the longest human name
        existing = out.get(code)
        if existing is None or len(name) > len(existing):
            out[code] = name
    return [{'ii_code': c, 'name': n} for c, n in sorted(out.items())]


def fetch_areas(region_code: int):
    """Returns [{ii_code, name, country}, ...] for one region. `country` is
    best-effort from the area label (II labels areas like 'Hawaii, Maui' —
    the left side is our country/parent-region proxy)."""
    html = _http_get('/web/cs', {'a': '1501', 'regionCode': region_code})
    soup = BeautifulSoup(html, 'html.parser')
    out = {}
    for a in soup.find_all('a', href=re.compile(r'areaCode=\d+')):
        m = re.search(r'areaCode=(\d+)', a['href'])
        if not m:
            continue
        code = int(m.group(1))
        name = a.get_text(strip=True)
        if not name:
            continue
        existing = out.get(code)
        if existing is None or len(name) > len(existing['name']):
            country = name.split(',')[0].strip() if ',' in name else None
            out[code] = {'ii_code': code, 'name': name, 'country': country}
    return list(out.values())


def fetch_resorts_in_area(area_code: int):
    """Returns [{ii_code, name}, ...] for one area. Detail fields come from
    fetch_resort_detail() — this page only has names + codes."""
    html = _http_get('/web/cs', {'a': '1502', 'areaCode': area_code})
    soup = BeautifulSoup(html, 'html.parser')
    out = {}
    for a in soup.find_all('a', href=re.compile(r'resortCode=[A-Z0-9]+')):
        m = re.search(r'resortCode=([A-Z0-9]+)', a['href'])
        if not m:
            continue
        code = m.group(1)
        name = a.get_text(strip=True)
        if not name or name.lower().startswith('resort details'):
            continue
        existing = out.get(code)
        if existing is None or len(name) > len(existing['name']):
            out[code] = {'ii_code': code, 'name': name}
    return list(out.values())


_RE_SLEEP_NUM = re.compile(r'<span id="(bedrooms|private|total)">(\d+)</span>')


def fetch_resort_detail(resort_code: str):
    """Return a dict of structural facts for one resort. Skips II's member-
    rating section entirely — that's a later Google Places enrichment."""
    html = _http_get('/web/cs', {'a': '1503', 'resortCode': resort_code})
    soup = BeautifulSoup(html, 'html.parser')

    # Title → 'Interval International | Resort Directory <Name>'
    title_tag = soup.find('title')
    name = None
    if title_tag:
        t = title_tag.get_text(strip=True)
        m = re.search(r'Resort Directory\s+(.+)', t)
        if m:
            name = m.group(1).strip()

    text = soup.get_text('\n', strip=True)

    def _after(marker, max_len=200):
        idx = text.find(marker)
        if idx < 0:
            return None
        # Grab the next non-empty line after the marker
        tail = text[idx + len(marker):].lstrip('\n ').split('\n')
        return (tail[0].strip() if tail else '')[:max_len] or None

    # Sleeping capacity (embedded as three <span> values)
    sleep = {k: int(v) for k, v in _RE_SLEEP_NUM.findall(html)}

    # Check-in days — legacy block lists a comma-separated weekday string
    check_in_days = _after('Check-In Days')

    # Nearest airport
    airport = _after('Nearest Airport')

    # Address + phone: look for the `resort_layout_information` block
    # labeled "Resort Address" when present
    address = _after('Resort Address', 400)

    # Phone: II renders as a tel: link
    phone = None
    tel = soup.find('a', href=re.compile(r'^tel:'))
    if tel:
        phone = tel.get_text(strip=True) or tel['href'].replace('tel:', '')

    # Tier: Premier Boutique / Select / Premier
    tier = None
    for t in ('Premier Boutique Resort', 'Premier Resort', 'Select Resort'):
        if t in html:
            tier = t.replace(' Resort', '').replace(' Boutique', '_Boutique')
            break

    # Description: II puts the resort blurb in a paragraph with class like
    # `resort_description` or just a long <p>. Fallback: first large <p>.
    desc = None
    for sel in [('div', {'class': re.compile(r'resort.*description', re.I)}),
                ('div', {'id': re.compile(r'description', re.I)})]:
        el = soup.find(*sel[:1], **{k: v for k, v in [sel[1].popitem()]} if sel[1] else {})
        if el:
            desc = el.get_text(' ', strip=True)[:2000]
            break

    # Photos: /images/_resd/jpglg/ii_<code>N.jpg (N=1..many). The detail HTML
    # lists all available — just collect them verbatim so we don't guess.
    photos = []
    for img in soup.find_all('img', src=re.compile(rf'/images/_resd/jpg(lg|md|sm)?/ii_{resort_code}\d+\.', re.I)):
        src = img.get('src', '')
        if src and src not in photos:
            photos.append(src if src.startswith('http') else f"{BASE_URL}{src}")

    # Embedded weather — `avgTemp` / `JSONtempF` / `JSONtempC` — parse if present
    weather = None
    m = re.search(r'var\s+JSONtempF\s*=\s*(\[[^;]*\]);', html)
    if m and len(m.group(1)) > 5:
        try:
            weather = {'tempF': json.loads(m.group(1))}
        except Exception:
            pass

    # Website (external) — often absent on the detail page; fallback to II's resort URL
    website_url = f"{BASE_URL}/web/cs?a=1503&resortCode={resort_code}"

    return {
        'ii_code': resort_code,
        'name': name,
        'address': address,
        'phone': phone,
        'website': website_url,
        'nearest_airport': airport,
        'check_in_day': check_in_days,
        'sleeping_capacity': sleep or None,
        'description': desc,
        'photo_urls': photos or None,
        'tier': tier,
        'amenities': None,  # deferred; structured list isn't static HTML
        'weather': weather,
    }


# ── Upsert with diff tracking ──────────────────────────────────────

def upsert_region(cur, region: dict, run_id: int):
    h = _hash(region['name'])
    cur.execute("""
        INSERT INTO crab.ii_regions (ii_code, name, content_hash, scraped_at, last_run_id, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, NOW(), %s, NOW(), NOW())
        ON CONFLICT (ii_code) DO UPDATE SET
            name = EXCLUDED.name,
            content_hash = EXCLUDED.content_hash,
            scraped_at = NOW(),
            last_run_id = EXCLUDED.last_run_id,
            last_seen_at = NOW(),
            status = 'active'
        RETURNING pk_id, (xmax = 0) AS inserted
    """, (region['ii_code'], region['name'], h, run_id))
    row = cur.fetchone()
    return row[0], row[1]  # (pk_id, was_inserted_bool)


def upsert_area(cur, area: dict, region_pk_id: int, run_id: int):
    h = _hash(area['name'], area.get('country'))
    cur.execute("""
        INSERT INTO crab.ii_areas (ii_code, name, country, region_id, content_hash,
                                   scraped_at, last_run_id, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, NOW(), %s, NOW(), NOW())
        ON CONFLICT (ii_code) DO UPDATE SET
            name = EXCLUDED.name,
            country = EXCLUDED.country,
            region_id = EXCLUDED.region_id,
            content_hash = EXCLUDED.content_hash,
            scraped_at = NOW(),
            last_run_id = EXCLUDED.last_run_id,
            last_seen_at = NOW(),
            status = 'active'
        RETURNING pk_id
    """, (area['ii_code'], area['name'], area.get('country'), region_pk_id, h, run_id))
    return cur.fetchone()[0]


def upsert_resort(cur, detail: dict, area_pk_id: int, run_id: int) -> str:
    """Returns 'new' | 'updated' | 'unchanged'."""
    h = _hash(
        detail.get('name'), detail.get('address'), detail.get('phone'),
        detail.get('nearest_airport'), detail.get('check_in_day'),
        json.dumps(detail.get('sleeping_capacity'), sort_keys=True),
        json.dumps(detail.get('photo_urls'), sort_keys=True),
        detail.get('tier'), detail.get('description'),
    )
    # Check existing hash first to classify new / updated / unchanged
    cur.execute(
        "SELECT pk_id, content_hash FROM crab.ii_resorts WHERE ii_code = %s",
        (detail['ii_code'],),
    )
    existing = cur.fetchone()
    if existing and existing[1] == h:
        cur.execute("""
            UPDATE crab.ii_resorts
               SET last_seen_at = NOW(), last_run_id = %s, status = 'active'
             WHERE pk_id = %s
        """, (run_id, existing[0]))
        return 'unchanged'

    cur.execute("""
        INSERT INTO crab.ii_resorts (
            ii_code, name, address, phone, website, nearest_airport,
            check_in_day, sleeping_capacity, photo_urls, tier, description,
            area_id, content_hash, scraped_at, last_run_id,
            first_seen_at, last_seen_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s::jsonb, %s::jsonb, %s, %s,
            %s, %s, NOW(), %s,
            NOW(), NOW()
        )
        ON CONFLICT (ii_code) DO UPDATE SET
            name = EXCLUDED.name,
            address = EXCLUDED.address,
            phone = EXCLUDED.phone,
            website = EXCLUDED.website,
            nearest_airport = EXCLUDED.nearest_airport,
            check_in_day = EXCLUDED.check_in_day,
            sleeping_capacity = EXCLUDED.sleeping_capacity,
            photo_urls = EXCLUDED.photo_urls,
            tier = EXCLUDED.tier,
            description = EXCLUDED.description,
            area_id = EXCLUDED.area_id,
            content_hash = EXCLUDED.content_hash,
            scraped_at = NOW(),
            last_run_id = EXCLUDED.last_run_id,
            last_seen_at = NOW(),
            status = 'active'
    """, (
        detail['ii_code'], detail.get('name'), detail.get('address'),
        detail.get('phone'), detail.get('website'), detail.get('nearest_airport'),
        detail.get('check_in_day'),
        json.dumps(detail.get('sleeping_capacity')) if detail.get('sleeping_capacity') else None,
        json.dumps(detail.get('photo_urls')) if detail.get('photo_urls') else None,
        detail.get('tier'), detail.get('description'),
        area_pk_id, h, run_id,
    ))
    return 'updated' if existing else 'new'


# ── Run orchestration ──────────────────────────────────────────────

def start_run(triggered_by: str = 'cron'):
    """Create a new ii_scrape_run and seed the queue with every region.
    Returns the run_id. Idempotent for same-day runs: if an already-running
    run exists, returns it instead of creating a duplicate."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id FROM crab.ii_scrape_runs
             WHERE status = 'running' AND started_at > NOW() - INTERVAL '1 day'
             ORDER BY started_at DESC LIMIT 1
        """)
        existing = cur.fetchone()
        if existing:
            return existing[0]

        # Fetch regions directly from II so the queue knows what to crawl
        regions = fetch_regions()

        cur.execute("""
            INSERT INTO crab.ii_scrape_runs (regions_total, triggered_by)
            VALUES (%s, %s) RETURNING pk_id
        """, (len(regions), triggered_by))
        run_id = cur.fetchone()[0]

        # Seed the queue
        for r in regions:
            cur.execute("""
                INSERT INTO crab.ii_scrape_queue
                    (run_id, region_code, region_name, status)
                VALUES (%s, %s, %s, 'pending')
                ON CONFLICT DO NOTHING
            """, (run_id, r['ii_code'], r['name']))
            # Also upsert the region row itself so it exists before areas
            upsert_region(cur, r, run_id)

        conn.commit()
        logger.info(f"ii_scrape_run {run_id} started with {len(regions)} queued regions")
        return run_id
    finally:
        conn.close()


def process_next(max_regions: int = 1):
    """Pick N pending queue rows (SKIP LOCKED), crawl each fully, update
    counters on the parent run. Returns a summary dict. Designed to be
    called by cron every few minutes until the queue drains."""
    from utilities.postgres_utils import get_db_connection
    summary = {
        'run_id': None,
        'regions_processed': [],
        'resorts_new': 0,
        'resorts_updated': 0,
        'resorts_unchanged': 0,
        'errors': 0,
    }
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for _ in range(max_regions):
            # Claim next pending row with SELECT ... FOR UPDATE SKIP LOCKED
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
                break
            queue_pk, run_id, region_code, region_name = row
            summary['run_id'] = run_id
            cur.execute("""
                UPDATE crab.ii_scrape_queue
                   SET status = 'running', started_at = NOW()
                 WHERE pk_id = %s
            """, (queue_pk,))
            conn.commit()

            new, updated, unchanged, errors = _crawl_region(cur, region_code, region_name, run_id)
            conn.commit()

            cur.execute("""
                UPDATE crab.ii_scrape_queue
                   SET status = %s, finished_at = NOW(),
                       areas_scraped = %s, resorts_scraped = %s
                 WHERE pk_id = %s
            """, ('done' if errors == 0 else 'done_with_errors',
                  None, new + updated + unchanged, queue_pk))
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

            summary['regions_processed'].append({
                'region_code': region_code, 'name': region_name,
                'new': new, 'updated': updated, 'unchanged': unchanged, 'errors': errors,
            })
            summary['resorts_new'] += new
            summary['resorts_updated'] += updated
            summary['resorts_unchanged'] += unchanged
            summary['errors'] += errors
        return summary
    finally:
        conn.close()


def _crawl_region(cur, region_code, region_name, run_id):
    """Full walk of one region. Caller owns the connection."""
    new = updated = unchanged = errors = 0
    try:
        cur.execute("SELECT pk_id FROM crab.ii_regions WHERE ii_code = %s", (region_code,))
        region_pk_id = cur.fetchone()[0]

        areas = fetch_areas(region_code)
        for area in areas:
            try:
                area_pk_id = upsert_area(cur, area, region_pk_id, run_id)
                resorts = fetch_resorts_in_area(area['ii_code'])
                for stub in resorts:
                    try:
                        detail = fetch_resort_detail(stub['ii_code'])
                        if not detail.get('name'):
                            detail['name'] = stub['name']  # fallback if title parse fails
                        result = upsert_resort(cur, detail, area_pk_id, run_id)
                        if result == 'new':
                            new += 1
                        elif result == 'updated':
                            updated += 1
                        else:
                            unchanged += 1
                    except Exception as e:
                        logger.warning(f"resort {stub['ii_code']} failed: {e}")
                        errors += 1
            except Exception as e:
                logger.warning(f"area {area['ii_code']} failed: {e}")
                errors += 1
    except Exception as e:
        logger.exception(f"region {region_code} ({region_name}) failed: {e}")
        errors += 1
    return new, updated, unchanged, errors


def get_run_summary(limit: int = 10):
    """Rollup for the admin page."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT r.*,
                   (SELECT COUNT(*) FROM crab.ii_scrape_queue q WHERE q.run_id = r.pk_id AND q.status = 'pending') AS pending_regions
              FROM crab.ii_scrape_runs r
             ORDER BY r.started_at DESC
             LIMIT %s
        """, (limit,))
        return [dict(x) for x in cur.fetchall()]
    finally:
        conn.close()
