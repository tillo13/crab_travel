"""
LiteAPI adapter — hotels + stays, 2M+ properties worldwide.
Sandbox key prefix: sand_   Live key prefix: ltapi_

Two-step flow:
  1. GET /data/hotels?countryCode=US&cityName=Phoenix  → hotel IDs
  2. POST /hotels-rates with IDs + dates              → live prices

Docs: https://docs.liteapi.travel/reference/post_hotels-rates
"""

import logging
import requests
from .base import TravelAdapter
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

BASE_URL = "https://api.liteapi.travel/v3.0"

# City name → ISO country code for the /data/hotels lookup
COUNTRY_MAP = {
    "phoenix": "US", "scottsdale": "US", "new york": "US", "new york city": "US",
    "los angeles": "US", "chicago": "US", "miami": "US", "las vegas": "US",
    "nashville": "US", "denver": "US", "austin": "US", "seattle": "US",
    "san francisco": "US", "boston": "US", "atlanta": "US", "dallas": "US",
    "houston": "US", "portland": "US", "san diego": "US", "orlando": "US",
    "honolulu": "US", "hawaii": "US",
    "helsinki": "FI", "london": "GB", "paris": "FR", "rome": "IT",
    "tokyo": "JP", "cancun": "MX", "toronto": "CA", "sydney": "AU",
    "amsterdam": "NL", "barcelona": "ES", "madrid": "ES", "berlin": "DE",
    "dubai": "AE", "singapore": "SG", "bangkok": "TH",
}


class LiteAPIAdapter(TravelAdapter):
    source_key = "liteapi"

    def __init__(self):
        self._key = None

    @property
    def key(self):
        if not self._key:
            self._key = get_secret("CRAB_LITEAPI_API_KEY")
        return self._key

    @property
    def _headers(self):
        return {
            "X-API-Key": self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Hotels ────────────────────────────────────────────────

    def search_hotels(self, destination, checkin, checkout, guests=2):
        """
        Search hotels in a city and return live rates.
        destination: city name (e.g. 'Phoenix') or country/city
        checkin/checkout: 'YYYY-MM-DD'
        """
        results = []
        try:
            hotel_ids = self._get_hotel_ids(destination, limit=20)
            if not hotel_ids:
                logger.info(f"🏨  LiteAPI: no hotels found for {destination}")
                return results

            rates = self._get_rates(hotel_ids, checkin, checkout, guests)
            results.extend(rates)
            logger.info(f"🏨  LiteAPI hotels {destination}: {len(results)} results")

        except Exception as e:
            logger.warning(f"⚠️  LiteAPI hotels failed: {e}")

        return results

    def _get_hotel_ids(self, destination, limit=20):
        """Step 1 — find hotel IDs for a city."""
        city = destination.strip()
        country = COUNTRY_MAP.get(city.lower(), "US")

        resp = requests.get(
            f"{BASE_URL}/data/hotels",
            headers=self._headers,
            params={
                "countryCode": country,
                "cityName": city,
                "limit": limit,
            },
            timeout=15,
        )
        resp.raise_for_status()
        hotels = resp.json().get("data", [])
        return [h["id"] for h in hotels if h.get("id")]

    def _get_rates(self, hotel_ids, checkin, checkout, guests):
        """Step 2 — get live rates for those hotel IDs."""
        try:
            from datetime import datetime
            nights = (
                datetime.strptime(checkout, "%Y-%m-%d") -
                datetime.strptime(checkin, "%Y-%m-%d")
            ).days
        except Exception:
            nights = None

        resp = requests.post(
            f"{BASE_URL}/hotels-rates",
            headers=self._headers,
            json={
                "hotelIds": hotel_ids,
                "checkin": checkin,
                "checkout": checkout,
                "currency": "USD",
                "guestNationality": "US",
                "occupancies": [{"adults": max(1, guests)}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])

        results = []
        for hotel in data:
            try:
                hotel_id = hotel.get("hotelId", "")
                room_types = hotel.get("roomTypes", [])
                if not room_types:
                    continue

                # Take cheapest room type
                cheapest = None
                for room in room_types:
                    for rate in room.get("rates", []):
                        retail = rate.get("retailRate", {})
                        totals = retail.get("total", [])
                        if totals:
                            amt = float(totals[0].get("amount", 0))
                            if amt and (cheapest is None or amt < cheapest["price"]):
                                cheapest = {
                                    "price": amt,
                                    "room_name": rate.get("name", ""),
                                    "board": rate.get("boardName", ""),
                                    "rate_id": rate.get("rateId", ""),
                                    "offer_id": room.get("offerId", ""),
                                }

                if not cheapest:
                    continue

                price_per_night = round(cheapest["price"] / nights, 2) if nights else cheapest["price"]
                deep_link = self._build_deep_link(hotel_id, checkin, checkout, guests)

                results.append(self.hotel(
                    property_id=hotel_id,
                    name=hotel_id,  # Name from /data/hotels step; rates don't include it
                    location="",
                    price_per_night_usd=price_per_night,
                    nights=nights,
                    total_price_usd=cheapest["price"],
                    deep_link=deep_link,
                    bookable=True,
                    raw={
                        "hotel_id": hotel_id,
                        "offer_id": cheapest["offer_id"],
                        "rate_id": cheapest["rate_id"],
                        "room_name": cheapest["room_name"],
                        "board": cheapest["board"],
                        "total": cheapest["price"],
                        "checkin": checkin,
                        "checkout": checkout,
                    },
                ))
            except Exception as e:
                logger.debug(f"Skipping hotel {hotel.get('hotelId')}: {e}")

        return results

    def _build_deep_link(self, hotel_id, checkin, checkout, guests):
        return (
            f"https://app.liteapi.travel/hotels/{hotel_id}"
            f"?checkin={checkin}&checkout={checkout}&adults={guests}"
        )
