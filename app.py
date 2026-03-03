import json
import logging
import os
from datetime import timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from utilities.google_auth_utils import get_secret
from utilities.postgres_utils import (
    init_database, upsert_user, get_user_profile, update_user_profile,
    get_user_tokens, update_user_tokens, set_user_calendar_synced,
    create_plan, get_plans_for_user, get_plan_by_id, get_plan_by_invite_token,
    add_plan_member, get_plan_members, get_member_for_plan,
    get_plan_preferences, upsert_plan_preferences, get_all_plan_preferences,
    save_member_availability, get_plan_availability, get_availability_overlap,
    create_destination_suggestion, update_destination_suggestion,
    get_destination_suggestions, get_destination_suggestion_by_id,
    upsert_vote, get_vote_tallies, get_user_votes, lock_plan,
    save_recommendations, get_recommendations, update_recommendation_status,
    delete_recommendations_for_plan,
)
from utilities.invite_utils import generate_token
from utilities.trip_ai import generate_recommendations, generate_destination_card, suggest_destinations
from utilities.calendar_utils import get_calendar_events, compute_free_windows, refresh_access_token
from utilities.amadeus_utils import research_destination

# ── App setup ────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'crab-dev-secret-change-me')
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Auth setup ───────────────────────────────────────────────

google_auth = None
try:
    client_id = get_secret('CRAB_GOOGLE_CLIENT_ID')
    client_secret = get_secret('CRAB_GOOGLE_CLIENT_SECRET')
    if client_id and client_secret:
        from authlib.integrations.flask_client import OAuth
        oauth = OAuth(app)
        google_auth = oauth.register(
            name='google',
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
            client_kwargs={
                'scope': 'openid email profile https://www.googleapis.com/auth/calendar.readonly',
                'access_type': 'offline',
                'prompt': 'consent',
            },
        )
except Exception as e:
    logger.warning(f"⚠️ Auth not configured: {e}")

AUTH_ENABLED = google_auth is not None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and 'user' not in session:
            logger.info(f"🚫 login_required blocked: session keys={list(session.keys())}, path={request.path}")
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def api_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and 'user' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return decorated


# ── Database init ────────────────────────────────────────────

try:
    init_database()
except Exception as e:
    logger.warning(f"⚠️ Database init deferred: {e}")


# ── Public routes ────────────────────────────────────────────

@app.route('/')
def index():
    user = session.get('user')
    if user:
        return redirect('/dashboard')
    return render_template('index.html', active_page='home')


@app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200


# ── Auth routes ──────────────────────────────────────────────

@app.route('/login')
def login():
    if not AUTH_ENABLED:
        return redirect('/')
    if 'user' in session:
        return redirect('/dashboard')
    return render_template('login.html', active_page='login')


@app.route('/login/google')
def login_google():
    if not AUTH_ENABLED:
        return redirect('/')
    return google_auth.authorize_redirect(url_for('auth_callback', _external=True))


@app.route('/auth/callback')
def auth_callback():
    if not AUTH_ENABLED:
        return redirect('/')
    try:
        logger.info(f"🔑 Auth callback hit, args: {dict(request.args)}")
        token = google_auth.authorize_access_token()
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
                    logger.info(f"🔑 Session set for: {db_user['email']}")
                    # Redirect to pending join if there was one
                    pending_join = session.pop('pending_join', None)
                    if pending_join:
                        return redirect(f"/join/{pending_join}")
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


@app.route('/logout')
def logout():
    email = session.get('user', {}).get('email', 'unknown')
    session.pop('user', None)
    logger.info(f"🔑 Logout: {email}")
    return redirect('/')


# ── Dashboard ────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    user = session['user']
    plans = get_plans_for_user(user['id'])
    logger.info(f"📍 Dashboard: {user['email']}")
    return render_template('dashboard.html', active_page='dashboard', user=user, plans=plans)


# ── Profile ──────────────────────────────────────────────────

