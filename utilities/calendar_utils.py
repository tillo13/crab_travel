import logging
from datetime import datetime, timedelta
import requests

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"


def refresh_access_token(refresh_token, client_id, client_secret):
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client_id,
            'client_secret': client_secret,
        })
        if resp.status_code == 200:
            data = resp.json()
            return data.get('access_token')
        logger.error(f"❌ Token refresh failed: {resp.status_code} {resp.text}")
        return None
    except Exception as e:
        logger.error(f"❌ Token refresh error: {e}")
        return None


def get_calendar_events(access_token, time_min, time_max):
    try:
        events = []
        page_token = None
        while True:
            params = {
                'timeMin': time_min.isoformat() + 'Z' if isinstance(time_min, datetime) else time_min,
                'timeMax': time_max.isoformat() + 'Z' if isinstance(time_max, datetime) else time_max,
                'singleEvents': 'true',
                'orderBy': 'startTime',
                'maxResults': 250,
            }
            if page_token:
                params['pageToken'] = page_token

            resp = requests.get(
                f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
                headers={'Authorization': f'Bearer {access_token}'},
                params=params,
            )
            if resp.status_code == 401:
                logger.warning("⚠️ Calendar access token expired")
                return None  # caller should refresh and retry
            if resp.status_code != 200:
                logger.error(f"❌ Calendar API error: {resp.status_code} {resp.text}")
                return []

            data = resp.json()
            for item in data.get('items', []):
                # Skip all-day events that are just reminders, cancelled events
                if item.get('status') == 'cancelled':
                    continue
                start = item.get('start', {})
                end = item.get('end', {})
                events.append({
                    'summary': item.get('summary', '(busy)'),
                    'start': start.get('dateTime') or start.get('date'),
                    'end': end.get('dateTime') or end.get('date'),
                    'all_day': 'date' in start and 'dateTime' not in start,
                })

            page_token = data.get('nextPageToken')
            if not page_token:
                break

        logger.info(f"📅 Fetched {len(events)} calendar events")
        return events
    except Exception as e:
        logger.error(f"❌ Calendar fetch error: {e}")
        return []


def compute_free_windows(events, time_min, time_max, min_days=3):
    busy_dates = set()
    for event in events:
        start_str = event['start']
        end_str = event['end']

        if event['all_day']:
            # All-day event — mark those dates as busy
            d = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_d = datetime.strptime(end_str, '%Y-%m-%d').date()
            while d < end_d:
                busy_dates.add(d)
                d += timedelta(days=1)
        else:
            # Timed event — mark the date as busy
            try:
                dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                busy_dates.add(dt.date())
            except Exception:
                pass

    # Find free windows
    start_date = time_min.date() if isinstance(time_min, datetime) else time_min
    end_date = time_max.date() if isinstance(time_max, datetime) else time_max

    windows = []
    current_start = None
    d = start_date
    while d <= end_date:
        if d not in busy_dates:
            if current_start is None:
                current_start = d
        else:
            if current_start is not None:
                length = (d - current_start).days
                if length >= min_days:
                    windows.append({
                        'start': current_start.isoformat(),
                        'end': (d - timedelta(days=1)).isoformat(),
                        'days': length,
                    })
                current_start = None
        d += timedelta(days=1)

    # Close final window
    if current_start is not None:
        length = (end_date - current_start).days + 1
        if length >= min_days:
            windows.append({
                'start': current_start.isoformat(),
                'end': end_date.isoformat(),
                'days': length,
            })

    logger.info(f"📅 Found {len(windows)} free windows (min {min_days} days)")
    return windows
