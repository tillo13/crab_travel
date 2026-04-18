"""
Plan routes — CRUD, settings, demo, preferences, availability
Split from crab_travel/app.py for kumori 1000-line compliance.
"""
import json
import logging
import os
import threading
from datetime import timedelta
from functools import wraps

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, Response
import psycopg2.extras

from utilities.google_auth_utils import get_secret
from utilities.postgres_utils import (
    get_db_connection,
    init_database, upsert_user, get_user_profile, update_user_profile,
    get_user_tokens, update_user_tokens, set_user_calendar_synced,
    create_plan, get_plans_for_user, get_plan_by_id, get_plan_by_invite_token,
    add_plan_member, get_plan_members, get_member_for_plan,
    get_plan_preferences, upsert_plan_preferences, get_all_plan_preferences,
    save_member_availability, get_plan_availability, get_availability_overlap,
    create_destination_suggestion, update_destination_suggestion, update_destination_data,
    get_destination_suggestions, get_destination_suggestion_by_id,
    upsert_vote, delete_vote, get_vote_tallies, get_user_votes, clear_rank_from_others, lock_plan,
    save_recommendations, get_recommendations, update_recommendation_status,
    delete_recommendations_for_plan,
    save_member_blackouts, get_member_blackouts,
    save_member_tentative_dates, get_member_tentative_dates,
    update_member_details,
    delete_plan, delete_destination_suggestion,
    create_message, get_plan_messages, delete_message,
    update_plan_stage, get_plan_blackouts, get_plan_tentative_dates,
    get_search_results, clear_search_results, get_deals_cache_grouped,
    log_invite_view, get_invite_view_stats, get_all_member_votes,
    create_member_watch, get_watches_for_plan, get_watches_for_member, get_active_watches,
    update_watch_price, update_watch_status, get_watch_history,
    get_trip_summary, get_itinerary_items, insert_itinerary_item, delete_itinerary_item,
    get_expenses, insert_expense, get_trip_cost_summary, log_llm_call,
    insert_bot_run, update_bot_run, insert_bot_event, get_bot_runs, get_bot_events,
    get_bot_run_status,
    save_price_history, upsert_deals_cache, get_price_average,
    save_search_result,
    get_member_by_token,
)
from utilities.invite_utils import generate_token
from utilities.trip_ai import generate_recommendations, generate_destination_card, suggest_destinations
from utilities.calendar_utils import get_calendar_events, compute_free_windows, refresh_access_token
from utilities.search_engine import trigger_search, is_searching
from utilities.deals_engine import get_hot_deals, get_hot_deals_grouped, refresh_deals_cache

from route_helpers import login_required, api_auth_required, AUTH_ENABLED

logger = logging.getLogger(__name__)

bp = Blueprint('plan_routes', __name__)


# ── Demo viewer / trip constants (shared with tasks_routes) ──
DEMO_VIEWER_GOOGLE_ID = 'demo_viewer_judy_tunaboat'
DEMO_VIEWER_NAME = 'Judy Tunaboat'

DEMO_TRIPS = {
    'booked':   {'token': 'qL6zhRAI', 'label': 'Booked Trip'},
    'voting':   {'token': None, 'label': 'Voting Stage'},
    'planning': {'token': None, 'label': 'Planning Stage'},
}
_DEMO_STAGE_STATUS = {'voting': 'voting', 'planning': 'locked', 'booked': 'booked'}
DEMO_DEFAULT_STAGE = 'booked'


def _research_destination(plan_id, suggestion_id, destination_name, plan):
    """Delayed import to avoid circular dependency: destinations_routes imports from plan_routes via bp registration."""
    from destinations_routes import _research_destination as _impl
    return _impl(plan_id, suggestion_id, destination_name, plan)


@bp.route('/plan/new')
@login_required
def plan_new():
    user = session['user']
    logger.info(f"📍 New plan: {user['email']}")
    return render_template('plan_new.html', active_page='dashboard', user=user)