@app.route('/profile')
@login_required
def profile():
    user = session['user']
    profile_data = get_user_profile(user['id'])
    logger.info(f"📍 Profile: {user['email']}")
    return render_template('profile.html', active_page='profile', user=user, profile=profile_data)


@app.route('/api/profile', methods=['POST'])
@api_auth_required
def api_update_profile():
    user = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    success = update_user_profile(user['id'], data)
    if success:
        logger.info(f"💬 Profile updated: {user['email']}")
        return jsonify({'success': True})
    return jsonify({'error': 'Update failed'}), 500


# ── Plan routes ─────────────────────────────────────────────

@app.route('/plan/new')
@login_required
def plan_new():
    user = session['user']
    logger.info(f"📍 New plan: {user['email']}")
    return render_template('plan_new.html', active_page='dashboard', user=user)


@app.route('/api/plan/create', methods=['POST'])
@api_auth_required
def api_create_plan():
    user = session['user']
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'error': 'Title is required'}), 400
    invite_token = generate_token()
    plan = create_plan(user['id'], data, invite_token)
    if not plan:
        return jsonify({'error': 'Failed to create plan'}), 500
    # Add organizer as first member
    member_token = generate_token()
    add_plan_member(plan['plan_id'], user['name'], member_token, email=user['email'], user_id=user['id'], role='organizer')
    logger.info(f"💬 Plan created: {plan['title']} by {user['email']}")
    return jsonify({'success': True, 'data': {'plan_id': str(plan['plan_id']), 'invite_token': plan['invite_token']}})


@app.route('/join/<invite_token>', methods=['GET', 'POST'])
def join_plan(invite_token):
    plan = get_plan_by_invite_token(invite_token)
    if not plan:
        return render_template('index.html', active_page='home', error='Plan not found'), 404

    user = session.get('user')

    if request.method == 'GET':
        # Must be logged in to join
        if not user:
            session['pending_join'] = invite_token
            return render_template('join.html', plan=plan, already_member=False, user=None, needs_login=True)

        # Check if already a member
        existing = get_member_for_plan(plan['plan_id'], user['id'])
        if existing:
            return render_template('join.html', plan=plan, already_member=True, user=user, needs_login=False)

        return render_template('join.html', plan=plan, already_member=False, user=user, needs_login=False)

    # POST — join the plan (must be logged in)
    if not user:
        return jsonify({'error': 'Must be logged in to join'}), 401

    # Check if already a member
    existing = get_member_for_plan(plan['plan_id'], user['id'])
    if existing:
        return jsonify({'success': True, 'data': {'already_member': True}})

    member_token = generate_token()
    member = add_plan_member(
        plan['plan_id'], user['name'], member_token,
        email=user['email'], user_id=user['id'],
    )
    if not member:
        return jsonify({'error': 'Failed to join'}), 500

    logger.info(f"👋 Joined plan: {user['name']} → {plan['title']}")
    return jsonify({'success': True, 'data': {'plan_id': str(plan['plan_id'])}})


@app.route('/plan/<plan_id>')
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
    recs = get_recommendations(plan_id) if plan.get('locked_destination') else []
    destinations = get_destination_suggestions(plan_id) if not plan.get('locked_destination') else []
    return render_template('plan.html', plan=plan, members=members, all_prefs=all_prefs,
                           recs=recs, destinations=destinations, is_organizer=is_organizer, user=user)


def _resolve_member(plan_id):
    user = session.get('user')
    if user:
        return get_member_for_plan(plan_id, user['id'])
    return None


@app.route('/plan/<plan_id>/preferences')
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


@app.route('/api/plan/<plan_id>/preferences', methods=['POST'])
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


# ── Calendar routes ─────────────────────────────────────────

@app.route('/api/calendar/sync', methods=['POST'])
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


@app.route('/api/plan/<plan_id>/sync-calendar', methods=['POST'])
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


@app.route('/api/plan/<plan_id>/availability')
@api_auth_required
def api_plan_availability(plan_id):
    """Get group availability overlap for this plan."""
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    overlap = get_availability_overlap(plan_id)
    return jsonify({'success': True, 'data': {'windows': overlap}})


