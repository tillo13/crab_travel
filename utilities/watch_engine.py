"""
Watch engine — auto-creates per-member price watches when a plan locks,
checks prices on a cron schedule, and sends alerts on price drops.

Usage:
    from utilities.watch_engine import create_watches_for_plan, check_all_watches
    create_watches_for_plan(plan_id)   # called on plan lock
    check_all_watches()                 # called by /tasks/check-watches cron
"""

import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from utilities.postgres_utils import (
    get_plan_by_id, get_plan_members, get_all_plan_preferences,
    create_member_watch, get_active_watches, update_watch_price,
    get_watch_history,
)
from utilities.search_engine import _destination_iata
from utilities.adapters.duffel import DuffelAdapter
from utilities.adapters.liteapi import LiteAPIAdapter
from utilities.adapters.travelpayouts import TravelpayoutsAdapter
from utilities.adapters.xotelo import XoteloAdapter

logger = logging.getLogger(__name__)


def create_watches_for_plan(plan_id):
    """Auto-create flight + hotel watches for every member when a plan locks."""
    plan = get_plan_by_id(plan_id)
    if not plan:
        logger.error(f"Watch create: plan {plan_id} not found")
        return 0

    destination = plan.get('locked_destination') or plan.get('destination')
    checkin = plan.get('locked_start_date') or plan.get('start_date')
    checkout = plan.get('locked_end_date') or plan.get('end_date')

    if not destination:
        logger.warning(f"Watch create: plan {plan_id} has no destination, skipping")
        return 0

    members = get_plan_members(plan_id)
    prefs = get_all_plan_preferences(plan_id)
    prefs_by_member = {p['member_id']: p for p in prefs}

    created = 0
    for member in members:
        member_id = member['pk_id']
        pref = prefs_by_member.get(member_id, {})
        budget_max = pref.get('budget_max')

        # Flight watch — needs a home airport
        home_airport = member.get('home_airport') or member.get('user_home_airport')
        if home_airport:
            watch = create_member_watch(
                plan_id=plan_id, member_id=member_id, watch_type='flight',
                origin=home_airport, destination=destination,
                checkin=checkin, checkout=checkout, budget_max=budget_max,
            )
            if watch:
                created += 1
                logger.info(f"Watch created: flight {home_airport}→{destination} for {member['display_name']}")
        else:
            logger.warning(f"Watch skip: no home airport for {member['display_name']}, skipping flight watch")

        # Hotel watch
        watch = create_member_watch(
            plan_id=plan_id, member_id=member_id, watch_type='hotel',
            destination=destination, checkin=checkin, checkout=checkout,
            budget_max=budget_max,
        )
        if watch:
            created += 1
            logger.info(f"Watch created: hotel in {destination} for {member['display_name']}")

    logger.info(f"Watch create complete: {created} watches for plan {plan_id}")
    return created