@bp.route('/api/plan/create', methods=['POST'])
@api_auth_required
def api_create_plan():
    user = session['user']
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'error': 'Title is required'}), 400
    invite_token = generate_token(6)
    plan = create_plan(user['id'], data, invite_token)
    if not plan:
        return jsonify({'error': 'Failed to create plan'}), 500
    # Add organizer as first member
    member_token = generate_token()
    add_plan_member(plan['plan_id'], user['name'], member_token, email=user['email'], user_id=user['id'], role='organizer')

    # Create candidate destinations and kick off research
    destinations = data.get('destinations', [])
    for dest_name in destinations:
        dest_name = dest_name.strip()
        if not dest_name:
            continue
        suggestion = create_destination_suggestion(plan['plan_id'], user['id'], dest_name)
        if suggestion:
            # Auto-vote yes for the organizer on their own destinations
            upsert_vote(plan['plan_id'], user['id'], 'destination', suggestion['suggestion_id'], 1)
            _research_destination(plan['plan_id'], suggestion['suggestion_id'], dest_name, plan)

    logger.info(f"💬 Plan created: {plan['title']} by {user['email']} with {len(destinations)} destinations")
    return jsonify({'success': True, 'data': {'plan_id': str(plan['plan_id']), 'invite_token': plan['invite_token']}})


@bp.route('/api/plan/<plan_id>/delete', methods=['POST'])
@api_auth_required
def api_delete_plan(plan_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Plan not found'}), 404
    if plan['organizer_id'] != user['id']:
        return jsonify({'error': 'Only the organizer can delete a plan'}), 403
    success = delete_plan(plan_id, user['id'])
    if success:
        logger.info(f"🗑️ Plan deleted: {plan['title']} by {user['email']}")
        return jsonify({'success': True})
    return jsonify({'error': 'Delete failed'}), 500


@bp.route('/join/<invite_token>')
@bp.route('/in/<invite_token>')
def join_plan(invite_token):
    return redirect(f'/to/{invite_token}', code=301)


def _auto_login_demo_viewer(plan):
    """For bot trips, auto-login anonymous visitors as Judy Tunaboat so they get
    the full interactive experience (comment, change dates, vote, etc.)."""
    is_bot_trip = plan.get('title', '').startswith('[BOT]')
    if not is_bot_trip:
        return
    if session.get('user'):
        return  # Already logged in as someone
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT pk_id, email, full_name, picture_url FROM crab.users WHERE google_id = %s",
                    (DEMO_VIEWER_GOOGLE_ID,))
        judy = cur.fetchone()
        cur.close()
        conn.close()
        if judy:
            session.permanent = True
            session['user'] = {
                'id': judy['pk_id'],
                'email': judy['email'],
                'name': judy['full_name'],
                'picture': judy['picture_url'],
            }
            session['_demo_viewer'] = True
            session.modified = True
            logger.info(f"Auto-login demo viewer: {judy['full_name']} (user_id={judy['pk_id']})")
    except Exception as e:
        logger.warning(f"Demo viewer auto-login failed: {e}")


def _resolve_demo_token(stage):
    """Return a bot trip invite_token for the given demo stage.
    Falls back to the booked demo if no match is found."""
    pinned = DEMO_TRIPS.get(stage, {}).get('token')
    if pinned:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM crab.plans WHERE invite_token = %s", (pinned,))
            ok = cur.fetchone() is not None
            cur.close(); conn.close()
            if ok:
                return pinned
        except Exception as e:
            logger.warning(f"Demo token check failed: {e}")
    # Dynamic lookup by status — pick a stable representative bot trip
    db_status = _DEMO_STAGE_STATUS.get(stage, 'booked')
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT invite_token FROM crab.plans "
            "WHERE title LIKE '[BOT]%%' AND status = %s "
            "ORDER BY plan_id ASC LIMIT 1",
            (db_status,),
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            return row[0]
    except Exception as e:
        logger.warning(f"Demo token dynamic lookup failed: {e}")
    return DEMO_TRIPS[DEMO_DEFAULT_STAGE]['token']