# ── Destination suggestion routes ───────────────────────────

@app.route('/api/plan/<plan_id>/suggest-destination', methods=['POST'])
@api_auth_required
def api_suggest_destination(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    data = request.get_json()
    destination_name = data.get('destination') if data else None
    if not destination_name:
        return jsonify({'error': 'Destination name required'}), 400

    # Create the suggestion in 'researching' status
    suggestion = create_destination_suggestion(plan_id, user['id'], destination_name)
    if not suggestion:
        return jsonify({'error': 'Failed to create suggestion'}), 500

    # Kick off research in background thread
    import threading
    suggestion_id = suggestion['suggestion_id']

    def _do_research():
        try:
            all_prefs = get_all_plan_preferences(plan_id)
            airports = [m.get('home_airport') for m in all_prefs if m.get('home_airport')]
            research = research_destination(destination_name, airports)
            card = generate_destination_card(destination_name, research, all_prefs)
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

    # Return immediately — card shows as "researching"
    return jsonify({
        'success': True,
        'data': {
            'suggestion_id': str(suggestion_id),
            'status': 'researching',
        }
    })


@app.route('/api/plan/<plan_id>/suggest-anywhere', methods=['POST'])
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


@app.route('/api/plan/<plan_id>/destinations')
@api_auth_required
def api_get_destinations(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    suggestions = get_destination_suggestions(plan_id)
    return jsonify({'success': True, 'data': {'destinations': suggestions}})


# ── Voting + Lock-in routes ─────────────────────────────────

@app.route('/api/plan/<plan_id>/vote', methods=['POST'])
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
    vote = data.get('vote')  # 1 or -1

    if target_type not in ('destination', 'date_window'):
        return jsonify({'error': 'Invalid target_type'}), 400
    if vote not in (1, -1):
        return jsonify({'error': 'Vote must be 1 or -1'}), 400
    if not target_id:
        return jsonify({'error': 'target_id required'}), 400

    success = upsert_vote(plan_id, user['id'], target_type, target_id, vote)
    if success:
        tallies = get_vote_tallies(plan_id, target_type)
        return jsonify({'success': True, 'data': {'tallies': tallies}})
    return jsonify({'error': 'Vote failed'}), 500


@app.route('/api/plan/<plan_id>/votes')
@api_auth_required
def api_get_votes(plan_id):
    user = session['user']
    member = get_member_for_plan(plan_id, user['id'])
    if not member:
        return jsonify({'error': 'Not a member'}), 403

    tallies = get_vote_tallies(plan_id)
    my_votes = get_user_votes(plan_id, user['id'])
    return jsonify({'success': True, 'data': {'tallies': tallies, 'my_votes': my_votes}})


@app.route('/api/plan/<plan_id>/lock', methods=['POST'])
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
        return jsonify({'success': True})
    return jsonify({'error': 'Lock failed'}), 500


# ── Recommendation routes ───────────────────────────────────

@app.route('/api/plan/<plan_id>/generate', methods=['POST'])
@api_auth_required
def api_generate_recs(plan_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or plan['organizer_id'] != user['id']:
        return jsonify({'error': 'Only the organizer can generate recommendations'}), 403

    all_prefs = get_all_plan_preferences(plan_id)
    recs, error = generate_recommendations(plan, all_prefs)
    if error:
        return jsonify({'error': error}), 400
    if not recs:
        return jsonify({'error': 'No recommendations generated'}), 500

    # Clear old recs and save new ones
    delete_recommendations_for_plan(plan_id)
    save_recommendations(plan_id, recs)

    logger.info(f"🤖 Generated {len(recs)} recs for {plan['title']} by {user['email']}")
    return jsonify({'success': True, 'data': {'count': len(recs)}})


@app.route('/api/recommendation/<recommendation_id>/status', methods=['POST'])
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


# ── Run ──────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=True)