def check_all_watches():
    """Check prices for all active watches. Called by cron.

    Prioritization:
    1. Trips departing within 14 days (urgent)
    2. Trips departing within 30 days
    3. Everything else
    Skips watches departing 60+ days out to conserve API calls.
    """
    watches = get_active_watches()
    if not watches:
        logger.info("Watch check: no active watches")
        return {'checked': 0, 'alerts_sent': 0, 'errors': []}

    today = date.today()

    # Group watches to minimize API calls
    flight_groups = defaultdict(list)
    hotel_groups = defaultdict(list)

    for w in watches:
        # Skip watches 60+ days out — prices are too volatile to be useful
        checkin = w.get('checkin')
        if checkin:
            if isinstance(checkin, str):
                checkin = date.fromisoformat(checkin)
            days_out = (checkin - today).days
            if days_out > 60:
                continue
            if days_out < 0:
                continue  # Past departure, skip

        if w['watch_type'] == 'flight' and w.get('origin'):
            key = (w['origin'], w['destination'], str(w['checkin']), str(w['checkout']))
            flight_groups[key].append(w)
        elif w['watch_type'] == 'hotel':
            key = (w['destination'], str(w['checkin']), str(w['checkout']))
            hotel_groups[key].append(w)

    # Sort groups by departure date (soonest first)
    def _sort_key(group_key):
        checkin_str = group_key[2] if len(group_key) > 2 else '9999-12-31'
        return checkin_str

    sorted_flight_keys = sorted(flight_groups.keys(), key=_sort_key)
    sorted_hotel_keys = sorted(hotel_groups.keys(), key=_sort_key)

    checked = 0
    alerts_sent = 0
    errors = []

    # Cap: 150 searches per run (free APIs can handle it)
    search_count = 0
    max_searches = 150

    for key in sorted_flight_keys:
        origin, destination, checkin, checkout = key
        group_watches = flight_groups[key]
        if search_count >= max_searches:
            logger.warning(f"Watch check: hit {max_searches} search cap, deferring remaining")
            break
        search_count += 1

        best_flight = _search_best_flight(origin, destination, checkin, checkout)
        if best_flight:
            for w in group_watches:
                old_price = float(w['last_price_usd']) if w['last_price_usd'] else None
                updated = update_watch_price(
                    w['pk_id'], best_flight['price_usd'],
                    deep_link=best_flight.get('deep_link'),
                    data=best_flight, source=best_flight.get('source', 'unknown'),
                )
                checked += 1
                if updated and _should_alert(old_price, best_flight['price_usd'], w['alert_threshold_pct']):
                    _send_alert(w, old_price, best_flight['price_usd'], best_flight.get('deep_link'))
                    alerts_sent += 1
                # Compute AI recommendation after price update
                try:
                    rec = compute_recommendation(w)
                    _update_recommendation(w['pk_id'], rec)
                except Exception as e:
                    logger.warning(f"Recommendation failed for watch {w['pk_id']}: {e}")

        time.sleep(1)  # Rate limiting between searches

    for key in sorted_hotel_keys:
        destination, checkin, checkout = key
        group_watches = hotel_groups[key]
        if search_count >= max_searches:
            logger.warning(f"Watch check: hit {max_searches} search cap, deferring remaining")
            break
        search_count += 1

        best_hotel = _search_best_hotel(destination, checkin, checkout)
        if best_hotel:
            for w in group_watches:
                old_price = float(w['last_price_usd']) if w['last_price_usd'] else None
                price = best_hotel.get('price_per_night_usd') or best_hotel.get('price_usd')
                if price:
                    updated = update_watch_price(
                        w['pk_id'], price,
                        deep_link=best_hotel.get('deep_link'),
                        data=best_hotel, source=best_hotel.get('source', 'unknown'),
                    )
                    checked += 1
                    if updated and _should_alert(old_price, price, w['alert_threshold_pct']):
                        _send_alert(w, old_price, price, best_hotel.get('deep_link'))
                        alerts_sent += 1
                    # Compute AI recommendation after price update
                    try:
                        rec = compute_recommendation(w)
                        _update_recommendation(w['pk_id'], rec)
                    except Exception as e:
                        logger.warning(f"Recommendation failed for watch {w['pk_id']}: {e}")

        time.sleep(1)

    summary = {'checked': checked, 'alerts_sent': alerts_sent, 'errors': errors}
    logger.info(f"Watch check complete: {summary}")
    return summary


def _search_best_flight(origin, destination, checkin, checkout):
    """Search adapters for the cheapest flight on this route.
    Order: free APIs first (Travelpayouts), Duffel last (has excess search fees).
    """
    dest_iata = _destination_iata(destination)
    best = None

    for AdapterClass in [TravelpayoutsAdapter, DuffelAdapter]:
        try:
            adapter = AdapterClass()
            flights = adapter.search_flights(
                origin=origin, destination=dest_iata,
                depart_date=checkin, return_date=checkout,
            )
            for f in flights:
                if f.get('price_usd') and (best is None or f['price_usd'] < best['price_usd']):
                    best = f
        except Exception as e:
            logger.warning(f"Watch flight search {AdapterClass.source_key} failed: {e}")

    return best


