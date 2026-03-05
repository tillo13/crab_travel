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
from datetime import datetime
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


def get_hot_deals(group_size=15, limit=20, origin=None):
    """
    Fetch hot deals from all sources for a group of ~group_size people.
    If origin is provided, search only from that city/airport.
    Otherwise searches across major global hubs.
    Returns list of deal dicts sorted by price_per_person, with total_for_group added.
    """
    results = []
    lock = threading.Lock()
    threads = []

    # Use provided origin or fan out across major hubs
    origins_to_search = [resolve_iata(origin)] if origin else ORIGIN_HUBS

    # Travelpayouts — one thread per origin
    for orig in origins_to_search:
        t = threading.Thread(
            target=_fetch_tp_deals,
            args=(orig, results, lock),
            daemon=True,
        )
        threads.append(t)
        t.start()

    # Viator special offers
    t = threading.Thread(target=_fetch_viator_deals, args=(results, lock), daemon=True)
    threads.append(t)
    t.start()

    for t in threads:
        t.join(timeout=15)

    # Score + deduplicate by (origin, destination, deal_type)
    seen = set()
    unique = []
    for d in results:
        key = (d["deal_type"], d.get("origin", ""), d["destination"])
        if key not in seen:
            seen.add(key)
            unique.append(d)

    # Sort by price per person
    unique.sort(key=lambda x: x["price_per_person"])

    # Add group totals
    for d in unique:
        d["group_size"] = group_size
        d["total_for_group"] = round(d["price_per_person"] * group_size, 2)

    return unique[:limit]
