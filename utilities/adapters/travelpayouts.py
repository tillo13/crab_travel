"""
Travelpayouts adapter — flights (Aviasales) + hotels (Hotellook).

Travelpayouts is the fastest integration: one API key covers 100+ travel brands.
Data is cached (not real-time), but comprehensive and free.
Deep links carry our marker (708186) for affiliate commission tracking.

Docs:
  Flights: https://support.travelpayouts.com/hc/en-us/categories/200358578
  Hotels:  https://support.travelpayouts.com/hc/en-us/articles/360004758251
"""

import logging
import requests
from .base import TravelAdapter
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

MARKER = "708186"
FLIGHTS_BASE = "https://api.travelpayouts.com"
HOTELS_BASE  = "https://engine.hotellook.com/api/v2"


class TravelpayoutsAdapter(TravelAdapter):
    source_key = "travelpayouts"

    def __init__(self):
        self._token = None

    @property
    def token(self):
        if not self._token:
            self._token = get_secret("CRAB_TRAVELPAYOUTS_API_KEY")
        return self._token

    # ── Flights ───────────────────────────────────────────────

    def search_flights(self, origin, destination, depart_date, return_date=None, passengers=1):
        """
        Fetch cheapest flight prices for a route + month.
        depart_date: 'YYYY-MM' or 'YYYY-MM-DD'
        Returns list of canonical flight dicts.
        """
        results = []
        try:
            # Use month-level search for price calendar (best for trip planning)
            month = str(depart_date)[:7]  # 'YYYY-MM'
            resp = requests.get(
                f"{FLIGHTS_BASE}/v1/prices/cheap",
                params={
                    "origin": origin,
                    "destination": destination,
                    "depart_date": month,
                    "return_date": str(return_date)[:7] if return_date else None,
                    "token": self.token,
                    "currency": "usd",
                    "one_way": "false" if return_date else "true",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})

            # Response is keyed by destination IATA
            dest_data = data.get(destination, {})
            for fare_key, fare in dest_data.items():
                price = fare.get("price")
                airline = fare.get("airline", "")
                depart = fare.get("depart_date", "")
                ret = fare.get("return_date", "")
                if not price:
                    continue

                deep_link = self._flight_link(origin, destination, depart, ret, passengers)
                results.append(self.flight(
                    origin=origin,
                    destination=destination,
                    depart_at=depart,
                    arrive_at=None,
                    airline=airline,
                    price_usd=float(price),
                    stops=fare.get("number_of_changes", 0),
                    deep_link=deep_link,
                    bookable=False,
                    raw=fare,
                ))

            logger.info(f"✈️  Travelpayouts flights {origin}→{destination}: {len(results)} results")
        except Exception as e:
            # 400/404 on free-text destinations are expected. Keep other errors loud.
            err = str(e)
            if '400' in err or '404' in err:
                logger.info(f"ℹ️   Travelpayouts flights no-match for {origin}→{destination} ({err[:80]})")
            else:
                logger.warning(f"⚠️  Travelpayouts flights failed: {e}")
        return results

    def _flight_link(self, origin, destination, depart_date, return_date, passengers):
        """Build Aviasales affiliate search URL."""
        # Format: aviasales.com/search/NYC1009PHX (origin + day+month + dest)
        try:
            from datetime import datetime
            d = datetime.strptime(depart_date[:10], "%Y-%m-%d")
            depart_str = d.strftime("%d%m")
        except Exception:
            depart_str = ""

        ret_str = ""
        if return_date:
            try:
                from datetime import datetime
                r = datetime.strptime(str(return_date)[:10], "%Y-%m-%d")
                ret_str = r.strftime("%d%m")
            except Exception:
                pass

        path = f"{origin}{depart_str}{destination}{ret_str}{passengers}"
        return f"https://www.aviasales.com/search/{path}?marker={MARKER}"

    # ── Hotels ────────────────────────────────────────────────

    def _get_location_id(self, destination):
        """Resolve city name to Hotellook locationId via lookup API."""
        resp = requests.get(
            f"{HOTELS_BASE}/lookup.json",
            params={"query": destination, "lang": "en", "lookFor": "city", "token": self.token},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", {})
        cities = results.get("cities", [])
        if not cities:
            return None
        return cities[0].get("id")

    def search_hotels(self, destination, checkin, checkout, guests=2):
        """
        Fetch hotel prices via Hotellook cache API.
        destination: city name (e.g. 'Phoenix') or IATA city code
        checkin/checkout: 'YYYY-MM-DD'
        Returns list of canonical hotel dicts.
        """
        results = []
        try:
            location_id = self._get_location_id(destination)
            if not location_id:
                logger.info(f"🏨  Travelpayouts: could not resolve locationId for {destination}")
                return results

            resp = requests.get(
                f"{HOTELS_BASE}/cache.json",
                params={
                    "locationId": location_id,
                    "checkIn": checkin,
                    "checkOut": checkout,
                    "adultsCount": guests,
                    "limit": 25,
                    "token": self.token,
                    "currency": "usd",
                    "sortBy": "price",
                    "sortAsc": 1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            hotels = resp.json()

            checkin_dt = checkin
            checkout_dt = checkout
            try:
                from datetime import datetime
                nights = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days
            except Exception:
                nights = None

            for h in hotels:
                price_per_night = h.get("priceFrom")
                if not price_per_night:
                    continue
                hotel_id = str(h.get("id", ""))
                name = h.get("name", "")
                deep_link = self._hotel_link(destination, checkin, checkout, guests)
                results.append(self.hotel(
                    property_id=hotel_id,
                    name=name,
                    location=destination,
                    price_per_night_usd=float(price_per_night),
                    nights=nights,
                    star_rating=h.get("stars"),
                    lat=h.get("location", {}).get("lat"),
                    lng=h.get("location", {}).get("lon"),
                    deep_link=deep_link,
                    bookable=False,
                    raw=h,
                ))

            logger.info(f"🏨  Travelpayouts hotels {destination}: {len(results)} results")
        except Exception as e:
            # 404s on the lookup endpoint are a known, expected failure mode
            # for free-text destination names (it wants IATA codes). Keep
            # other errors at WARNING so real outages still surface.
            if '404' in str(e):
                logger.info(f"ℹ️   Travelpayouts hotels no-match for {destination!r} (404 — expected for free-text destinations)")
            else:
                logger.warning(f"⚠️  Travelpayouts hotels failed: {e}")
        return results

    def _hotel_link(self, destination, checkin, checkout, guests):
        """Build Hotellook affiliate search URL."""
        from urllib.parse import quote
        dest_encoded = quote(destination)
        return (
            f"https://www.hotellook.com/hotels?destination={dest_encoded}"
            f"&checkIn={checkin}&checkOut={checkout}&adultsCount={guests}&marker={MARKER}"
        )
