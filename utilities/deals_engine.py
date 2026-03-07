"""
deals_engine.py — global hot deals aggregator.

Queries all available "deals" endpoints across adapters and returns
a unified, scored list of the best deals right now for groups.

Scoring is purely by price_per_person (lowest = best).
Each deal includes total_for_n so the UI can show cost for any group size.
"""

import logging
import threading
import requests
from datetime import datetime, date, timedelta
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

TRAVELPAYOUTS_TOKEN = None

# Major global departure hubs to query — covers most group trip origins
ORIGIN_HUBS = [
    "NYC", "LAX", "CHI", "MIA", "SFO", "SEA", "DFW", "BOS",
    "LHR", "CDG", "AMS", "DXB", "SYD", "YYZ",
]

MARKER = "708186"

# City name / common alias → IATA code
CITY_TO_IATA = {
    "new york": "NYC", "new york city": "NYC", "nyc": "NYC", "jfk": "JFK", "lga": "LGA", "ewr": "EWR",
    "los angeles": "LAX", "la": "LAX", "lax": "LAX",
    "chicago": "CHI", "ord": "ORD", "midway": "MDW",
    "miami": "MIA", "mia": "MIA",
    "san francisco": "SFO", "sf": "SFO", "sfo": "SFO",
    "seattle": "SEA", "sea": "SEA",
    "dallas": "DFW", "dfw": "DFW", "fort worth": "DFW",
    "boston": "BOS", "bos": "BOS",
    "denver": "DEN", "den": "DEN",
    "atlanta": "ATL", "atl": "ATL",
    "houston": "HOU", "iah": "IAH", "hou": "HOU",
    "phoenix": "PHX", "phx": "PHX",
    "las vegas": "LAS", "vegas": "LAS", "las": "LAS",
    "orlando": "MCO", "mco": "MCO",
    "washington": "WAS", "dc": "WAS", "washington dc": "WAS", "iad": "IAD", "dca": "DCA",
    "minneapolis": "MSP", "msp": "MSP",
    "detroit": "DTW", "dtw": "DTW",
    "portland": "PDX", "pdx": "PDX",
    "san diego": "SAN", "san": "SAN",
    "nashville": "BNA", "bna": "BNA",
    "austin": "AUS", "aus": "AUS",
    "charlotte": "CLT", "clt": "CLT",
    "philadelphia": "PHL", "philly": "PHL", "phl": "PHL",
    "salt lake city": "SLC", "slc": "SLC",
    "kansas city": "MCI", "mci": "MCI",
    "tampa": "TPA", "tpa": "TPA",
    "honolulu": "HNL", "hawaii": "HNL", "hnl": "HNL",
    # International
    "london": "LHR", "lhr": "LHR", "london gatwick": "LGW",
    "paris": "CDG", "cdg": "CDG",
    "amsterdam": "AMS", "ams": "AMS",
    "frankfurt": "FRA", "fra": "FRA",
    "dubai": "DXB", "dxb": "DXB",
    "toronto": "YYZ", "yyz": "YYZ",
    "vancouver": "YVR", "yvr": "YVR",
    "sydney": "SYD", "syd": "SYD",
    "melbourne": "MEL", "mel": "MEL",
    "tokyo": "TYO", "tyo": "TYO", "narita": "NRT",
    "singapore": "SIN", "sin": "SIN",
    "hong kong": "HKG", "hkg": "HKG",
    "bangkok": "BKK", "bkk": "BKK",
    "mexico city": "MEX", "mex": "MEX",
    "cancun": "CUN", "cun": "CUN",
    "madrid": "MAD", "mad": "MAD",
    "barcelona": "BCN", "bcn": "BCN",
    "rome": "FCO", "fco": "FCO",
    "milan": "MXP", "mxp": "MXP",
    "zurich": "ZRH", "zrh": "ZRH",
    "stockholm": "ARN", "arn": "ARN",
    "oslo": "OSL", "osl": "OSL",
    "copenhagen": "CPH", "cph": "CPH",
}


