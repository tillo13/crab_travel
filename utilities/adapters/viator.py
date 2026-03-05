"""
Viator adapter — tours, activities, and experiences worldwide.
300,000+ experiences across 200+ countries.

Auth: exp-api-key header (sandbox key prefix: UUID format)
Flow:
  1. GET /destinations/search?searchTerm={city}  → destId
  2. POST /products/search with destId + filters → activity list

Docs: https://docs.viator.com/partner-api/affiliate/technical/
"""

import logging
import requests
from .base import TravelAdapter
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

BASE_URL = "https://api.viator.com/partner"


class ViatorAdapter(TravelAdapter):
    source_key = "viator"

    def __init__(self):
        self._key = None

    @property
    def key(self):
        if not self._key:
            self._key = get_secret("CRAB_VIATOR_API_KEY").strip()
        return self._key

    @property
    def _headers(self):
        return {
            "exp-api-key": self.key,
            "Accept": "application/json;version=2.0",
            "Accept-Language": "en-US",
            "Content-Type": "application/json",
        }

    # ── Activities ────────────────────────────────────────────

    def search_activities(self, destination, checkin=None, checkout=None, guests=2):
        """
        Search activities/experiences for a destination.
        Returns canonical activity dicts sorted by traveler rating.
        """
        results = []
        try:
            dest_id = self._get_dest_id(destination)
            if not dest_id:
                logger.info(f"🎟️  Viator: could not resolve destId for {destination}")
                return results

            products = self._search_products(dest_id, checkin, checkout, limit=20)
            results.extend(products)
            logger.info(f"🎟️  Viator activities {destination}: {len(results)} results")

        except Exception as e:
            logger.warning(f"⚠️  Viator activities failed: {e}")

        return results

    def _get_dest_id(self, destination):
        """Resolve city name to Viator destId."""
        resp = requests.get(
            f"{BASE_URL}/destinations/search",
            headers=self._headers,
            params={"searchTerm": destination, "includeDetails": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        destinations = data.get("destinations", [])
        if not destinations:
            return None
        # Prefer city-type results
        for d in destinations:
            if d.get("destinationType") in ("CITY", "REGION"):
                return d.get("destinationId") or d.get("ref")
        return destinations[0].get("destinationId") or destinations[0].get("ref")

    def _search_products(self, dest_id, checkin, checkout, limit=20):
        """Search products for a destId and return canonical activity dicts."""
        body = {
            "filtering": {
                "destination": str(dest_id),
            },
            "sorting": {
                "sort": "TRAVELER_RATING",
                "order": "DESCENDING",
            },
            "pagination": {
                "start": 1,
                "count": limit,
            },
            "currency": "USD",
        }

        # Add date range if provided
        if checkin and checkout:
            body["filtering"]["dateRange"] = {
                "from": checkin,
                "to": checkout,
            }

        resp = requests.post(
            f"{BASE_URL}/products/search",
            headers=self._headers,
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        products = data.get("products", [])

        results = []
        for p in products:
            try:
                results.append(self._normalize_product(p))
            except Exception as e:
                logger.debug(f"Skipping Viator product: {e}")

        return results

    def _normalize_product(self, p):
        pricing = p.get("pricing", {}).get("summary", {})
        from_price = pricing.get("fromPrice") or pricing.get("fromPriceBeforeDiscount")
        price_usd = float(from_price) if from_price else 0.0

        duration = p.get("duration", {})
        duration_str = self._format_duration(duration)

        rating = p.get("reviewAvgRating") or p.get("reviews", {}).get("combinedAverageRating")
        review_count = p.get("reviewCount") or p.get("reviews", {}).get("totalReviews", 0)

        product_code = p.get("productCode", "")
        deep_link = p.get("productUrl") or self._build_deep_link(product_code)

        return self.activity(
            activity_id=product_code,
            name=p.get("title", ""),
            location=p.get("destination", {}).get("name", ""),
            price_per_person_usd=price_usd,
            duration=duration_str,
            category=self._extract_category(p),
            rating=float(rating) if rating else None,
            review_count=int(review_count) if review_count else None,
            deep_link=deep_link,
            bookable=True,
            raw={
                "product_code": product_code,
                "duration": duration,
                "flags": p.get("flags", []),
                "images": p.get("images", [])[:1],
            },
        )

    def _format_duration(self, duration):
        if not duration:
            return ""
        fixed = duration.get("fixedDurationInMinutes")
        if fixed:
            h, m = divmod(int(fixed), 60)
            return f"{h}h {m}m" if h else f"{m}m"
        from_mins = duration.get("variableDurationFromMinutes")
        to_mins = duration.get("variableDurationToMinutes")
        if from_mins and to_mins:
            fh, fm = divmod(int(from_mins), 60)
            th, tm = divmod(int(to_mins), 60)
            return f"{fh}h–{th}h"
        return ""

    def _extract_category(self, p):
        tags = p.get("tags", [])
        if tags:
            return tags[0].get("allNamesByLocale", {}).get("en", "")
        return ""

    def _build_deep_link(self, product_code):
        return f"https://www.viator.com/tours/{product_code}"
