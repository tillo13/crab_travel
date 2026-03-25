"""
Xotelo adapter — free hotel prices API.
Pulls real-time rates from Booking.com, Expedia, Hotels.com, Agoda via xotelo.com.
No API key needed. Free tier, no documented rate limits.
"""

import logging
import requests
from utilities.adapters.base import TravelAdapter

logger = logging.getLogger(__name__)

API_BASE = "https://data.xotelo.com/api"


class XoteloAdapter(TravelAdapter):
    source_key = "xotelo"

    def search_hotels(self, destination, checkin, checkout, guests=2):
        """Search Xotelo for hotel prices in a destination."""
        results = []

        try:
            # Step 1: Search for hotels in the destination
            search_resp = requests.get(
                f"{API_BASE}/search",
                params={"query": destination},
                timeout=10,
            )
            if search_resp.status_code != 200:
                logger.warning(f"Xotelo search failed: {search_resp.status_code}")
                return []

            search_data = search_resp.json()
            hotels = (search_data.get("result") or [])[:10]  # Top 10 hotels

            if not hotels:
                logger.info(f"Xotelo: no hotels found for '{destination}'")
                return []

            # Step 2: Get rates for each hotel
            checkin_str = str(checkin) if checkin else None
            checkout_str = str(checkout) if checkout else None

            for hotel in hotels[:5]:  # Limit to 5 to stay fast
                hotel_key = hotel.get("key")
                if not hotel_key:
                    continue

                try:
                    params = {"hotel_key": hotel_key, "currency": "USD"}
                    if checkin_str:
                        params["chk_in"] = checkin_str
                    if checkout_str:
                        params["chk_out"] = checkout_str

                    rates_resp = requests.get(
                        f"{API_BASE}/rates",
                        params=params,
                        timeout=10,
                    )
                    if rates_resp.status_code != 200:
                        continue

                    rates_data = rates_resp.json()
                    rate_result = rates_data.get("result")
                    if not rate_result:
                        continue

                    # Find the cheapest rate across all providers
                    rates = rate_result.get("rates") or []
                    cheapest = None
                    cheapest_source = None
                    for rate in rates:
                        price = rate.get("rate")
                        if price and (cheapest is None or price < cheapest):
                            cheapest = price
                            cheapest_source = rate.get("name", "unknown")

                    if cheapest and cheapest > 0:
                        hotel_name = hotel.get("name", "Unknown Hotel")
                        # Calculate nightly rate
                        nights = 1
                        if checkin_str and checkout_str:
                            from datetime import date
                            try:
                                d1 = date.fromisoformat(checkin_str)
                                d2 = date.fromisoformat(checkout_str)
                                nights = max(1, (d2 - d1).days)
                            except Exception:
                                pass

                        nightly = round(cheapest / nights, 2)
                        deep_link = hotel.get("url") or f"https://www.google.com/travel/hotels?q={destination}"

                        results.append(self.hotel(
                            property_id=hotel_key,
                            name=hotel_name,
                            location=destination,
                            price_per_night_usd=nightly,
                            nights=nights,
                            total_price_usd=cheapest,
                            star_rating=hotel.get("stars"),
                            deep_link=deep_link,
                            bookable=False,
                            raw={"source_provider": cheapest_source, "all_rates": rates},
                        ))

                except Exception as e:
                    logger.debug(f"Xotelo rate check failed for {hotel_key}: {e}")
                    continue

            logger.info(f"🏨 Xotelo hotels in {destination}: {len(results)} with prices")

        except Exception as e:
            logger.warning(f"Xotelo adapter failed: {e}")

        return results