def resolve_iata(origin_input):
    """Convert city name or IATA code to uppercase IATA code."""
    if not origin_input:
        return None
    key = origin_input.strip().lower()
    return CITY_TO_IATA.get(key, origin_input.strip().upper())


def _get_tp_token():
    global TRAVELPAYOUTS_TOKEN
    if not TRAVELPAYOUTS_TOKEN:
        TRAVELPAYOUTS_TOKEN = get_secret("CRAB_TRAVELPAYOUTS_API_KEY")
    return TRAVELPAYOUTS_TOKEN


def _fetch_tp_deals(origin, results, lock):
    """Fetch Travelpayouts special offers for one origin hub."""
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/get_special_offers",
            params={
                "token": _get_tp_token(),
                "origin": origin,
                "currency": "usd",
                "locale": "en",
            },
            timeout=10,
        )
        resp.raise_for_status()
        deals = resp.json().get("data", [])
        normalized = []
        for d in deals:
            price = d.get("price")
            if not price:
                continue
            try:
                depart_dt = datetime.fromisoformat(d["departure_at"].replace("Z", "+00:00"))
                depart_str = depart_dt.strftime("%b %-d, %Y")
            except Exception:
                depart_str = d.get("departure_at", "")[:10]

            raw_link = d.get("link", "")
            sep = "&" if "?" in raw_link else "?"
            deep_link = f"https://www.aviasales.com{raw_link}{sep}marker={MARKER}"

            normalized.append({
                "deal_type": "flight",
                "source": "travelpayouts",
                "origin": d.get("origin", origin),
                "origin_name": d.get("origin_name", origin),
                "destination": d.get("destination", ""),
                "destination_name": d.get("destination_name", ""),
                "airline": d.get("airline_title", ""),
                "price_per_person": float(price),
                "depart_date": depart_str,
                "duration_mins": d.get("duration"),
                "deep_link": deep_link,
                "title": d.get("title", ""),
                "bookable": False,
            })

        with lock:
            results.extend(normalized)
        logger.info(f"✈️  TP special offers {origin}: {len(normalized)} deals")
    except Exception as e:
        logger.warning(f"⚠️  TP special offers {origin} failed: {e}")