@bp.route('/demo')
@bp.route('/demo/<stage>')
def demo_mode(stage=None):
    """Switch to Judy Tunaboat and redirect to a demo trip.
    /demo → booked trip, /demo/voting → voting stage, /demo/planning → planning stage.
    Stashes the real user so they auto-restore when navigating away."""
    stage = stage or request.args.get('stage', DEMO_DEFAULT_STAGE)
    if stage not in DEMO_TRIPS:
        stage = DEMO_DEFAULT_STAGE
    token = _resolve_demo_token(stage)

    try:
        real_user = session.get('user')
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT pk_id, email, full_name, picture_url FROM crab.users WHERE google_id = %s",
                    (DEMO_VIEWER_GOOGLE_ID,))
        judy = cur.fetchone()
        cur.close()
        conn.close()
        if judy:
            session['_demo_stashed_user'] = real_user
            session['user'] = {
                'id': judy['pk_id'],
                'email': judy['email'],
                'name': judy['full_name'],
                'picture': judy['picture_url'],
            }
            session['_demo_viewer'] = True
            session['_demo_stage'] = stage
            session.permanent = True
            session.modified = True
    except Exception as e:
        logger.warning(f"Demo mode switch failed: {e}")
    return redirect(f'/to/{token}')


@bp.route('/to/<invite_token>')
def invite_page(invite_token):
    plan = get_plan_by_invite_token(invite_token)
    if not plan:
        return render_template('index.html', active_page='home', error='Plan not found'), 404

    # Auto-login anonymous visitors as Judy Tunaboat on bot trips
    _auto_login_demo_viewer(plan)

    user = session.get('user')

    # Track invite page view
    log_invite_view(
        plan['plan_id'],
        user_id=user['id'] if user else None,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent'),
        is_authenticated=user is not None,
    )

    destinations = get_destination_suggestions(plan['plan_id'])
    members = get_plan_members(plan['plan_id'])
    vote_tallies = get_vote_tallies(plan['plan_id'], 'destination')

    is_member = False
    member = None
    my_votes = {}
    blackouts = []
    tentative_dates = []
    member_airport = None
    member_flexible = False

    profile_completed = False
    if user:
        member = get_member_for_plan(plan['plan_id'], user['id'])
        is_member = member is not None
        my_votes = get_user_votes(plan['plan_id'], user['id'])
        profile = get_user_profile(user['id'])
        profile_completed = bool(profile and profile.get('profile_completed'))
        if member:
            blackouts = get_member_blackouts(plan['plan_id'], user['id'])
            tentative_dates = get_member_tentative_dates(plan['plan_id'], user['id'])
            member_airport = member.get('home_airport') or user.get('home_airport')
            member_flexible = member.get('is_flexible', False)
        else:
            # Pre-fill from profile even if not yet a member
            member_airport = user.get('home_airport')
    else:
        session['pending_join'] = invite_token

    # Serialize destinations for client-side board rendering
    def _default_ser(o):
        if hasattr(o, 'isoformat'):
            return o.isoformat()
        return str(o)
    destinations_json = json.dumps(destinations, default=_default_ser)

    # Helper to strip [BOT] prefix from names
    def _clean(name):
        return (name or '').replace('[BOT] ', '')

    # Calendar data — all members' blackouts + tentative dates
    is_organizer = user is not None and user['id'] == plan['organizer_id']
    is_bot_trip = plan.get('title', '').startswith('[BOT]')

    # Always load calendar data for all members (bot trips + logged-in users)
    if user is not None or is_bot_trip:
        all_blackouts = get_plan_blackouts(plan['plan_id'])
        all_tentative = get_plan_tentative_dates(plan['plan_id'])
    else:
        all_blackouts = []
        all_tentative = []
    calendar_json = json.dumps({
        'blackouts': [{'name': _clean(b['full_name']), 'start': b['blackout_start'].isoformat(), 'end': b['blackout_end'].isoformat()} for b in all_blackouts],
        'tentative': [{'name': _clean(t['full_name']), 'start': t['date_start'].isoformat(), 'end': t['date_end'].isoformat(), 'preference': t.get('preference', 'works')} for t in all_tentative],
        'members': [{'name': _clean(m['display_name']), 'is_flexible': m.get('is_flexible', False)} for m in members],
    }, default=_default_ser)

    # Per-member profile data for clickable member cards
    all_prefs = get_all_plan_preferences(plan['plan_id'])
    members_detail_json = json.dumps([{
        'member_id': p['member_id'],
        'name': _clean(p['display_name']),
        'role': p.get('role', 'member'),
        'airport': p.get('home_airport', ''),
        'budget_min': p.get('budget_min'),
        'budget_max': p.get('budget_max'),
        'accommodation': p.get('accommodation_style', ''),
        'dietary': p.get('dietary_needs', ''),
        'interests': p.get('interests', []),
        'mobility': p.get('mobility_notes', ''),
        'room_pref': p.get('room_preference', ''),
        'completed': p.get('completed', False),
    } for p in all_prefs], default=_default_ser)

    # Fetch watch data for locked plans
    watches_json = '[]'
    if plan.get('status') == 'locked' or plan.get('locked_destination'):
        from utilities.postgres_utils import get_watches_for_plan, get_watch_history
        watches = get_watches_for_plan(plan['plan_id'])
        watches_data = []
        for w in watches:
            history = get_watch_history(w['pk_id'], limit=20)
            watch_data = w.get('data') or {}
            rec = w.get('recommendation') or {}
            watches_data.append({
                'pk_id': w['pk_id'], 'member_id': w['member_id'],
                'member_name': w['member_name'], 'watch_type': w['watch_type'],
                'origin': w.get('origin'), 'destination': w['destination'],
                'checkin': w['checkin'].isoformat() if w.get('checkin') else None,
                'checkout': w['checkout'].isoformat() if w.get('checkout') else None,
                'status': w['status'],
                'best_price': float(w['best_price_usd']) if w.get('best_price_usd') else None,
                'last_price': float(w['last_price_usd']) if w.get('last_price_usd') else None,
                'deep_link': w.get('deep_link'),
                'last_checked': w['last_checked_at'].isoformat() if w.get('last_checked_at') else None,
                'history': [{'price': float(h['price_usd']), 'at': h['observed_at'].isoformat()} for h in history],
                'booked_price': watch_data.get('booked_price'),
                'confirmation': watch_data.get('confirmation'),
                'departure_time': watch_data.get('departure_time'),
                'arrival_time': watch_data.get('arrival_time'),
                'return_departure_time': watch_data.get('return_departure_time'),
                'return_arrival_time': watch_data.get('return_arrival_time'),
                'recommendation': rec if rec else None,
            })
        watches_json = json.dumps(watches_data, default=_default_ser)

    # Show "Viewing as" banner for demo viewer on bot trips
    viewing_as = None
    if user and is_member and (session.get('_demo_viewer') or is_bot_trip):
        viewing_as = user.get('name', 'Demo User')

    # Per-member token from email link (?m=<member_token>) — used by the
    # CrabAI Deal Hunter accordion to personalize without login.
    board_member_token = (request.args.get('m') or '').strip() or None
    if board_member_token is None and member and member.get('member_token'):
        board_member_token = member['member_token']

    # Booked trip summary stats (for header display)
    booked_summary = None
    if plan.get('status') in ('locked', 'booked') or plan.get('locked_destination'):
        try:
            from utilities.postgres_utils import get_trip_summary
            booked_summary = get_trip_summary(plan['plan_id'])
        except Exception:
            pass

    return render_template('invite.html',
        plan=plan, destinations=destinations, members=members,
        vote_tallies=vote_tallies, my_votes=my_votes,
        user=user, is_member=is_member, member=member,
        blackouts=blackouts, tentative_dates=tentative_dates,
        member_airport=member_airport,
        member_flexible=member_flexible,
        needs_login=(user is None and not is_bot_trip),
        is_organizer=is_organizer,
        is_bot_trip=is_bot_trip,
        profile_completed=profile_completed,
        destinations_json=destinations_json,
        calendar_json=calendar_json if not (user is None and not is_bot_trip) else '{}',
        watches_json=watches_json,
        members_detail_json=members_detail_json,
        viewing_as=viewing_as,
        booked_summary=booked_summary,
        demo_stage=session.get('_demo_stage'),
        board_member_token=board_member_token,
    )


