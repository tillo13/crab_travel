"""
Destination, voting, recommendation, search routes
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

bp = Blueprint('destinations_routes', __name__)


# ── Background job status (in-memory) ────────────────────────
_rec_status = {}       # plan_id -> {status, count, error, updated_at}
_rec_status_lock = threading.Lock()


def _set_rec_status(plan_id, status, count=None, error=None):
    with _rec_status_lock:
        _rec_status[str(plan_id)] = {
            'status': status,
            'count': count,
            'error': error,
        }


def _get_rec_status(plan_id):
    with _rec_status_lock:
        return _rec_status.get(str(plan_id))


def _research_destination(plan_id, suggestion_id, destination_name, plan):
    def _do_research():
        try:
            all_prefs = get_all_plan_preferences(plan_id)
            airports = [m.get('home_airport') for m in all_prefs if m.get('home_airport')]
            checkin = str(plan.get('start_date') or plan.get('travel_window_start') or '')
            checkout = str(plan.get('end_date') or plan.get('travel_window_end') or '')
            trigger_search(plan_id, destination_name, checkin or None, checkout or None, airports or None)
            research = {'destination': destination_name}
            travel_window = None
            ws = plan.get('travel_window_start')
            we = plan.get('travel_window_end')
            if ws or we:
                travel_window = {'start': str(ws) if ws else 'flexible', 'end': str(we) if we else 'flexible'}
            group_vibes = plan.get('group_vibes')
            card = generate_destination_card(destination_name, research, all_prefs, travel_window=travel_window, group_vibes=group_vibes)
            update_data = {
                'destination_data': {'research': research, 'card': card},
                'avg_flight_cost': research.get('avg_flight_cost'),
                'compatibility_score': card.get('compatibility_score') if card else None,
                'status': 'ready',
            }
            update_destination_suggestion(suggestion_id, update_data)
            logger.info(f"🔍 Destination researched: {destination_name} for plan {plan_id}")
        except Exception as e:
            logger.error(f"❌ Background research failed for {destination_name}: {e}")
            update_destination_suggestion(suggestion_id, {'destination_data': {'error': str(e)}, 'status': 'error'})

    threading.Thread(target=_do_research, daemon=True).start()


@bp.route('/api/plan/<plan_id>/suggest-destination', methods=['POST'])
@api_auth_required
def api_suggest_destination(plan_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Plan not found'}), 404

    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        # Auto-join the plan when suggesting a destination
        member_token = generate_token()
        member = add_plan_member(
            plan_id, user['name'], member_token,
            email=user['email'], user_id=user['id'],
        )
        if not member:
            return jsonify({'error': 'Failed to join plan'}), 500
        logger.info(f"👋 Auto-joined plan via suggestion: {user['name']} → plan {plan_id}")

    data = request.get_json()
    destination_name = data.get('destination') if data else None
    if not destination_name:
        return jsonify({'error': 'Destination name required'}), 400

    is_organizer = user['id'] == plan['organizer_id']

    # Create the suggestion — organizer auto-approves, others go pending
    suggestion = create_destination_suggestion(plan_id, user['id'], destination_name)
    if not suggestion:
        return jsonify({'error': 'Failed to create suggestion'}), 500

    # Auto-vote yes for whoever suggests a destination
    upsert_vote(plan_id, user['id'], 'destination', suggestion['suggestion_id'], 1)

    if is_organizer:
        # Organizer: immediately start researching
        _research_destination(plan_id, suggestion['suggestion_id'], destination_name, plan)
        return jsonify({
            'success': True,
            'data': {
                'suggestion_id': str(suggestion['suggestion_id']),
                'status': 'researching',
            }
        })
    else:
        # Non-organizer: mark as pending for coordinator approval
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE crab.destination_suggestions SET status = 'pending' WHERE suggestion_id = %s",
                        (suggestion['suggestion_id'],))
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass
        return jsonify({
            'success': True,
            'data': {
                'suggestion_id': str(suggestion['suggestion_id']),
                'status': 'pending',
            }
        })


@bp.route('/api/plan/<plan_id>/approve-suggestion', methods=['POST'])
@api_auth_required
def api_approve_suggestion(plan_id):
    """Organizer approves a pending destination suggestion — triggers research."""
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or plan['organizer_id'] != user['id']:
        return jsonify({'error': 'Only the organizer can approve suggestions'}), 403
    data = request.get_json() or {}
    suggestion_id = data.get('suggestion_id')
    action = data.get('action', 'approve')
    if not suggestion_id:
        return jsonify({'error': 'suggestion_id required'}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if action == 'reject':
            cur.execute("DELETE FROM crab.destination_suggestions WHERE suggestion_id = %s AND plan_id = %s",
                        (suggestion_id, plan_id))
        else:
            cur.execute("UPDATE crab.destination_suggestions SET status = 'researching' WHERE suggestion_id = %s AND plan_id = %s",
                        (suggestion_id, plan_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Approve suggestion failed: {e}")
        return jsonify({'error': 'Database error'}), 500
    if action == 'approve':
        # Get the destination name and kick off research
        suggestions = get_destination_suggestions(plan_id)
        dest = next((s for s in suggestions if str(s['suggestion_id']) == str(suggestion_id)), None)
        if dest:
            _research_destination(plan_id, suggestion_id, dest['destination_name'], plan)
    return jsonify({'success': True, 'action': action})


@bp.route('/api/plan/<plan_id>/suggest-anywhere', methods=['POST'])
@api_auth_required
def api_suggest_anywhere(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    all_prefs = get_all_plan_preferences(plan_id)
    suggestions = suggest_destinations(all_prefs)
    logger.info(f"🤖 AI suggested {len(suggestions)} destinations for plan {plan_id}")
    return jsonify({'success': True, 'data': {'suggestions': suggestions}})


@bp.route('/api/plan/<plan_id>/destinations')
@api_auth_required
def api_get_destinations(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    suggestions = get_destination_suggestions(plan_id)
    return jsonify({'success': True, 'data': {'destinations': suggestions}})


@bp.route('/api/plan/<plan_id>/destination/<suggestion_id>', methods=['DELETE'])
@api_auth_required
def api_delete_destination(plan_id, suggestion_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or str(plan['organizer_id']) != str(user['id']):
        return jsonify({'error': 'Only the organizer can delete destinations'}), 403

    success = delete_destination_suggestion(suggestion_id)
    if not success:
        return jsonify({'error': 'Failed to delete destination'}), 500

    logger.info(f"🗑️ Destination {suggestion_id} deleted from plan {plan_id} by {user['email']}")
    return jsonify({'success': True})


@bp.route('/api/plan/<plan_id>/destination/<suggestion_id>/pin', methods=['POST'])
@api_auth_required
def api_add_pin(plan_id, suggestion_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or str(plan['organizer_id']) != str(user['id']):
        return jsonify({'error': 'Only the organizer can add pins'}), 403

    data = request.get_json()
    name = (data.get('name') or '').strip() if data else ''
    if not name:
        return jsonify({'error': 'Name required'}), 400

    suggestion = get_destination_suggestion_by_id(suggestion_id)
    if not suggestion or str(suggestion['plan_id']) != str(plan_id):
        return jsonify({'error': 'Destination not found'}), 404

    category = data.get('category', 'things_to_do')
    if category not in ('stays', 'things_to_do', 'food_and_drink', 'upcoming_events'):
        return jsonify({'error': 'Invalid category'}), 400

    pin = {'name': name, 'description': data.get('description', ''), 'image_search': name}
    url = (data.get('url') or '').strip()
    if url:
        pin['url'] = url
    price_map = {'stays': '$$', 'things_to_do': '$$', 'food_and_drink': '$$', 'upcoming_events': ''}
    pin['price_hint'] = price_map.get(category, '')

    dest_data = suggestion.get('destination_data') or {}
    card = dest_data.get('card', {})
    items = card.get(category, [])
    items.insert(0, pin)  # Add to top
    card[category] = items
    dest_data['card'] = card
    update_destination_data(suggestion_id, dest_data)

    logger.info(f"📌 Custom pin added: {name} to {suggestion['destination_name']}")
    return jsonify({'success': True})


@bp.route('/api/plan/<plan_id>/destination/<suggestion_id>/pin', methods=['DELETE'])
@api_auth_required
def api_delete_pin(plan_id, suggestion_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or str(plan['organizer_id']) != str(user['id']):
        return jsonify({'error': 'Only the organizer can remove pins'}), 403

    data = request.get_json()
    category = data.get('category')  # stays, things_to_do, food_and_drink, upcoming_events
    idx = data.get('index')
    if category is None or idx is None:
        return jsonify({'error': 'category and index required'}), 400

    suggestion = get_destination_suggestion_by_id(suggestion_id)
    if not suggestion or str(suggestion['plan_id']) != str(plan_id):
        return jsonify({'error': 'Destination not found'}), 404

    dest_data = suggestion.get('destination_data') or {}
    card = dest_data.get('card', {})
    items = card.get(category, [])
    if 0 <= idx < len(items):
        removed = items.pop(idx)
        card[category] = items
        dest_data['card'] = card
        update_destination_data(suggestion_id, dest_data)
        logger.info(f"🗑️ Pin removed: {removed.get('name', '?')} from {suggestion['destination_name']}")

    return jsonify({'success': True})


@bp.route('/api/plan/<plan_id>/destination/<suggestion_id>/media', methods=['POST'])
@api_auth_required
def api_add_media(plan_id, suggestion_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    data = request.get_json()
    url = (data.get('url') or '').strip() if data else ''
    if not url:
        return jsonify({'error': 'URL required'}), 400

    # Basic URL validation
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL'}), 400

    suggestion = get_destination_suggestion_by_id(suggestion_id)
    if not suggestion or str(suggestion['plan_id']) != str(plan_id):
        return jsonify({'error': 'Destination not found'}), 404

    dest_data = suggestion.get('destination_data') or {}
    custom_media = dest_data.get('custom_media', [])
    custom_media.append({
        'url': url,
        'caption': (data.get('caption') or '').strip(),
        'added_by': user.get('full_name', user['email']),
    })
    dest_data['custom_media'] = custom_media
    update_destination_data(suggestion_id, dest_data)

    logger.info(f"🎬 Media added to {suggestion['destination_name']} by {user['email']}")
    return jsonify({'success': True})


@bp.route('/api/plan/<plan_id>/destination/<suggestion_id>/media/<int:media_idx>', methods=['DELETE'])
@api_auth_required
def api_remove_media(plan_id, suggestion_id, media_idx):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or str(plan['organizer_id']) != str(user['id']):
        return jsonify({'error': 'Only the organizer can remove media'}), 403

    suggestion = get_destination_suggestion_by_id(suggestion_id)
    if not suggestion or str(suggestion['plan_id']) != str(plan_id):
        return jsonify({'error': 'Destination not found'}), 404

    dest_data = suggestion.get('destination_data') or {}
    custom_media = dest_data.get('custom_media', [])
    if 0 <= media_idx < len(custom_media):
        custom_media.pop(media_idx)
        dest_data['custom_media'] = custom_media
        update_destination_data(suggestion_id, dest_data)

    return jsonify({'success': True})


@bp.route('/api/plan/<plan_id>/vote', methods=['POST'])
@api_auth_required
def api_vote(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    target_type = data.get('target_type')  # 'destination', 'date_window'
    target_id = data.get('target_id')
    vote_val = data.get('vote')  # positive int = rank (1=1st, 2=2nd...), 0 = unvote

    if target_type not in ('destination', 'date_window'):
        return jsonify({'error': 'Invalid target_type'}), 400
    if not isinstance(vote_val, int) or vote_val < 0:
        return jsonify({'error': 'Vote must be 0 or a positive integer'}), 400
    if not target_id:
        return jsonify({'error': 'target_id required'}), 400

    if vote_val == 0:
        success = delete_vote(plan_id, user['id'], target_type, target_id)
    else:
        # Clear this rank from other destinations for this user, then set it
        clear_rank_from_others(plan_id, user['id'], target_type, target_id, vote_val)
        success = upsert_vote(plan_id, user['id'], target_type, target_id, vote_val)
    if success:
        tallies = get_vote_tallies(plan_id, target_type)
        my_votes = get_user_votes(plan_id, user['id'])
        return jsonify({'success': True, 'data': {'tallies': tallies, 'my_votes': my_votes}})
    return jsonify({'error': 'Vote failed'}), 500


@bp.route('/api/plan/<plan_id>/votes')
@api_auth_required
def api_get_votes(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    tallies = get_vote_tallies(plan_id)
    my_votes = get_user_votes(plan_id, user['id'])
    return jsonify({'success': True, 'data': {'tallies': tallies, 'my_votes': my_votes}})


@bp.route('/api/plan/<plan_id>/stage', methods=['POST'])
@api_auth_required
def api_update_stage(plan_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or plan['organizer_id'] != user['id']:
        return jsonify({'error': 'Only the organizer can change the stage'}), 403
    data = request.get_json() or {}
    stage = data.get('stage')
    if stage not in ('voting', 'planning', 'locked', 'booked', 'completed'):
        return jsonify({'error': 'Invalid stage'}), 400
    update_plan_stage(plan_id, stage)
    return jsonify({'success': True, 'stage': stage})


@bp.route('/api/plan/<plan_id>/lock', methods=['POST'])
@api_auth_required
def api_lock_plan(plan_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or plan['organizer_id'] != user['id']:
        return jsonify({'error': 'Only the organizer can lock the plan'}), 403

    data = request.get_json()
    if not data or not data.get('destination'):
        return jsonify({'error': 'Destination required'}), 400

    success = lock_plan(plan_id, data['destination'], data.get('start_date'), data.get('end_date'))
    if success:
        logger.info(f"🔒 Plan locked: {plan['title']} → {data['destination']}")
        # Auto-create per-member price watches in background
        from utilities.watch_engine import create_watches_for_plan
        threading.Thread(target=create_watches_for_plan, args=(plan_id,), daemon=True).start()
        return jsonify({'success': True})
    return jsonify({'error': 'Lock failed'}), 500


@bp.route('/api/plan/<plan_id>/generate', methods=['POST'])
@api_auth_required
def api_generate_recs(plan_id):
    """Kick off rec generation in background — returns immediately."""
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Plan not found'}), 404

    current = _get_rec_status(plan_id)
    if current and current['status'] == 'generating':
        return jsonify({'success': True, 'data': {'status': 'generating'}})

    _set_rec_status(plan_id, 'generating')

    def _do_generate():
        try:
            all_prefs = get_all_plan_preferences(plan_id)
            recs, error = generate_recommendations(plan, all_prefs)
            if error:
                _set_rec_status(plan_id, 'error', error=error)
                return
            if not recs:
                _set_rec_status(plan_id, 'error', error='No recommendations generated')
                return
            delete_recommendations_for_plan(plan_id)
            save_recommendations(plan_id, recs)
            _set_rec_status(plan_id, 'done', count=len(recs))
            logger.info(f"🤖 Generated {len(recs)} recs for {plan['title']}")
        except Exception as e:
            logger.error(f"❌ Rec generation failed for {plan_id}: {e}")
            _set_rec_status(plan_id, 'error', error=str(e))

    threading.Thread(target=_do_generate, daemon=True).start()
    logger.info(f"🤖 Rec generation started in background: {plan['title']} by {user['email']}")
    return jsonify({'success': True, 'data': {'status': 'generating'}})


@bp.route('/api/plan/<plan_id>/generate/status')
@api_auth_required
def api_generate_recs_status(plan_id):
    """Poll this to check if background rec generation is done."""
    member = get_member_for_plan(plan_id, session['user']['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    status = _get_rec_status(plan_id)
    if not status:
        # No active job — check if recs already exist
        recs = get_recommendations(plan_id)
        if recs:
            return jsonify({'success': True, 'data': {'status': 'done', 'count': len(recs)}})
        return jsonify({'success': True, 'data': {'status': 'idle'}})

    return jsonify({'success': True, 'data': status})


@bp.route('/api/recommendation/<recommendation_id>/status', methods=['POST'])
@api_auth_required
def api_update_rec_status(recommendation_id):
    data = request.get_json()
    status = data.get('status') if data else None
    if status not in ('approved', 'rejected', 'suggested'):
        return jsonify({'error': 'Invalid status'}), 400
    success = update_recommendation_status(recommendation_id, status)
    if success:
        return jsonify({'success': True})
    return jsonify({'error': 'Update failed'}), 500


@bp.route('/api/plan/<plan_id>/search/trigger', methods=['POST'])
@api_auth_required
def api_search_trigger(plan_id):
    """Trigger background search fan-out for this plan."""
    plan = get_plan_by_id(plan_id)
    if not plan:
        return jsonify({'error': 'Plan not found'}), 404
    member = get_member_for_plan(plan_id, session['user']['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    destination = plan.get('destination') or plan.get('locked_destination')
    if not destination:
        return jsonify({'error': 'No destination set on plan yet'}), 400

    checkin = str(plan.get('start_date') or plan.get('locked_start_date') or '')
    checkout = str(plan.get('end_date') or plan.get('locked_end_date') or '')

    all_prefs = get_all_plan_preferences(plan_id)
    origin_airports = [p['home_airport'] for p in all_prefs if p.get('home_airport')]
    guests = len(all_prefs) or 2

    trigger_search(plan_id, destination, checkin or None, checkout or None,
                   origin_airports or None, guests)

    logger.info(f"🔍 Search triggered: {destination} for plan {plan_id}")
    return jsonify({'success': True, 'data': {'status': 'searching', 'destination': destination}})


@bp.route('/api/plan/<plan_id>/search/status')
@api_auth_required
def api_search_status(plan_id):
    """Quick poll: is the search still running? How many results so far?"""
    member = get_member_for_plan(plan_id, session['user']['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403
    results = get_search_results(plan_id, limit=1000)
    return jsonify({'success': True, 'data': {
        'searching': is_searching(plan_id),
        'count': len(results),
    }})


@bp.route('/api/plan/<plan_id>/search/results')
@api_auth_required
def api_search_results(plan_id):
    """Return all accumulated search results (for initial page load)."""
    member = get_member_for_plan(plan_id, session['user']['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403
    result_type = request.args.get('type')  # flight | hotel | activity | car
    since_id = int(request.args.get('since_id', 0))
    results = get_search_results(plan_id, result_type=result_type, since_id=since_id)
    return jsonify({'success': True, 'data': {
        'results': results,
        'searching': is_searching(plan_id),
    }})


@bp.route('/api/plan/<plan_id>/search/stream')
@api_auth_required
def api_search_stream(plan_id):
    """
    SSE endpoint — pushes new search_results rows to connected clients as they land.
    Frontend connects once; new cards appear automatically as adapters return data.
    Closes when all adapters finish (searching=False and no new rows for 3 cycles).
    """
    member = get_member_for_plan(plan_id, session['user']['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    import time

    def generate():
        last_id = int(request.args.get('since_id', 0))
        idle_cycles = 0

        while True:
            new_rows = get_search_results(plan_id, since_id=last_id, limit=50)
            if new_rows:
                idle_cycles = 0
                for row in new_rows:
                    last_id = row['pk_id']
                    yield f"data: {json.dumps(row)}\n\n"
            else:
                idle_cycles += 1

            # Done when search finished and no new rows for 3 seconds
            if not is_searching(plan_id) and idle_cycles >= 3:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break

            time.sleep(1)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )
