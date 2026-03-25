"""
Search engine — fans out across all adapters in parallel background threads.
Each adapter writes results to crab.search_results as they land.
The SSE endpoint in app.py pushes new rows to connected clients.

Usage:
    from utilities.search_engine import trigger_search
    trigger_search(plan_id, destination, checkin, checkout, origin_airports)
"""

import logging
import threading
from utilities.adapters import ALL_ADAPTERS
from utilities.postgres_utils import save_search_result, save_price_history

logger = logging.getLogger(__name__)

# Track which plans have a search in progress
_active_searches = {}
_active_searches_lock = threading.Lock()


def is_searching(plan_id):
    with _active_searches_lock:
        return _active_searches.get(str(plan_id), False)


def trigger_search(plan_id, destination, checkin=None, checkout=None,
                   origin_airports=None, guests=2):
    """
    Kick off a full adapter fan-out for a plan.
    Returns immediately — all work happens in background threads.
    Each adapter runs as its own daemon thread so slow sources
    don't block fast ones.
    """
    plan_id = str(plan_id)

    with _active_searches_lock:
        if _active_searches.get(plan_id):
            logger.info(f"🔍 Search already running for plan {plan_id}, skipping")
            return
        _active_searches[plan_id] = True

    logger.info(f"🔍 Starting search fan-out for plan {plan_id} → {destination}")

    def _run_adapter(AdapterClass):
        adapter = AdapterClass()
        results = []
        try:
            # Flights — one thread per origin airport
            if origin_airports and checkin:
                for origin in origin_airports:
                    flights = adapter.search_flights(
                        origin=origin,
                        destination=_destination_iata(destination),
                        depart_date=checkin,
                        return_date=checkout,
                    )
                    for f in flights:
                        row = save_search_result(
                            plan_id=plan_id,
                            result_type='flight',
                            source=f['source'],
                            canonical_key=f['canonical_key'],
                            title=f"{f['airline']} {f['origin']}→{f['destination']}",
                            price_usd=f['price_usd'],
                            deep_link=f['deep_link'],
                            data=f,
                        )
                        if row and f.get('price_usd'):
                            save_price_history(
                                result_type='flight',
                                canonical_key=f['canonical_key'],
                                source=f['source'],
                                price_usd=f['price_usd'],
                                travel_date=checkin,
                            )
                        results.append(f)

            # Hotels
            if checkin and checkout:
                hotels = adapter.search_hotels(
                    destination=destination,
                    checkin=checkin,
                    checkout=checkout,
                    guests=guests,
                )
                for h in hotels:
                    row = save_search_result(
                        plan_id=plan_id,
                        result_type='hotel',
                        source=h['source'],
                        canonical_key=h['canonical_key'],
                        title=h['name'],
                        price_usd=h.get('price_per_night_usd'),
                        deep_link=h['deep_link'],
                        data=h,
                    )
                    if row and h.get('price_per_night_usd'):
                        save_price_history(
                            result_type='hotel',
                            canonical_key=h['canonical_key'],
                            source=h['source'],
                            price_usd=h['price_per_night_usd'],
                            travel_date=checkin,
                        )
                    results.append(h)

            # Activities
            activities = adapter.search_activities(destination=destination)
            for a in activities:
                save_search_result(
                    plan_id=plan_id,
                    result_type='activity',
                    source=a['source'],
                    canonical_key=a['canonical_key'],
                    title=a['name'],
                    price_usd=a.get('price_usd'),
                    deep_link=a['deep_link'],
                    data=a,
                )
                results.append(a)

            logger.info(f"✅ {adapter.source_key}: {len(results)} results for plan {plan_id}")

        except Exception as e:
            logger.error(f"❌ Adapter {adapter.source_key} failed for plan {plan_id}: {e}")

    # Spawn one thread per adapter — they all write to DB independently
    threads = []
    for AdapterClass in ALL_ADAPTERS:
        t = threading.Thread(target=_run_adapter, args=(AdapterClass,), daemon=True)
        t.start()
        threads.append(t)

    # Watcher thread — clears the active flag when all adapters finish
    def _mark_done():
        for t in threads:
            t.join()
        with _active_searches_lock:
            _active_searches[plan_id] = False
        logger.info(f"🏁 Search complete for plan {plan_id}")

    threading.Thread(target=_mark_done, daemon=True).start()


_IATA_CACHE = {}

