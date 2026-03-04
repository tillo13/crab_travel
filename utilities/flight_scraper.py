import logging
import requests
import re
import json
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"


def _get_api_key():
    try:
        return get_secret('SCRAPINGBEE_API_KEY')
    except Exception:
        return None


def _build_google_flights_url(origin, destination, date, return_date=None):
    """Build a Google Flights search URL."""
    # Google Flights URL format: /travel/flights?hl=en#flt=ORIGIN.DEST.DATE;c:USD;e:1;sd:1;t:f
    url = f"https://www.google.com/travel/flights?hl=en#flt={origin}.{destination}.{date}"
    if return_date:
        url += f"*{destination}.{origin}.{return_date}"
    url += ";c:USD;e:1;sd:1;t:f"
    return url


def _parse_flight_prices(html):
    """Extract flight data from Google Flights HTML."""
    flights = []

    # Look for price patterns — Google Flights shows prices like "$XXX"
    # Multiple parsing strategies for robustness
    price_pattern = re.compile(r'\$(\d{1,2},?\d{3}|\d{2,3})')
    prices_found = price_pattern.findall(html)

    # Clean prices
    clean_prices = []
    for p in prices_found:
        try:
            val = int(p.replace(',', ''))
            if 30 <= val <= 15000:  # reasonable flight price range
                clean_prices.append(val)
        except ValueError:
            pass

    # Deduplicate and sort
    clean_prices = sorted(set(clean_prices))

    if clean_prices:
        flights.append({
            'cheapest_price': clean_prices[0],
            'prices_found': clean_prices[:10],
            'price_range': f"${clean_prices[0]}–${clean_prices[-1]}" if len(clean_prices) > 1 else f"${clean_prices[0]}",
        })

    # Try to extract airline names
    airline_pattern = re.compile(r'(?:operated by |with )?(United|Delta|American|Southwest|JetBlue|Alaska|Spirit|Frontier|Hawaiian|Sun Country|Norse|Icelandair|British Airways|Lufthansa|Air France|KLM|Emirates|Qatar|Turkish|Finnair|SAS|Norwegian|Ryanair|easyJet|WestJet|Air Canada)', re.IGNORECASE)
    airlines = list(set(airline_pattern.findall(html)))

    if flights:
        flights[0]['airlines'] = airlines[:5]

    return flights, clean_prices


def scrape_flights(origin, destination, date):
    """Scrape Google Flights for a specific route and date."""
    api_key = _get_api_key()
    if not api_key:
        logger.warning("⚠️ ScrapingBee API key not configured")
        return None

    url = _build_google_flights_url(origin, destination, date)

    try:
        resp = requests.get(SCRAPINGBEE_URL, params={
            'api_key': api_key,
            'url': url,
            'render_js': 'true',
            'wait': '5000',  # wait 5s for JS to load flight results
        }, timeout=60)

        if resp.status_code == 200:
            html = resp.text
            flights, prices = _parse_flight_prices(html)
            logger.info(f"✈️ Scraped {origin}→{destination} on {date}: {len(prices)} prices found")
            return {
                'origin': origin,
                'destination': destination,
                'date': date,
                'flights': flights,
                'cheapest': prices[0] if prices else None,
                'source': 'google_flights',
            }
        else:
            logger.error(f"❌ ScrapingBee error: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"❌ Flight scrape failed {origin}→{destination}: {e}")
        return None


def scrape_flights_multi_origin(origins, destination, date):
    """Scrape flights from multiple origin airports to one destination."""
    results = {}
    for origin in origins:
        if not origin:
            continue
        data = scrape_flights(origin, destination, date)
        if data:
            results[origin] = data
        else:
            results[origin] = {'origin': origin, 'destination': destination, 'cheapest': None, 'flights': []}
    return results


def research_destination_flights(destination_iata, member_airports, sample_date=None):
    """Get flight pricing from all member airports to a destination."""
    if not sample_date:
        from datetime import datetime, timedelta
        sample_date = (datetime.utcnow() + timedelta(days=60)).strftime('%Y-%m-%d')

    if not member_airports:
        return {}

    unique_airports = list(set(a for a in member_airports if a))
    return scrape_flights_multi_origin(unique_airports, destination_iata, sample_date)