@bp.route('/api/plan/<plan_id>/join-full', methods=['POST'])
@api_auth_required
def api_join_full(plan_id):
    user = session['user']
    data = request.get_json() or {}

    # Join or get existing membership
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        member_token = generate_token()
        member = add_plan_member(
            plan_id, user['name'], member_token,
            email=user['email'], user_id=user['id'],
        )
        if not member:
            return jsonify({'error': 'Failed to join'}), 500
        logger.info(f"👋 Joined plan: {user['name']} → plan {plan_id}")

    # Update airport + flexible — resolve freeform location to IATA code
    from utilities.airport_utils import resolve_airport
    location_input = data.get('home_airport', '').strip()
    is_flexible = data.get('is_flexible', False)
    resolved = resolve_airport(location_input) if location_input else None
    airport_code = resolved['code'] if resolved else (location_input.upper() if len(location_input) == 3 else None)
    update_member_details(member['pk_id'], home_airport=airport_code or location_input or None, is_flexible=is_flexible)

    # Also save to user profile so it carries across plans
    if location_input:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE crab.users SET home_airport = %s, home_location = %s WHERE pk_id = %s AND (home_airport IS NULL OR home_airport = '')",
                        (airport_code, location_input, user['id']))
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass

    # Save blackouts
    blackouts = data.get('blackouts', [])
    if blackouts or not is_flexible:
        save_member_blackouts(plan_id, user['id'], blackouts)

    # Save tentative dates
    tentative = data.get('tentative_dates', [])
    if tentative or not is_flexible:
        save_member_tentative_dates(plan_id, user['id'], tentative)

    # Save votes
    votes = data.get('votes', {})
    for target_id, vote_val in votes.items():
        if vote_val in (1, -1):
            upsert_vote(plan_id, user['id'], 'destination', target_id, vote_val)

    return jsonify({'success': True, 'data': {'plan_id': str(plan_id), 'member_id': member['pk_id']}})


