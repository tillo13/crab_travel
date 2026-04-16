"""
Auth routes — login/logout/auth + profile
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

bp = Blueprint('auth_routes', __name__)


def _google_auth():
    """Delayed import to avoid circular dependency with app.py."""
    from app import google_auth
    return google_auth


@bp.route('/login')
def login():
    if not AUTH_ENABLED:
        return redirect('/')
    if 'user' in session:
        return redirect('/dashboard')
    return render_template('login.html', active_page='login')


@bp.route('/login/google')
def login_google():
    if not AUTH_ENABLED:
        return redirect('/')
    return _google_auth().authorize_redirect(url_for('auth_routes.auth_callback', _external=True))


@bp.route('/auth/callback')
def auth_callback():
    if not AUTH_ENABLED:
        return redirect('/')
    try:
        logger.info(f"🔑 Auth callback hit, args: {dict(request.args)}")
        token = _google_auth().authorize_access_token()
        logger.info(f"🔑 Token received: {bool(token)}")
        if token:
            user_info = token.get('userinfo')
            logger.info(f"🔑 User info: {user_info}")
            if user_info:
                access_token = token.get('access_token')
                refresh_token = token.get('refresh_token')
                db_user = upsert_user(user_info, access_token=access_token, refresh_token=refresh_token)
                logger.info(f"🔑 DB user: {db_user}")
                if db_user:
                    session.permanent = True
                    session['user'] = {
                        'id': db_user['pk_id'],
                        'email': db_user['email'],
                        'name': db_user['full_name'],
                        'picture': db_user['picture_url'],
                        'home_airport': db_user.get('home_airport'),
                    }
                    session['user_is_admin'] = db_user.get('is_admin', False)
                    logger.info(f"🔑 Session set for: {db_user['email']}")
                    # Redirect to pending join if there was one
                    pending_join = session.pop('pending_join', None)
                    if pending_join:
                        return redirect(f"/to/{pending_join}")
                    return redirect('/dashboard')
                else:
                    logger.error("🔑 DB upsert returned None")
            else:
                logger.error("🔑 No userinfo in token")
        else:
            logger.error("🔑 No token received")
    except Exception as e:
        logger.error(f"❌ Auth callback error: {e}")
    return redirect('/login')


@bp.route('/logout')
def logout():
    email = session.get('user', {}).get('email', 'unknown')
    session.pop('user', None)
    logger.info(f"🔑 Logout: {email}")
    return redirect('/')


@bp.route('/dashboard')
@login_required
def dashboard():
    user = session['user']
    plans = get_plans_for_user(user['id'])
    logger.info(f"📍 Dashboard: {user['email']}")
    return render_template('dashboard.html', active_page='dashboard', user=user, plans=plans)


@bp.route('/profile')
@login_required
def profile():
    user = session['user']
    profile_data = get_user_profile(user['id'])
    # Get phone/notification prefs from users table
    user_phone = ''
    notify_prefs = {'notify_chat': 'off', 'notify_updates': 'off', 'notify_channel': 'email'}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT phone_number, notify_chat, notify_updates, notify_channel FROM crab.users WHERE pk_id = %s", (user['id'],))
        row = cur.fetchone()
        if row:
            user_phone = row[0] or ''
            notify_prefs = {
                'notify_chat': row[1] or 'off',
                'notify_updates': row[2] or 'off',
                'notify_channel': row[3] or 'email',
            }
        cur.close()
        conn.close()
    except Exception:
        pass
    logger.info(f"📍 Profile: {user['email']}")
    return render_template('profile.html', active_page='profile', user=user, profile=profile_data,
                           user_phone=user_phone, notify_prefs=notify_prefs)


@bp.route('/profile/demo')
def profile_demo():
    """Public read-only preview of the Profile page.

    Published for Twilio A2P 10DLC reviewers so the SMS consent Call-to-Action
    can be verified without Google sign-in. The real form is /profile.
    """
    demo_user = {
        'id': 0,
        'name': 'Sample Reviewer',
        'email': 'reviewer@crab.travel',
        'picture': None,
    }
    demo_profile = {
        'home_location': 'Scottsdale, AZ',
        'home_airport': 'PHX',
        'interests': ['hiking', 'food tours', 'live music'],
        'travel_style': 'adventure,foodie',
        'accommodation_preference': 'hotel',
        'budget_comfort': 'moderate',
        'dietary_needs': '',
        'mobility_notes': '',
        'bio': '',
    }
    demo_prefs = {'notify_chat': 'realtime', 'notify_updates': 'daily', 'notify_channel': 'email'}
    return render_template(
        'profile.html',
        active_page='profile',
        display_user=demo_user,
        profile=demo_profile,
        user_phone='+1 (425) 555-0123',
        notify_prefs=demo_prefs,
        demo_mode=True,
    )


@bp.route('/notifications/off/<member_token>')
def unsubscribe_notifications(member_token):
    """One-click email unsubscribe — sets notify_chat=off for this user."""
    from utilities.postgres_utils import get_member_by_token
    member = get_member_by_token(member_token)
    if not member or not member.get('user_id'):
        return render_template('404.html'), 404
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE crab.users SET notify_chat = 'off' WHERE pk_id = %s", (member['user_id'],))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Unsubscribe failed: {e}")
        return render_template('404.html'), 500
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Unsubscribed — crab.travel</title>
    <style>body{font-family:system-ui;background:#1a0a0a;color:#f0eae0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .box{text-align:center;max-width:400px;padding:2rem}h1{font-size:1.5rem;margin-bottom:1rem}a{color:#e8593a}</style></head>
    <body><div class="box"><h1>Unsubscribed</h1><p>You won't receive chat notifications anymore.</p>
    <p style="margin-top:1rem"><a href="/profile">Re-enable in profile settings</a></p></div></body></html>"""


@bp.route('/api/profile', methods=['POST'])
@api_auth_required
def api_update_profile():
    user = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    # Auto-resolve airport from freeform location
    from utilities.airport_utils import resolve_airport
    location = data.get('home_airport') or data.get('home_location') or ''
    if location:
        result = resolve_airport(location)
        if result:
            data['home_airport'] = result['code']
            data['home_location'] = location
        else:
            data['home_location'] = location
            data['home_airport'] = None
    success = update_user_profile(user['id'], data)
    if success:
        logger.info(f"💬 Profile updated: {user['email']}")
        airport_info = resolve_airport(location) if location else None
        return jsonify({'success': True, 'resolved_airport': airport_info})
    return jsonify({'error': 'Update failed'}), 500


@bp.route('/api/airport/resolve')
def api_resolve_airport():
    """Live-resolve freeform text to nearest airport."""
    from utilities.airport_utils import resolve_airport
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'result': None})
    result = resolve_airport(q)
    return jsonify({'result': result})
