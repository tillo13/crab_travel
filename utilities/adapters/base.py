"""
Base adapter class — every travel data source implements this interface.
search() returns a list of normalized canonical dicts.
"""

import logging

logger = logging.getLogger(__name__)


class TravelAdapter:
    source_key = None  # e.g. "travelpayouts", "duffel", "liteapi"

    def search_flights(self, origin, destination, depart_date, return_date=None, passengers=1):
        """Returns list of normalized flight dicts."""
        return []

    def search_hotels(self, destination, checkin, checkout, guests=2):
        """Returns list of normalized hotel dicts."""
        return []

    def search_activities(self, destination, date_from=None, date_to=None, interests=None):
        """Returns list of normalized activity dicts."""
        return []

    def search_cars(self, pickup_location, pickup_date, dropoff_date, dropoff_location=None):
        """Returns list of normalized car rental dicts."""
        return []

    # ── Canonical record builders ─────────────────────────────

    def flight(self, origin, destination, depart_at, arrive_at, airline,
               price_usd, stops=0, deep_link=None, bookable=False, raw=None):
        return {
            'type': 'flight',
            'source': self.source_key,
            'canonical_key': f"{origin}-{destination}-{airline}",
            'origin': origin,
            'destination': destination,
            'depart_at': depart_at,
            'arrive_at': arrive_at,
            'airline': airline,
            'stops': stops,
            'price_usd': price_usd,
            'deep_link': deep_link,
            'bookable': bookable,
            'raw': raw or {},
        }

    def hotel(self, property_id, name, location, price_per_night_usd,
              nights=None, total_price_usd=None, star_rating=None,
              deep_link=None, bookable=False, lat=None, lng=None, raw=None):
        return {
            'type': 'hotel',
            'source': self.source_key,
            'canonical_key': f"{self.source_key}_{property_id}",
            'property_id': property_id,
            'name': name,
            'location': location,
            'lat': lat,
            'lng': lng,
            'star_rating': star_rating,
            'price_per_night_usd': price_per_night_usd,
            'nights': nights,
            'total_price_usd': total_price_usd or (price_per_night_usd * nights if nights else None),
            'deep_link': deep_link,
            'bookable': bookable,
            'raw': raw or {},
        }

    def activity(self, activity_id, name, location, price_per_person_usd,
                 duration=None, category=None, rating=None, review_count=None,
                 deep_link=None, bookable=False, raw=None):
        return {
            'type': 'activity',
            'source': self.source_key,
            'canonical_key': f"{self.source_key}_{activity_id}",
            'activity_id': activity_id,
            'name': name,
            'location': location,
            'price_usd': price_per_person_usd,
            'price_per_person_usd': price_per_person_usd,
            'duration': duration,
            'category': category,
            'rating': rating,
            'review_count': review_count,
            'deep_link': deep_link,
            'bookable': bookable,
            'raw': raw or {},
        }

    def car(self, car_id, car_class, pickup_location, pickup_at,
            dropoff_at, price_total_usd, deep_link=None, bookable=False, raw=None):
        return {
            'type': 'car',
            'source': self.source_key,
            'canonical_key': f"{self.source_key}_{car_id}",
            'car_class': car_class,
            'pickup_location': pickup_location,
            'pickup_at': pickup_at,
            'dropoff_at': dropoff_at,
            'price_total_usd': price_total_usd,
            'deep_link': deep_link,
            'bookable': bookable,
            'raw': raw or {},
        }
