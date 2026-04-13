"""
Watches, deals, messages, YouTube/photo search routes
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

bp = Blueprint('watches_routes', __name__)


@bp.route('/api/plan/<plan_id>/watches')
def api_get_watches(plan_id):
    """Get all member watches for a plan, grouped by member."""
    from utilities.postgres_utils import get_watches_for_plan, get_watch_history
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Plan not found'}), 404
    # Allow unauthenticated access for bot trips
    is_bot_trip = plan.get('title', '').startswith('[BOT]')
    if not is_bot_trip:
        if AUTH_ENABLED and 'user' not in session:
            return jsonify({'error': 'Not authenticated'}), 401

    watches = get_watches_for_plan(plan_id)
    # Group by member and attach recent history for sparklines
    members = {}
    for w in watches:
        mid = w['member_id']
        if mid not in members:
            members[mid] = {
                'member_id': mid,
                'member_name': w['member_name'],
                'watches': [],
            }
        history = get_watch_history(w['pk_id'], limit=20)
        w['history'] = [{'price': float(h['price_usd']), 'at': h['observed_at'].isoformat()} for h in history]
        # Convert decimals for JSON
        for field in ('best_price_usd', 'last_price_usd'):
            if w.get(field) is not None:
                w[field] = float(w[field])
        members[mid]['watches'].append(w)

    return jsonify({'success': True, 'data': {'members': list(members.values())}})


@bp.route('/api/plan/<plan_id>/watches/<int:watch_id>/status', methods=['POST'])
@api_auth_required
def api_update_watch_status(plan_id, watch_id):
    """Update watch status (mark as booked, paused, or active)."""
    from utilities.postgres_utils import update_watch_status
    data = request.get_json()
    new_status = data.get('status') if data else None
    if new_status not in ('active', 'paused', 'booked'):
        return jsonify({'error': 'Invalid status'}), 400
    # Capture optional booking details
    booked_price = data.get('booked_price') if data else None
    confirmation = data.get('confirmation') if data else None
    success = update_watch_status(watch_id, new_status,
                                  booked_price=booked_price,
                                  confirmation=confirmation)
    # Auto-transition plan to 'booked' when ALL watches are booked
    plan_auto_booked = False
    if success and new_status == 'booked':
        from utilities.postgres_utils import get_watches_for_plan
        all_watches = get_watches_for_plan(plan_id)
        if all_watches and all(w['status'] == 'booked' for w in all_watches):
            update_plan_stage(plan_id, 'booked')
            plan_auto_booked = True
            logger.info(f"All watches booked — plan {plan_id} auto-transitioned to 'booked'")
    return jsonify({'success': success, 'plan_booked': plan_auto_booked})


@bp.route('/api/plan/<plan_id>/watches/<int:watch_id>/history')
def api_get_watch_history(plan_id, watch_id):
    """Price history for a single watch (sparkline data)."""
    from utilities.postgres_utils import get_watch_history
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Plan not found'}), 404
    is_bot_trip = plan.get('title', '').startswith('[BOT]')
    if not is_bot_trip:
        if AUTH_ENABLED and 'user' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
    history = get_watch_history(watch_id, limit=int(request.args.get('limit', 50)))
    return jsonify({
        'success': True,
        'data': [{'price': float(h['price_usd']), 'source': h['source'],
                   'at': h['observed_at'].isoformat()} for h in history]
    })


@bp.route('/api/deals')
@login_required
def api_deals():
    """Global hot deals feed — reads from nightly cache, grouped by source tab."""
    try:
        group_size = int(request.args.get('group_size', 15))
        group_size = max(2, min(group_size, 100))
        origin = request.args.get('origin', '').strip() or None

        from utilities.deals_engine import resolve_iata
        iata = resolve_iata(origin) if origin else None

        cache = get_deals_cache_grouped(origin=iata)
        tabs = cache.get('tabs', []) if isinstance(cache, dict) else []
        last_updated = cache.get('last_updated') if isinstance(cache, dict) else None

        # Cache empty — fall back to live fetch so the first visit still works
        if not tabs:
            logger.info("💡 Deals cache empty — falling back to live fetch")
            tabs = get_hot_deals_grouped(group_size=group_size, origin=origin)

        # Annotate group totals (cache stores raw prices)
        for tab in tabs:
            for d in tab['deals']:
                d['group_size'] = group_size
                if d.get('deal_type') != 'hotel':
                    d['total_for_group'] = round(d['price_per_person'] * group_size, 2)

        return jsonify({'success': True, 'data': {'tabs': tabs, 'group_size': group_size, 'last_updated': last_updated}})
    except Exception as e:
        logger.error(f"❌ api_deals failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/plan/<plan_id>/deals')
@api_auth_required
def api_plan_deals(plan_id):
    """Hot deals for a plan's Deal Desk — reads from cache, flat list sorted by price."""
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'success': False, 'error': 'Plan not found'}), 404
    try:
        group_size = plan.get('headcount') or int(request.args.get('group_size', 15))
        limit = int(request.args.get('limit', 20))

        cache = get_deals_cache_grouped()
        tabs = cache.get('tabs', []) if isinstance(cache, dict) else []

        # Flatten all tabs into one sorted list
        all_deals = []
        for tab in tabs:
            all_deals.extend(tab.get('deals', []))
        all_deals.sort(key=lambda d: d['price_per_person'])

        # Annotate group totals
        for d in all_deals[:limit]:
            d['group_size'] = group_size
            if d.get('deal_type') != 'hotel':
                d['total_for_group'] = round(d['price_per_person'] * group_size, 2)

        # Fall back to live fetch if cache empty
        if not all_deals:
            all_deals = get_hot_deals(group_size=group_size, limit=limit)

        return jsonify({'success': True, 'data': {'deals': all_deals[:limit], 'group_size': group_size}})
    except Exception as e:
        logger.error(f"❌ api_plan_deals failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/youtube-search')
def api_youtube_search():
    q = request.args.get('q', '')
    max_results = min(int(request.args.get('max_results', 6)), 12)
    if not q:
        return jsonify({'success': False, 'videos': []})
    try:
        yt_key = get_secret('CRAB_YOUTUBE_API_KEY')
    except Exception:
        yt_key = None
    if not yt_key:
        return jsonify({'success': False, 'videos': [], 'error': 'no_key'})
    try:
        import requests as http_requests
        resp = http_requests.get('https://www.googleapis.com/youtube/v3/search', params={
            'part': 'snippet',
            'q': q,
            'type': 'video',
            'maxResults': max_results,
            'key': yt_key,
            'videoEmbeddable': 'true',
        }, timeout=8)
        data = resp.json()
        videos = []
        for item in data.get('items', []):
            videos.append({
                'id': item['id']['videoId'],
                'title': item['snippet']['title'],
                'thumbnail': item['snippet']['thumbnails'].get('medium', {}).get('url', ''),
                'channel': item['snippet']['channelTitle'],
            })
        return jsonify({'success': True, 'videos': videos})
    except Exception as e:
        logger.error(f"YouTube search failed: {e}")
        return jsonify({'success': False, 'videos': [], 'error': str(e)})


@bp.route('/api/photo-search')
def api_photo_search():
    q = request.args.get('q', '')
    per_page = min(int(request.args.get('per_page', 8)), 15)
    if not q:
        return jsonify({'success': False, 'photos': []})
    try:
        pexels_key = get_secret('CRAB_PEXELS_API_KEY')
    except Exception:
        pexels_key = None
    if not pexels_key:
        return jsonify({'success': False, 'photos': [], 'error': 'no_key'})
    try:
        import requests as http_requests
        resp = http_requests.get('https://api.pexels.com/v1/search', params={
            'query': q,
            'per_page': per_page,
            'orientation': 'landscape',
        }, headers={'Authorization': pexels_key}, timeout=8)
        data = resp.json()
        photos = []
        for p in data.get('photos', []):
            photos.append({
                'id': p['id'],
                'url': p['src']['medium'],
                'full': p['src'].get('large2x') or p['src'].get('original', p['src']['medium']),
                'alt': p.get('alt', ''),
                'photographer': p.get('photographer', ''),
            })
        return jsonify({'success': True, 'photos': photos})
    except Exception as e:
        logger.error(f"Pexels search failed: {e}")
        return jsonify({'success': False, 'photos': [], 'error': str(e)})


@bp.route('/api/plan/<plan_id>/messages')
def api_get_messages(plan_id):
    # Allow unauthenticated access for bot trips (public voyeur mode)
    plan = get_plan_by_id(plan_id)
    is_bot_trip = plan and plan.get('title', '').startswith('[BOT]')
    if not is_bot_trip:
        if AUTH_ENABLED and 'user' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
    messages = get_plan_messages(plan_id)
    # Organize into threads: top-level messages + replies nested under parent
    top_level = []
    replies_map = {}
    for m in messages:
        m['message_id'] = str(m['message_id'])
        m['plan_id'] = str(m['plan_id'])
        m['parent_id'] = str(m['parent_id']) if m['parent_id'] else None
        m['created_at'] = m['created_at'].isoformat() if m['created_at'] else None
        if m['parent_id']:
            replies_map.setdefault(m['parent_id'], []).append(m)
        else:
            top_level.append(m)

    for msg in top_level:
        msg['replies'] = replies_map.get(msg['message_id'], [])
        msg['reply_count'] = len(msg['replies'])

    return jsonify({'success': True, 'data': {'messages': top_level}})


@bp.route('/api/plan/<plan_id>/messages', methods=['POST'])
@api_auth_required
def api_post_message(plan_id):
    user = session['user']
    data = request.get_json()
    content = (data.get('content') or '').strip() if data else ''
    if not content:
        return jsonify({'error': 'Message content required'}), 400
    if len(content) > 2000:
        return jsonify({'error': 'Message too long (max 2000 chars)'}), 400

    parent_id = data.get('parent_id')
    display_name = user.get('name', user.get('email', 'Anonymous'))

    msg = create_message(plan_id, user['id'], display_name, content, parent_id=parent_id)
    if not msg:
        return jsonify({'error': 'Failed to send message'}), 500

    msg['message_id'] = str(msg['message_id'])
    msg['plan_id'] = str(msg['plan_id'])
    msg['parent_id'] = str(msg['parent_id']) if msg['parent_id'] else None
    msg['created_at'] = msg['created_at'].isoformat() if msg['created_at'] else None
    msg['user_picture'] = user.get('picture')

    # Send notifications to members (async, don't block response).
    # Routes via the unified dispatcher: email always, SMS only if the trip's
    # organizer is on subscription_tier='premium'.
    try:
        from utilities.notification_utils import notify_chat_message
        import threading
        threading.Thread(
            target=notify_chat_message,
            args=(plan_id, display_name, content, user['id']),
            kwargs={'message_id': msg['message_id']},
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning(f"Notification dispatch failed: {e}")

    return jsonify({'success': True, 'data': {'message': msg}})


@bp.route('/api/plan/<plan_id>/messages/<message_id>', methods=['DELETE'])
@api_auth_required
def api_delete_message(plan_id, message_id):
    user = session['user']
    success = delete_message(message_id, user['id'])
    return jsonify({'success': success})
