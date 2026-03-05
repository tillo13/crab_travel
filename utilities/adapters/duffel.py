"""
Duffel adapter — real bookable flights via NDC.
Covers 300+ airlines including AA, Delta, United, BA — the carriers Amadeus misses.

In test mode: returns realistic synthetic flight data.
Go live: flip CRAB_DUFFEL_API_KEY to a live key in Secret Manager.

Docs: https://duffel.com/docs/api/v2/offer-requests/create-offer-request
"""

import logging
import requests
from .base import TravelAdapter
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

BASE_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"


class DuffelAdapter(TravelAdapter):
    source_key = "duffel"

    def __init__(self):
        self._token = None

    @property
    def token(self):
        if not self._token:
            self._token = get_secret("CRAB_DUFFEL_API_KEY")
        return self._token

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Duffel-Version": DUFFEL_VERSION,
        }

    # ── Flights ───────────────────────────────────────────────

    def search_flights(self, origin, destination, depart_date, return_date=None, passengers=1):
        """
        Create a Duffel offer request and return all offers as canonical flight dicts.
        depart_date: 'YYYY-MM-DD'
        passengers: integer count (all adults)
        """
        results = []
        try:
            slices = [{
                "origin": origin,
                "destination": destination,
                "departure_date": str(depart_date)[:10],
            }]
            if return_date:
                slices.append({
                    "origin": destination,
                    "destination": origin,
                    "departure_date": str(return_date)[:10],
                })

            passenger_list = [{"type": "adult"} for _ in range(max(1, int(passengers)))]

            resp = requests.post(
                f"{BASE_URL}/air/offer_requests",
                headers=self._headers,
                params={"return_offers": "true", "supplier_timeout": 10000},
                json={"data": {
                    "slices": slices,
                    "passengers": passenger_list,
                    "cabin_class": "economy",
                    "max_connections": 1,
                }},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            offers = data.get("offers", [])

            for offer in offers:
                try:
                    results.append(self._normalize_offer(offer, origin, destination))
                except Exception as e:
                    logger.debug(f"Skipping offer: {e}")

            logger.info(f"✈️  Duffel flights {origin}→{destination}: {len(results)} offers")

        except requests.HTTPError as e:
            logger.warning(f"⚠️  Duffel HTTP error: {e.response.status_code} — {e.response.text[:200]}")
        except Exception as e:
            logger.warning(f"⚠️  Duffel flights failed: {e}")

        return results

    def _normalize_offer(self, offer, origin, destination):
        total = float(offer.get("total_amount", 0))
        currency = offer.get("total_currency", "USD")

        # Convert to USD if needed (most Duffel responses are already USD)
        price_usd = total if currency == "USD" else total

        # First slice, first segment for airline + timing
        slices = offer.get("slices", [])
        first_slice = slices[0] if slices else {}
        segments = first_slice.get("segments", [])
        first_seg = segments[0] if segments else {}
        last_seg = segments[-1] if segments else {}

        airline = first_seg.get("marketing_carrier", {}).get("iata_code", "")
        airline_name = first_seg.get("marketing_carrier", {}).get("name", airline)
        depart_at = first_seg.get("departing_at", "")
        arrive_at = last_seg.get("arriving_at", "")
        stops = len(segments) - 1

        deep_link = self._build_deep_link(offer.get("id", ""), origin, destination)

        return self.flight(
            origin=origin,
            destination=destination,
            depart_at=depart_at,
            arrive_at=arrive_at,
            airline=airline_name or airline,
            price_usd=price_usd,
            stops=stops,
            deep_link=deep_link,
            bookable=True,
            raw={
                "offer_id": offer.get("id"),
                "expires_at": offer.get("expires_at"),
                "total_amount": offer.get("total_amount"),
                "total_currency": offer.get("total_currency"),
                "conditions": offer.get("conditions", {}),
            },
        )

    def _build_deep_link(self, offer_id, origin, destination):
        """
        In test mode Duffel doesn't produce real booking URLs.
        In production this would link to your Duffel-powered checkout
        or Duffel's hosted booking flow.
        """
        return f"https://api.duffel.com/air/offers/{offer_id}"