def _search_best_hotel(destination, checkin, checkout):
    """Search adapters for the cheapest hotel in this destination.
    Order: Xotelo (free, real prices) → Travelpayouts (free) → LiteAPI (sandbox).
    """
    best = None

    for AdapterClass in [XoteloAdapter, TravelpayoutsAdapter, LiteAPIAdapter]:
        try:
            adapter = AdapterClass()
            hotels = adapter.search_hotels(
                destination=destination, checkin=checkin, checkout=checkout,
            )
            for h in hotels:
                price = h.get('price_per_night_usd') or h.get('price_usd')
                if price and (best is None or price < (best.get('price_per_night_usd') or best.get('price_usd', float('inf')))):
                    best = h
        except Exception as e:
            logger.warning(f"Watch hotel search {AdapterClass.source_key} failed: {e}")

    return best


def _should_alert(old_price, new_price, threshold_pct):
    """Check if price dropped enough to trigger an alert."""
    if old_price is None or new_price is None:
        return False
    if old_price <= 0:
        return False
    drop_pct = ((old_price - new_price) / old_price) * 100
    return drop_pct >= (threshold_pct or 10)


def compute_recommendation(watch, history=None):
    """Analyze price history + timing and return a booking recommendation.

    Returns dict: {verdict, reason, trend, scans, computed_at}
    verdict is one of: 'book_now', 'book_soon', 'wait', 'watching'
    """
    if history is None:
        history = get_watch_history(watch['pk_id'], limit=50)

    prices = [float(h['price_usd']) for h in reversed(history)] if history else []
    n_scans = len(prices)
    now = datetime.now(timezone.utc).date()

    # Days until departure
    checkin = watch.get('checkin')
    if isinstance(checkin, str):
        checkin = date.fromisoformat(checkin)
    days_out = (checkin - now).days if checkin else 999

    current_price = prices[-1] if prices else None
    best_price = float(watch['best_price_usd']) if watch.get('best_price_usd') else current_price

    if not current_price or n_scans < 2:
        return {
            'verdict': 'watching',
            'reason': f'Gathering data — {n_scans} scan{"s" if n_scans != 1 else ""} so far. Need a few more to spot trends.',
            'trend': 'flat',
            'scans': n_scans,
            'computed_at': datetime.now(timezone.utc).isoformat(),
        }

    # Compute trend from recent prices
    recent = prices[-min(5, n_scans):]
    if len(recent) >= 2:
        avg_first_half = sum(recent[:len(recent)//2]) / max(len(recent)//2, 1)
        avg_second_half = sum(recent[len(recent)//2:]) / max(len(recent) - len(recent)//2, 1)
        pct_change = ((avg_second_half - avg_first_half) / avg_first_half) * 100 if avg_first_half > 0 else 0
    else:
        pct_change = 0

    if pct_change < -3:
        trend = 'dropping'
    elif pct_change > 3:
        trend = 'rising'
    else:
        trend = 'stable'

    # How close to best price?
    pct_from_best = ((current_price - best_price) / best_price * 100) if best_price > 0 else 0

    # Consecutive drops
    consecutive_drops = 0
    for i in range(len(prices) - 1, 0, -1):
        if prices[i] < prices[i - 1]:
            consecutive_drops += 1
        else:
            break

    # Decision logic
    verdict = 'wait'
    reason = ''

    watch_type_label = 'flight' if watch.get('watch_type') == 'flight' else 'hotel'

    if days_out <= 7:
        verdict = 'book_now'
        reason = f'Only {days_out} days until departure. {watch_type_label.title()} prices almost never drop this close to travel.'
    elif days_out <= 14:
        if trend == 'dropping':
            verdict = 'book_soon'
            reason = f'Prices are dropping but you\'re {days_out} days out. Could dip a bit more, but don\'t wait too long.'
        else:
            verdict = 'book_now'
            reason = f'{days_out} days out and prices are {trend}. This is the booking window — waiting is risky.'
    elif pct_from_best <= 2 and n_scans >= 4:
        verdict = 'book_now'
        if pct_from_best == 0:
            reason = f'This is the lowest price we\'ve seen across {n_scans} scans. Strong buy signal.'
        else:
            reason = f'Within 2% of the best price we\'ve tracked. After {n_scans} scans, this is a good deal.'
    elif trend == 'dropping' and consecutive_drops >= 3:
        verdict = 'wait'
        reason = f'Prices dropped {consecutive_drops} scans in a row (↓{abs(pct_change):.0f}%). Trend is in your favor — let it ride.'
    elif trend == 'dropping':
        verdict = 'wait'
        reason = f'Prices trending down over recent scans. {days_out} days out gives you room to wait for a better deal.'
    elif trend == 'rising' and days_out > 30:
        verdict = 'book_soon'
        reason = f'Prices are climbing (↑{pct_change:.0f}% recently). Still {days_out} days out, but the trend isn\'t great.'
    elif trend == 'rising' and days_out <= 30:
        verdict = 'book_now'
        reason = f'Prices rising and only {days_out} days out. The longer you wait, the more you\'ll pay.'
    elif trend == 'stable' and n_scans >= 6:
        verdict = 'book_soon'
        reason = f'Prices have been flat across {n_scans} scans. Unlikely to drop much — book when ready.'
    else:
        verdict = 'wait'
        reason = f'{n_scans} scans over {days_out} days out. Prices look {trend} — watching for a better entry point.'

    return {
        'verdict': verdict,
        'reason': reason,
        'trend': trend,
        'scans': n_scans,
        'current_price': current_price,
        'best_price': best_price,
        'pct_from_best': round(pct_from_best, 1),
        'days_out': days_out,
        'computed_at': datetime.now(timezone.utc).isoformat(),
    }


def _update_recommendation(watch_id, recommendation):
    """Store the computed recommendation in the member_watches table."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.member_watches SET recommendation = %s WHERE pk_id = %s
        """, (psycopg2.extras.Json(recommendation), watch_id))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Update recommendation failed for watch {watch_id}: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _send_alert(watch, old_price, new_price, deep_link=None):
    """Send price drop notification via email and/or SMS."""
    try:
        from utilities.gmail_utils import send_simple_email
        from utilities.sms_utils import send_sms

        member_name = watch.get('member_name', 'Traveler')
        origin = watch.get('origin', '')
        destination = watch.get('destination', '')

        if watch['watch_type'] == 'flight':
            subject = f"Price drop: {origin}→{destination} now ${new_price:.0f}"
            body = (
                f"Hey {member_name}!\n\n"
                f"Your {origin} → {destination} flight dropped to ${new_price:.2f} "
                f"(was ${old_price:.2f}).\n\n"
            )
        else:
            subject = f"Price drop: Hotels in {destination} now ${new_price:.0f}/night"
            body = (
                f"Hey {member_name}!\n\n"
                f"Hotels in {destination} dropped to ${new_price:.2f}/night "
                f"(was ${old_price:.2f}/night).\n\n"
            )

        if deep_link:
            body += f"Book now: {deep_link}\n\n"
        body += "— crab.travel"

        # Email
        email = watch.get('user_email')
        if email:
            send_simple_email(subject, body, email)
            logger.info(f"Watch alert email sent to {email}: {subject}")

        # SMS
        phone = watch.get('phone_number')
        notify_channel = watch.get('notify_channel', 'email')
        if phone and notify_channel in ('sms', 'both'):
            sms_body = f"[crab.travel] {subject}"
            if deep_link:
                sms_body += f" Book: {deep_link}"
            send_sms(phone, sms_body[:160])
            logger.info(f"Watch alert SMS sent to {phone}")

    except Exception as e:
        logger.error(f"Watch alert failed for watch {watch.get('pk_id')}: {e}")