def _fetch_tp_cheapest(origin, results, lock):
    """Fetch cheapest fares to ALL destinations from origin via Travelpayouts /v1/prices/cheap."""
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/v1/prices/cheap",
            params={
                "token": _get_tp_token(),
                "origin": origin,
                "currency": "usd",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        normalized = []
        for dest_iata, fares in data.items():
            # fares is {fare_index: fare_obj} — take the cheapest
            best = min(fares.values(), key=lambda f: f.get("price", 9999999))
            price = best.get("price")
            if not price:
                continue

            depart_raw = best.get("departure_at", "")
            try:
                depart_dt = datetime.fromisoformat(depart_raw.replace("Z", "+00:00"))
                depart_str = depart_dt.strftime("%b %-d, %Y")
                # Build aviasales search URL: ORIGIN+DDMM+DEST+passengers
                path = f"{origin}{depart_dt.strftime('%d%m')}{dest_iata}1"
            except Exception:
                depart_str = depart_raw[:10]
                path = f"{origin}{dest_iata}1"

            deep_link = f"https://www.aviasales.com/search/{path}?marker={MARKER}"

            normalized.append({
                "deal_type": "flight",
                "source": "travelpayouts_cheap",
                "origin": origin,
                "origin_name": origin,
                "destination": dest_iata,
                "destination_name": dest_iata,
                "airline": best.get("airline", ""),
                "price_per_person": float(price),
                "depart_date": depart_str,
                "duration_mins": None,
                "deep_link": deep_link,
                "title": f"{origin} → {dest_iata}",
                "bookable": False,
            })

        with lock:
            results.extend(normalized)
        logger.info(f"✈️  TP cheapest {origin}: {len(normalized)} destinations")
    except Exception as e:
        logger.warning(f"⚠️  TP cheapest {origin} failed: {e}")


def _fetch_viator_deals(results, lock):
    """Fetch Viator discounted activities globally."""
    try:
        key = get_secret("CRAB_VIATOR_API_KEY").strip()
        resp = requests.post(
            "https://api.viator.com/partner/products/search",
            headers={
                "exp-api-key": key,
                "Accept": "application/json;version=2.0",
                "Accept-Language": "en-US",
                "Content-Type": "application/json",
            },
            json={
                "filtering": {
                    "flags": ["SPECIAL_OFFER"],
                },
                "sorting": {"sort": "TRAVELER_RATING", "order": "DESCENDING"},
                "pagination": {"start": 1, "count": 20},
                "currency": "USD",
            },
            timeout=15,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        normalized = []
        for p in products:
            pricing = p.get("pricing", {}).get("summary", {})
            price = pricing.get("fromPrice") or pricing.get("fromPriceBeforeDiscount")
            if not price:
                continue
            normalized.append({
                "deal_type": "activity",
                "source": "viator",
                "origin": None,
                "origin_name": None,
                "destination": p.get("destination", {}).get("ref", ""),
                "destination_name": p.get("destination", {}).get("name", ""),
                "airline": None,
                "price_per_person": float(price),
                "depart_date": None,
                "duration_mins": p.get("duration", {}).get("fixedDurationInMinutes"),
                "deep_link": p.get("productUrl", f"https://www.viator.com/tours/{p.get('productCode','')}"),
                "title": p.get("title", ""),
                "bookable": True,
            })
        with lock:
            results.extend(normalized)
        logger.info(f"🎟️  Viator special offers: {len(normalized)} deals")
    except Exception as e:
        logger.warning(f"⚠️  Viator special offers failed: {e}")


# Popular destinations to probe when using Duffel
DUFFEL_PROBE_DESTINATIONS = ["PHX", "LAS", "MCO", "MIA", "DEN", "HNL", "BNA", "AUS", "CUN", "LHR"]

# Popular cities to fetch hotels for via LiteAPI
LITEAPI_CITIES = [
    ("Las Vegas", "US"), ("Miami", "US"), ("Phoenix", "US"), ("Denver", "US"),
    ("Honolulu", "US"), ("Nashville", "US"), ("New York", "US"), ("Austin", "US"),
    ("Cancun", "MX"), ("London", "GB"),
]


def _fetch_duffel_deals(origin, results, lock):
    """Search Duffel for flights from origin to popular destinations.
    Sequential with small delay to avoid 429 rate limiting."""
    import time
    from utilities.adapters.duffel import DuffelAdapter
    adapter = DuffelAdapter()
    depart_date = (date.today() + timedelta(days=60)).strftime("%Y-%m-%d")
    dests = [d for d in DUFFEL_PROBE_DESTINATIONS if d != origin]

    normalized = []
    for dest in dests:
        try:
            flights = adapter.search_flights(origin, dest, depart_date, passengers=1)
            if not flights:
                continue
            cheapest = min(flights, key=lambda f: f["price_usd"])
            try:
                depart_dt = datetime.fromisoformat(cheapest["depart_at"].replace("Z", "+00:00"))
                depart_str = depart_dt.strftime("%b %-d, %Y")
            except Exception:
                depart_str = depart_date
            gf_link = (
                f"https://www.google.com/travel/flights?hl=en"
                f"#flt={origin}.{dest}.{depart_date};c:USD;e:1;sd:1;t:f"
            )
            normalized.append({
                "deal_type": "flight",
                "source": "duffel",
                "origin": origin,
                "origin_name": origin,
                "destination": dest,
                "destination_name": dest,
                "airline": cheapest.get("airline", ""),
                "price_per_person": cheapest["price_usd"],
                "price_unit": "person",
                "depart_date": depart_str,
                "duration_mins": None,
                "deep_link": gf_link,
                "title": f"{origin} → {dest}",
                "bookable": False,
            })
            time.sleep(0.5)  # avoid 429
        except Exception as e:
            logger.debug(f"Duffel {origin}→{dest}: {e}")

    with lock:
        results.extend(normalized)
    logger.info(f"✈️  Duffel flights from {origin}: {len(normalized)} deals")


# Star rating → estimated nightly rate (sandbox key has no live rates)
_STAR_PRICE = {5: 350, 4: 180, 3: 100, 2: 65, 1: 45}


def _fetch_liteapi_hotels(results, lock):
    """Fetch hotel listing from LiteAPI for popular cities.
    Sandbox key doesn't support live rates — uses star-based price estimate.
    Swap to a live key (ltapi_ prefix) to get real rates."""
    try:
        key = get_secret("CRAB_LITEAPI_API_KEY").strip()
        is_sandbox = key.startswith("sand_")
        headers = {"X-API-Key": key, "Accept": "application/json", "Content-Type": "application/json"}
        checkin = (date.today() + timedelta(days=60)).strftime("%Y-%m-%d")
        checkout = (date.today() + timedelta(days=63)).strftime("%Y-%m-%d")
        nights = 3

        normalized = []
        for city, country in LITEAPI_CITIES:
            try:
                r1 = requests.get(
                    "https://api.liteapi.travel/v3.0/data/hotels",
                    headers=headers,
                    params={"countryCode": country, "cityName": city, "limit": 8},
                    timeout=15,
                )
                r1.raise_for_status()
                hotels_meta = r1.json().get("data", [])
                if not hotels_meta:
                    continue

                if is_sandbox:
                    # Sandbox: no live rates — estimate from star rating
                    for h in hotels_meta:
                        stars = int(h.get("stars") or 3)
                        price_per_night = _STAR_PRICE.get(stars, 100)
                        hotel_id = h.get("id", "")
                        normalized.append({
                            "deal_type": "hotel",
                            "source": "liteapi",
                            "origin": None,
                            "origin_name": None,
                            "destination": city,
                            "destination_name": city,
                            "airline": None,
                            "price_per_person": float(price_per_night),
                            "price_unit": "room/night (est.)",
                            "depart_date": f"Check in {checkin}",
                            "duration_mins": None,
                            "deep_link": (
                                f"https://app.liteapi.travel/hotels/{hotel_id}"
                                f"?checkin={checkin}&checkout={checkout}&adults=2"
                            ),
                            "title": h.get("name", hotel_id),
                            "bookable": False,
                        })
                else:
                    # Live key: fetch real rates
                    hotel_map = {h["id"]: h.get("name", h["id"]) for h in hotels_meta if h.get("id")}
                    r2 = requests.post(
                        "https://api.liteapi.travel/v3.0/hotels-rates",
                        headers=headers,
                        json={
                            "hotelIds": list(hotel_map.keys()),
                            "checkin": checkin, "checkout": checkout,
                            "currency": "USD", "guestNationality": "US",
                            "occupancies": [{"adults": 2}],
                        },
                        timeout=30,
                    )
                    r2.raise_for_status()
                    if not r2.content:
                        continue
                    for hotel in r2.json().get("data", []):
                        hotel_id = hotel.get("hotelId", "")
                        cheapest_total = None
                        for room in hotel.get("roomTypes", []):
                            for rate in room.get("rates", []):
                                totals = rate.get("retailRate", {}).get("total", [])
                                if totals:
                                    amt = float(totals[0].get("amount", 0))
                                    if amt and (cheapest_total is None or amt < cheapest_total):
                                        cheapest_total = amt
                        if not cheapest_total:
                            continue
                        normalized.append({
                            "deal_type": "hotel",
                            "source": "liteapi",
                            "origin": None,
                            "origin_name": None,
                            "destination": city,
                            "destination_name": city,
                            "airline": None,
                            "price_per_person": round(cheapest_total / nights, 2),
                            "price_unit": "room/night",
                            "depart_date": f"Check in {checkin}",
                            "duration_mins": None,
                            "deep_link": (
                                f"https://app.liteapi.travel/hotels/{hotel_id}"
                                f"?checkin={checkin}&checkout={checkout}&adults=2"
                            ),
                            "title": hotel_map.get(hotel_id, hotel_id),
                            "bookable": True,
                        })

                logger.info(f"🏨  LiteAPI {city}: {len([x for x in normalized if x['destination'] == city])} hotels ({'est.' if is_sandbox else 'live'})")
            except Exception as e:
                logger.warning(f"⚠️  LiteAPI {city}: {e}")

        with lock:
            results.extend(normalized)
    except Exception as e:
        logger.warning(f"⚠️  LiteAPI hotels failed: {e}")


def _run_all_sources(origin=None):
    """Spin up all source threads and return raw results list."""
    results = []
    lock = threading.Lock()
    threads = []

    origins_to_search = [resolve_iata(origin)] if origin else ORIGIN_HUBS

    for orig in origins_to_search:
        for fn in (_fetch_tp_deals, _fetch_tp_cheapest):
            t = threading.Thread(target=fn, args=(orig, results, lock), daemon=True)
            threads.append(t)
            t.start()

    # Duffel — one thread for the whole origin (fans out internally per destination)
    if origin:
        orig = resolve_iata(origin)
        t = threading.Thread(target=_fetch_duffel_deals, args=(orig, results, lock), daemon=True)
        threads.append(t)
        t.start()

    # LiteAPI hotels + Viator — source-level, not origin-specific
    for fn in (_fetch_viator_deals, _fetch_liteapi_hotels):
        t = threading.Thread(target=fn, args=(results, lock), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=30)

    return results


def _annotate(deals, group_size):
    for d in deals:
        d["group_size"] = group_size
        d["total_for_group"] = round(d["price_per_person"] * group_size, 2)
    return deals


def get_hot_deals(group_size=15, limit=20, origin=None):
    """
    Returns flat list of best deals sorted by price_per_person.
    Deduplicates across sources (special offers beat cheapest for same route).
    """
    results = _run_all_sources(origin)

    SOURCE_PRIORITY = {"travelpayouts": 0, "viator": 1, "travelpayouts_cheap": 2}
    results.sort(key=lambda d: SOURCE_PRIORITY.get(d.get("source", ""), 9))

    seen = set()
    unique = []
    for d in results:
        key = (d["deal_type"], d.get("origin", ""), d["destination"])
        if key not in seen:
            seen.add(key)
            unique.append(d)

    unique.sort(key=lambda x: x["price_per_person"])
    return _annotate(unique[:limit], group_size)


def get_hot_deals_grouped(group_size=15, origin=None):
    """
    Returns deals grouped into tabs by source — no cross-source dedup, no global limit.
    Each tab sorted by price_per_person.

    Returns list of tab dicts:
      [{"key": str, "label": str, "deals": [...]}]
    """
    results = _run_all_sources(origin)

    # Bucket by source
    buckets = {}
    for d in results:
        src = d.get("source", "other")
        buckets.setdefault(src, []).append(d)

    # Sort each bucket
    for src in buckets:
        buckets[src].sort(key=lambda x: x["price_per_person"])
        _annotate(buckets[src], group_size)

    TAB_ORDER = [
        ("travelpayouts",       "✈️ Aviasales Specials"),
        ("travelpayouts_cheap", "✈️ Aviasales All Flights"),
        ("duffel",              "✈️ Duffel Flights"),
        ("liteapi",             "🏨 LiteAPI Hotels"),
        ("viator",              "🎟️ Viator Activities"),
    ]

    tabs = []
    for src_key, label in TAB_ORDER:
        deals = buckets.get(src_key, [])
        if deals:
            tabs.append({
                "key": src_key,
                "label": f"{label} ({len(deals)})",
                "deals": deals,
            })

    return tabs


def refresh_deals_cache(origins=None):
    """
    Fetch deals from all sources for all origins and write to DB cache.
    Called by the nightly cron job. origins defaults to ORIGIN_HUBS.
    Returns total deals upserted.
    """
    from utilities.postgres_utils import upsert_deals_cache

    if origins is None:
        origins = ORIGIN_HUBS

    total = 0
    for origin in origins:
        logger.info(f"🔄 Refreshing deals cache for {origin}…")
        results = _run_all_sources(origin=origin)
        n = upsert_deals_cache(results)
        total += n
        logger.info(f"✅ {origin}: {n} deals upserted")

    # Also run hotel/activity sources once (not origin-specific)
    # They're already included via _run_all_sources with origin set,
    # but run once standalone to catch any that were missed
    logger.info(f"🔄 Refresh complete — {total} total deals in cache")
    return total
