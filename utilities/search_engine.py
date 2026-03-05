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


def _destination_iata(destination):
    """
    Best-effort: if destination looks like an IATA code already, use it.
    Otherwise return as-is — adapters handle city name lookups.
    Known city→IATA mappings for common US destinations.
    Full IATA lookup via an API is a future improvement.
    """
    IATA_MAP = {
        'phoenix': 'PHX', 'scottsdale': 'PHX',
        'new york': 'NYC', 'new york city': 'NYC',
        'los angeles': 'LAX',
        'chicago': 'ORD',
        'miami': 'MIA',
        'las vegas': 'LAS',
        'nashville': 'BNA',
        'denver': 'DEN',
        'austin': 'AUS',
        'seattle': 'SEA',
        'san francisco': 'SFO',
        'boston': 'BOS',
        'atlanta': 'ATL',
        'dallas': 'DFW',
        'houston': 'IAH',
        'portland': 'PDX',
        'san diego': 'SAN',
        'orlando': 'MCO',
        'hawaii': 'HNL', 'honolulu': 'HNL',
        'helsinki': 'HEL',
        'london': 'LON',
        'paris': 'PAR',
        'rome': 'ROM',
        'tokyo': 'TYO',
        'cancun': 'CUN',
    }
    key = destination.lower().strip()
    if len(key) == 3 and key.isalpha():
        return key.upper()
    return IATA_MAP.get(key, destination)
