import logging
import requests
import time
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_API_BASE = "https://test.api.amadeus.com"

_token_cache = {'token': None, 'expires_at': 0}


def _get_token():
    now = time.time()
    if _token_cache['token'] and _token_cache['expires_at'] > now + 60:
        return _token_cache['token']

    try:
        client_id = get_secret('AMADEUS_CLIENT_ID')
        client_secret = get_secret('AMADEUS_CLIENT_SECRET')
    except Exception:
        client_id = None
        client_secret = None
    if not client_id or not client_secret:
        logger.warning("⚠️ Amadeus credentials not configured — flight/hotel data unavailable")
        return None

    try:
        resp = requests.post(AMADEUS_AUTH_URL, data={
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
        })
        if resp.status_code == 200:
            data = resp.json()
            _token_cache['token'] = data['access_token']
            _token_cache['expires_at'] = now + data.get('expires_in', 1799)
            return _token_cache['token']
        logger.error(f"❌ Amadeus auth failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"❌ Amadeus auth error: {e}")
    return None


def _api_get(path, params=None):
    token = _get_token()
    if not token:
        return None
    try:
        resp = requests.get(
            f"{AMADEUS_API_BASE}{path}",
            headers={'Authorization': f'Bearer {token}'},
            params=params,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"⚠️ Amadeus API {path}: {resp.status_code}")
        return None
    except Exception as e:
        logger.error(f"❌ Amadeus API error: {e}")
        return None


def search_flights(origin, destination, date, adults=1):
    data = _api_get('/v2/shopping/flight-offers', {
        'originLocationCode': origin,
        'destinationLocationCode': destination,
        'departureDate': date,
        'adults': adults,
        'nonStop': 'false',
        'max': 5,
        'currencyCode': 'USD',
    })
    if not data or 'data' not in data:
        return []

    flights = []
    for offer in data['data']:
        price = float(offer.get('price', {}).get('total', 0))
        segments = offer.get('itineraries', [{}])[0].get('segments', [])
        carrier = segments[0].get('carrierCode', '') if segments else ''
        stops = len(segments) - 1
        flights.append({
            'price_usd': price,
            'price_cents': int(price * 100),
            'carrier': carrier,
            'stops': stops,
            'departure': segments[0].get('departure', {}).get('at', '') if segments else '',
        })
    return flights


def search_flights_multi_origin(origins, destination, date):
    results = {}
    for origin in origins:
        if not origin:
            continue
        flights = search_flights(origin, destination, date)
        if flights:
            cheapest = min(flights, key=lambda f: f['price_usd'])
            results[origin] = {
                'cheapest': cheapest,
                'all': flights,
            }
        else:
            results[origin] = {'cheapest': None, 'all': []}
    return results


def search_hotels(city_code, check_in=None, check_out=None):
    # Step 1: Get hotel list by city
    data = _api_get('/v1/reference-data/locations/hotels/by-city', {
        'cityCode': city_code,
        'radius': 30,
        'radiusUnit': 'KM',
        'hotelSource': 'ALL',
    })
    if not data or 'data' not in data:
        return []

    hotels = []
    for h in data['data'][:10]:  # top 10
        hotels.append({
            'name': h.get('name', ''),
            'hotel_id': h.get('hotelId', ''),
            'lat': h.get('geoCode', {}).get('latitude'),
            'lng': h.get('geoCode', {}).get('longitude'),
        })
    return hotels


def search_activities(lat, lng):
    data = _api_get('/v1/shopping/activities', {
        'latitude': lat,
        'longitude': lng,
        'radius': 20,
    })
    if not data or 'data' not in data:
        return []

    activities = []
    for a in data['data'][:10]:
        price = a.get('price', {})
        activities.append({
            'name': a.get('name', ''),
            'description': a.get('shortDescription', ''),
            'price_amount': price.get('amount', ''),
            'price_currency': price.get('currencyCode', 'USD'),
            'rating': a.get('rating', ''),
            'pictures': [p.strip() for p in (a.get('pictures', []) or [])[:2]],
            'booking_link': a.get('bookingLink', ''),
        })
    return activities


def get_city_iata(city_name):
    """Look up IATA city code from city name."""
    data = _api_get('/v1/reference-data/locations', {
        'subType': 'CITY,AIRPORT',
        'keyword': city_name,
        'page[limit]': 1,
    })
    if data and 'data' in data and data['data']:
        loc = data['data'][0]
        return {
            'iata': loc.get('iataCode', ''),
            'name': loc.get('name', ''),
            'lat': loc.get('geoCode', {}).get('latitude'),
            'lng': loc.get('geoCode', {}).get('longitude'),
        }
    return None


def research_destination(destination_name, member_airports, sample_date=None):
    """Full research: flights from each airport + hotels + activities."""
    if not sample_date:
        from datetime import datetime, timedelta
        sample_date = (datetime.utcnow() + timedelta(days=60)).strftime('%Y-%m-%d')

    # Look up destination IATA (may fail if Amadeus not configured)
    dest_info = get_city_iata(destination_name)
    dest_iata = dest_info['iata'] if dest_info else None

    result = {
        'destination': destination_name,
        'iata': dest_iata,
        'lat': dest_info.get('lat') if dest_info else None,
        'lng': dest_info.get('lng') if dest_info else None,
        'flights': {},
        'hotels': [],
        'activities': [],
    }

    if dest_iata:
        # Search flights from each member's airport
        if member_airports:
            unique_airports = list(set(a for a in member_airports if a))
            result['flights'] = search_flights_multi_origin(unique_airports, dest_iata, sample_date)

        # Search hotels
        result['hotels'] = search_hotels(dest_iata)

        # Search activities
        if dest_info.get('lat') and dest_info.get('lng'):
            result['activities'] = search_activities(dest_info['lat'], dest_info['lng'])
    else:
        logger.warning(f"⚠️ No IATA for {destination_name} — skipping Amadeus lookups")

    # Calculate averages
    flight_prices = [f['cheapest']['price_cents'] for f in result['flights'].values() if f.get('cheapest')]
    result['avg_flight_cost'] = int(sum(flight_prices) / len(flight_prices)) if flight_prices else None

    logger.info(f"🔍 Researched {destination_name}: {len(result['flights'])} airports, {len(result['hotels'])} hotels, {len(result['activities'])} activities")
    return result