# Fast local lookup — covers common destinations without burning an LLM call
_IATA_MAP = {
    'phoenix': 'PHX', 'scottsdale': 'PHX', 'mesa': 'PHX', 'tempe': 'PHX',
    'new york': 'JFK', 'new york city': 'JFK', 'nyc': 'JFK', 'manhattan': 'JFK',
    'los angeles': 'LAX', 'la': 'LAX', 'hollywood': 'LAX',
    'chicago': 'ORD',
    'miami': 'MIA', 'fort lauderdale': 'FLL',
    'las vegas': 'LAS', 'vegas': 'LAS',
    'nashville': 'BNA',
    'denver': 'DEN',
    'austin': 'AUS',
    'seattle': 'SEA',
    'san francisco': 'SFO', 'sf': 'SFO',
    'boston': 'BOS',
    'atlanta': 'ATL',
    'dallas': 'DFW', 'fort worth': 'DFW',
    'houston': 'IAH',
    'portland': 'PDX',
    'san diego': 'SAN',
    'orlando': 'MCO',
    'hawaii': 'HNL', 'honolulu': 'HNL', 'maui': 'OGG', 'kauai': 'LIH',
    'helsinki': 'HEL',
    'london': 'LHR',
    'paris': 'CDG',
    'rome': 'FCO',
    'tokyo': 'NRT',
    'cancun': 'CUN',
    'detroit': 'DTW',
    'minneapolis': 'MSP',
    'philadelphia': 'PHL', 'philly': 'PHL',
    'charlotte': 'CLT',
    'salt lake city': 'SLC',
    'san antonio': 'SAT',
    'tampa': 'TPA',
    'new orleans': 'MSY', 'nola': 'MSY',
    'pittsburgh': 'PIT',
    'st louis': 'STL', 'saint louis': 'STL',
    'kansas city': 'MCI',
    'indianapolis': 'IND',
    'raleigh': 'RDU', 'durham': 'RDU',
    'savannah': 'SAV',
    'charleston': 'CHS',
    'boise': 'BOI',
    'tucson': 'TUS',
    'albuquerque': 'ABQ',
    'anchorage': 'ANC',
    'juneau': 'JNU',
    'key west': 'EYW',
    'napa': 'SFO', 'napa valley': 'SFO',
    'palm springs': 'PSP',
    'jackson hole': 'JAC',
    'aspen': 'ASE',
    'cabo': 'SJD', 'cabo san lucas': 'SJD',
    'barcelona': 'BCN',
    'amsterdam': 'AMS',
    'lisbon': 'LIS',
    'dublin': 'DUB',
    'reykjavik': 'KEF', 'iceland': 'KEF',
    'bali': 'DPS',
    'bangkok': 'BKK',
    'sydney': 'SYD',
    'toronto': 'YYZ',
    'vancouver': 'YVR',
    'mexico city': 'MEX',
}


def _destination_iata(destination):
    """Convert a destination string to an IATA airport code.

    Tries: exact 3-letter code → local map → in-memory cache → LLM lookup.
    """
    if not destination:
        return destination

    key = destination.lower().strip()

    # Already an IATA code
    if len(key) == 3 and key.isalpha():
        return key.upper()

    # Strip common suffixes like "AZ", "CA", state abbreviations
    import re
    cleaned = re.sub(r',?\s+[A-Z]{2}$', '', destination.strip())
    clean_key = cleaned.lower().strip()

    # Local map (fast, no API call)
    if clean_key in _IATA_MAP:
        return _IATA_MAP[clean_key]
    if key in _IATA_MAP:
        return _IATA_MAP[key]

    # In-memory cache from previous LLM lookups
    if key in _IATA_CACHE:
        return _IATA_CACHE[key]
    if clean_key in _IATA_CACHE:
        return _IATA_CACHE[clean_key]

    # LLM lookup — use a free backend to resolve
    try:
        from utilities.llm_router import generate
        prompt = (
            f"What is the nearest major airport IATA code for: {destination}\n"
            "Reply with ONLY the 3-letter IATA code, nothing else. "
            "Example: PHX"
        )
        result, _backend = generate(prompt, max_tokens=10, temperature=0.0, caller='iata_lookup')
        if result:
            code = result.strip().upper()[:3]
            if len(code) == 3 and code.isalpha():
                _IATA_CACHE[key] = code
                _IATA_CACHE[clean_key] = code
                logger.info(f"IATA lookup: '{destination}' → {code} (via LLM)")
                return code
    except Exception as e:
        logger.warning(f"IATA LLM lookup failed for '{destination}': {e}")

    # Last resort — return as-is
    return destination