@bp.route('/api/plan/<plan_id>/calendar')
@api_auth_required
def api_plan_calendar(plan_id):
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Not found'}), 404
    member = get_member_for_plan(plan_id, session['user']['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403
    members = get_plan_members(plan_id)
    all_blackouts = get_plan_blackouts(plan_id)
    all_tentative = get_plan_tentative_dates(plan_id)
    return jsonify({
        'blackouts': [{'name': b['full_name'], 'start': b['blackout_start'].isoformat(), 'end': b['blackout_end'].isoformat()} for b in all_blackouts],
        'tentative': [{'name': t['full_name'], 'start': t['date_start'].isoformat(), 'end': t['date_end'].isoformat(), 'preference': t.get('preference', 'works')} for t in all_tentative],
        'members': [{'name': m['display_name'], 'is_flexible': m.get('is_flexible', False)} for m in members],
    })


@bp.route('/plan/<plan_id>')
@login_required
def view_plan(plan_id):
    plan = get_plan_by_id(plan_id)
    if not plan:
        return redirect('/dashboard')

    user = session['user']
    is_organizer = user['id'] == plan['organizer_id']

    # Access check: must be organizer or authed member
    has_access = is_organizer
    if not has_access:
        member = get_member_for_plan(plan_id, user['id'])
        has_access = member is not None
    if not has_access:
        return redirect(f"/join/{plan['invite_token']}")

    members = get_plan_members(plan_id)
    all_prefs = get_all_plan_preferences(plan_id)
    recs = get_recommendations(plan_id)
    destinations = get_destination_suggestions(plan_id)
    view_stats = get_invite_view_stats(plan_id) if is_organizer else None
    member_votes = {}
    vote_tallies = None
    if is_organizer:
        raw_votes = get_all_member_votes(plan_id)
        # Build a set of user_ids who have voted
        voted_user_ids = set()
        for uid, mv in raw_votes.items():
            if mv.get('votes'):
                voted_user_ids.add(uid)
        member_votes = {'voted_user_ids': voted_user_ids, 'raw': raw_votes}
        vote_tallies = get_vote_tallies(plan_id, 'destination')
    return render_template('plan.html', plan=plan, members=members, all_prefs=all_prefs,
                           recs=recs, destinations=destinations, is_organizer=is_organizer, user=user,
                           view_stats=view_stats, member_votes=member_votes, vote_tallies=vote_tallies)


def _resolve_member(plan_id):
    user = session.get('user')
    if user:
        return get_member_for_plan(plan_id, user['id'])
    return None


@bp.route('/plan/<plan_id>/preferences')
def plan_preferences(plan_id):
    plan = get_plan_by_id(plan_id)
    if not plan:
        return redirect('/dashboard')
    member = _resolve_member(plan_id)
    if not member:
        return redirect(f"/join/{plan['invite_token']}")
    prefs = get_plan_preferences(member['pk_id'])
    user = session.get('user')
    return render_template('preferences.html', plan=plan, member=member, prefs=prefs, user=user)


@bp.route('/api/plan/<plan_id>/preferences', methods=['POST'])
def api_update_preferences(plan_id):
    member = _resolve_member(plan_id)
    if not member:
        return jsonify({'error': 'Access denied'}), 403
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    success = upsert_plan_preferences(member['pk_id'], data)
    if success:
        logger.info(f"💬 Preferences saved: {member['display_name']} for plan {plan_id}")
        return jsonify({'success': True})
    return jsonify({'error': 'Save failed'}), 500


@bp.route('/api/calendar/sync', methods=['POST'])
@api_auth_required
def api_sync_calendar():
    user = session['user']
    tokens = get_user_tokens(user['id'])
    if not tokens or not tokens.get('google_access_token'):
        return jsonify({'error': 'No calendar access. Please re-login with Google.'}), 400

    access_token = tokens['google_access_token']

    # Try fetching events — if 401, refresh token
    from datetime import datetime
    time_min = datetime.utcnow()
    time_max = time_min + timedelta(days=180)  # next 6 months

    events = get_calendar_events(access_token, time_min, time_max)
    if events is None and tokens.get('google_refresh_token'):
        # Token expired — refresh it
        new_token = refresh_access_token(
            tokens['google_refresh_token'],
            get_secret('CRAB_GOOGLE_CLIENT_ID'),
            get_secret('CRAB_GOOGLE_CLIENT_SECRET'),
        )
        if new_token:
            update_user_tokens(user['id'], new_token)
            events = get_calendar_events(new_token, time_min, time_max)

    if events is None:
        return jsonify({'error': 'Calendar access expired. Please re-login.'}), 401

    windows = compute_free_windows(events, time_min, time_max)
    set_user_calendar_synced(user['id'])

    logger.info(f"📅 Calendar synced for {user['email']}: {len(events)} events, {len(windows)} free windows")
    return jsonify({
        'success': True,
        'data': {
            'event_count': len(events),
            'free_windows': windows,
        }
    })


@bp.route('/api/plan/<plan_id>/sync-calendar', methods=['POST'])
@api_auth_required
def api_sync_plan_calendar(plan_id):
    """Sync current user's calendar and save availability for this plan."""
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member of this plan'}), 403

    tokens = get_user_tokens(user['id'])
    if not tokens or not tokens.get('google_access_token'):
        return jsonify({'error': 'No calendar access. Please re-login.'}), 400

    access_token = tokens['google_access_token']
    from datetime import datetime
    time_min = datetime.utcnow()
    time_max = time_min + timedelta(days=180)

    events = get_calendar_events(access_token, time_min, time_max)
    if events is None and tokens.get('google_refresh_token'):
        new_token = refresh_access_token(
            tokens['google_refresh_token'],
            get_secret('CRAB_GOOGLE_CLIENT_ID'),
            get_secret('CRAB_GOOGLE_CLIENT_SECRET'),
        )
        if new_token:
            update_user_tokens(user['id'], new_token)
            events = get_calendar_events(new_token, time_min, time_max)

    if events is None:
        return jsonify({'error': 'Calendar access expired. Please re-login.'}), 401

    windows = compute_free_windows(events, time_min, time_max)
    save_member_availability(plan_id, user['id'], windows, source='calendar')
    set_user_calendar_synced(user['id'])

    logger.info(f"📅 Plan calendar synced: {user['email']} for plan {plan_id}")
    return jsonify({'success': True, 'data': {'windows': len(windows)}})


@bp.route('/api/plan/<plan_id>/availability')
@api_auth_required
def api_plan_availability(plan_id):
    """Get group availability overlap for this plan."""
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    overlap = get_availability_overlap(plan_id)
    return jsonify({'success': True, 'data': {'windows': overlap}})


@bp.route('/api/plan/<plan_id>/leave', methods=['POST'])
@api_auth_required
def api_leave_plan(plan_id):
    """Leave a trip — removes the current user from the plan."""
    from utilities.postgres_utils import remove_plan_member
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401
    removed = remove_plan_member(plan_id, user['pk_id'])
    if removed:
        return jsonify({'success': True})
    return jsonify({'error': 'Could not leave trip (you may be the organizer)'}), 400
