"""
Watch engine — auto-creates per-member price watches when a plan locks,
checks prices on a cron schedule, and sends alerts on price drops.

Usage:
    from utilities.watch_engine import create_watches_for_plan, check_all_watches
    create_watches_for_plan(plan_id)   # called on plan lock
    check_all_watches()                 # called by /tasks/check-watches cron
"""

import logging
import time
from collections import defaultdict
from utilities.postgres_utils import (
    get_plan_by_id, get_plan_members, get_all_plan_preferences,
    create_member_watch, get_active_watches, update_watch_price,
)
from utilities.search_engine import _destination_iata
from utilities.adapters.duffel import DuffelAdapter
from utilities.adapters.liteapi import LiteAPIAdapter
from utilities.adapters.travelpayouts import TravelpayoutsAdapter

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
    """Check prices for all active watches. Called by cron every 6 hours."""
    watches = get_active_watches()
    if not watches:
        logger.info("Watch check: no active watches")
        return {'checked': 0, 'alerts_sent': 0, 'errors': []}

    # Group watches to minimize API calls
    flight_groups = defaultdict(list)  # (origin, destination, checkin, checkout) → [watches]
    hotel_groups = defaultdict(list)   # (destination, checkin, checkout) → [watches]

    for w in watches:
        if w['watch_type'] == 'flight' and w.get('origin'):
            key = (w['origin'], w['destination'], str(w['checkin']), str(w['checkout']))
            flight_groups[key].append(w)
        elif w['watch_type'] == 'hotel':
            key = (w['destination'], str(w['checkin']), str(w['checkout']))
            hotel_groups[key].append(w)

    checked = 0
    alerts_sent = 0
    errors = []

    # Check flights — cap at 50 unique searches per run
    search_count = 0
    max_searches = 50

    for (origin, destination, checkin, checkout), group_watches in flight_groups.items():
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

        time.sleep(1)  # Rate limiting between searches

    for (destination, checkin, checkout), group_watches in hotel_groups.items():
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

        time.sleep(1)

    summary = {'checked': checked, 'alerts_sent': alerts_sent, 'errors': errors}
    logger.info(f"Watch check complete: {summary}")
    return summary


def _search_best_flight(origin, destination, checkin, checkout):
    """Search adapters for the cheapest flight on this route."""
    dest_iata = _destination_iata(destination)
    best = None

    for AdapterClass in [DuffelAdapter, TravelpayoutsAdapter]:
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
    """Search adapters for the cheapest hotel in this destination."""
    best = None

    for AdapterClass in [LiteAPIAdapter, TravelpayoutsAdapter]:
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
