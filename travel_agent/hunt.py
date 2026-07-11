"""
hunt — flight/hotel options + deep links for a trip. NEVER purchases anything;
every result ends in a deep link the traveler clicks themself.

Data honesty (adapter keys verified 2026-07-10):
  - Travelpayouts: REAL key, free tier. Cached (not live) prices — treat as
    "what the market looks like", confirm on the deep link.
  - Duffel: TEST key → synthetic offers. Skipped here; flip to a live key in
    Secret Manager and wire it back in if real NDC quotes are ever wanted.
  - LiteAPI: SANDBOX key → hotel names/geo are real-ish, prices are not
    bookable-real. Every result is tagged sandbox=True.
  - Hotellook (travelpayouts hotels): dead upstream since 2026-05, returns [].

Run from repo root:
  venv_crab/bin/python -m travel_agent.hunt JFK DEN 2026-09-01 2026-09-04
"""

import sys
from urllib.parse import quote

from utilities.adapters.travelpayouts import TravelpayoutsAdapter
from utilities.adapters.liteapi import LiteAPIAdapter


# ── Flights ───────────────────────────────────────────────────

def flight_links(origin, dest, depart, ret=None):
    """Search-page deep links — these always work; prices are whatever the site shows."""
    kayak = f"https://www.kayak.com/flights/{origin}-{dest}/{depart}"
    if ret:
        kayak += f"/{ret}"
    gf_q = f"Flights from {origin} to {dest} on {depart}"
    if ret:
        gf_q += f" through {ret}"
    return {
        "kayak": kayak,
        "google_flights": f"https://www.google.com/travel/flights?q={quote(gf_q)}",
        "delta": "https://www.delta.com/flight-search/book-a-flight",
        "alaska": "https://www.alaskaair.com/",
    }


# Travelpayouts keys multi-airport metros by CITY code — querying the airport code
# returns an empty dict (verified 2026-07-10: SEA→YYZ empty, SEA→YTO has data).
TP_METRO_CODE = {"YYZ": "YTO", "YTZ": "YTO", "JFK": "NYC", "LGA": "NYC", "EWR": "NYC",
                 "ORD": "CHI", "MDW": "CHI", "LHR": "LON", "LGW": "LON", "STN": "LON",
                 "CDG": "PAR", "ORY": "PAR", "NRT": "TYO", "HND": "TYO",
                 "DCA": "WAS", "IAD": "WAS"}


def hunt_flights(origin, dest, depart, ret=None, passengers=1):
    tp = TravelpayoutsAdapter()
    results = tp.search_flights(origin, dest, depart, ret, passengers)
    if not results and dest in TP_METRO_CODE:
        results = tp.search_flights(origin, TP_METRO_CODE[dest], depart, ret, passengers)
    results.sort(key=lambda r: r["price_usd"])
    return {"results": results, "deep_links": flight_links(origin, dest, depart, ret)}


# ── Hotels ────────────────────────────────────────────────────

def hotel_links(near, checkin, checkout, guests=1):
    """near: address or city string — center the search on the meeting, not downtown."""
    return {
        "google_hotels": f"https://www.google.com/travel/search?q={quote(f'hotels near {near}')}",
        "booking": (f"https://www.booking.com/searchresults.html?ss={quote(near)}"
                    f"&checkin={checkin}&checkout={checkout}&group_adults={guests}"),
    }


def hunt_hotels(city, checkin, checkout, guests=1, near_address=None):
    results = LiteAPIAdapter().search_hotels(city, checkin, checkout, guests)
    for r in results:
        r["sandbox"] = True  # sandbox key — do not trust these prices
    results.sort(key=lambda r: r["price_per_night_usd"])
    return {
        "results": results,
        "deep_links": hotel_links(near_address or city, checkin, checkout, guests),
    }


if __name__ == "__main__":
    # usage: hunt ORIGIN DEST DEPART [RETURN] [CITY]  (CITY for the hotel half)
    origin, dest, depart = sys.argv[1], sys.argv[2], sys.argv[3]
    ret = sys.argv[4] if len(sys.argv) > 4 else None
    city = sys.argv[5] if len(sys.argv) > 5 else dest

    f = hunt_flights(origin, dest, depart, ret)
    print(f"\n✈️  {origin}→{dest} {depart}" + (f" – {ret}" if ret else "") +
          f" — {len(f['results'])} travelpayouts fares (cached)")
    for r in f["results"][:10]:
        print(f"  ${r['price_usd']:>7.2f}  {r['airline']:<4} stops={r['stops']}  "
              f"depart {r['depart_at']}  {r['deep_link']}")
    for name, url in f["deep_links"].items():
        print(f"  🔗 {name}: {url}")

    h = hunt_hotels(city, depart, ret or depart)
    print(f"\n🏨  {city} {depart}–{ret} — {len(h['results'])} liteapi results (SANDBOX prices)")
    for r in h["results"][:8]:
        print(f"  ${r['price_per_night_usd']:>7.2f}/nt  {r['name']}")
    for name, url in h["deep_links"].items():
        print(f"  🔗 {name}: {url}")
