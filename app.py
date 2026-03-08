import json
import logging
import os
import threading
from datetime import timedelta
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix

from utilities.google_auth_utils import get_secret
from utilities.postgres_utils import (
    init_database, upsert_user, get_user_profile, update_user_profile,
    get_user_tokens, update_user_tokens, set_user_calendar_synced,
    create_plan, get_plans_for_user, get_plan_by_id, get_plan_by_invite_token,
    add_plan_member, get_plan_members, get_member_for_plan,
    get_plan_preferences, upsert_plan_preferences, get_all_plan_preferences,
    save_member_availability, get_plan_availability, get_availability_overlap,
    create_destination_suggestion, update_destination_suggestion, update_destination_data,
    get_destination_suggestions, get_destination_suggestion_by_id,
    upsert_vote, delete_vote, get_vote_tallies, get_user_votes, lock_plan,
    save_recommendations, get_recommendations, update_recommendation_status,
    delete_recommendations_for_plan,
    save_member_blackouts, get_member_blackouts,
    save_member_tentative_dates, get_member_tentative_dates,
    update_member_details,
    delete_plan, delete_destination_suggestion,
    create_message, get_plan_messages, delete_message,
    update_plan_stage, get_plan_blackouts, get_plan_tentative_dates,
)
from utilities.invite_utils import generate_token
from utilities.trip_ai import generate_recommendations, generate_destination_card, suggest_destinations
from utilities.calendar_utils import get_calendar_events, compute_free_windows, refresh_access_token
from utilities.search_engine import trigger_search, is_searching
from utilities.postgres_utils import get_search_results, clear_search_results, get_deals_cache_grouped, log_invite_view, get_invite_view_stats, get_db_connection, get_all_member_votes
from utilities.deals_engine import get_hot_deals, get_hot_deals_grouped, refresh_deals_cache

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
                'scope': 'openid email profile',
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


# ── Background job status (in-memory, same pattern as inroads) ────

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


# ── Public routes ────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', active_page='home')


@app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200


@app.route('/privacy')
def privacy():
    return render_template('privacy.html', active_page=None)


@app.route('/terms')
def terms():
    return render_template('terms.html', active_page=None)


@app.route('/contact')
def contact():
    return render_template('contact.html', active_page=None)


@app.route('/api/contact', methods=['POST'])
def api_contact():
    try:
        data = request.get_json()
        email = data.get('email', '').strip()
        message = data.get('message', '').strip()
        honeypot = data.get('honeypot', '').strip()
        time_open = data.get('time_open', 0)

        if honeypot:
            logger.warning(f"Spam blocked: honeypot from {request.remote_addr}")
            return jsonify({'error': 'Invalid submission'}), 400

        if time_open < 3000:
            return jsonify({'error': 'Please take a moment to review your message'}), 400

        if not email or not message:
            return jsonify({'error': 'Email and message are required'}), 400

        from utilities.gmail_utils import send_simple_email
        subject = f"[crab] Contact: {email}"
        body = f"""New contact from crab.travel:

From: {email}
IP: {request.remote_addr}
User Agent: {request.headers.get('User-Agent', 'Unknown')}
Time open: {time_open/1000:.1f}s

Message:
{message}

---
Sent from crab.travel/contact
"""
        success = send_simple_email(subject=subject, body=body, to_email='andy.tillo@gmail.com', from_name='crab.travel')
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Failed to send'}), 500
    except Exception as e:
        logger.error(f"Contact form error: {e}")
        return jsonify({'error': str(e)}), 500


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
    # Get phone/sms from users table
    user_phone = ''
    user_sms_on = False
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT phone_number, sms_notifications FROM crab.users WHERE pk_id = %s", (user['id'],))
        row = cur.fetchone()
        if row:
            user_phone = row[0] or ''
            user_sms_on = row[1] or False
        cur.close()
        conn.close()
    except Exception:
        pass
    logger.info(f"📍 Profile: {user['email']}")
    return render_template('profile.html', active_page='profile', user=user, profile=profile_data,
                           user_phone=user_phone, user_sms_on=user_sms_on)


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


@app.route('/api/plan/<plan_id>/delete', methods=['POST'])
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


@app.route('/join/<invite_token>')
@app.route('/in/<invite_token>')
def join_plan(invite_token):
    return redirect(f'/to/{invite_token}', code=301)


@app.route('/to/<invite_token>')
def invite_page(invite_token):
    plan = get_plan_by_invite_token(invite_token)
    if not plan:
        return render_template('index.html', active_page='home', error='Plan not found'), 404

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

    # Calendar data — all members' blackouts + tentative dates
    all_blackouts = get_plan_blackouts(plan['plan_id']) if not (user is None) else []
    all_tentative = get_plan_tentative_dates(plan['plan_id']) if not (user is None) else []
    calendar_json = json.dumps({
        'blackouts': [{'name': b['full_name'], 'start': b['blackout_start'].isoformat(), 'end': b['blackout_end'].isoformat()} for b in all_blackouts],
        'tentative': [{'name': t['full_name'], 'start': t['date_start'].isoformat(), 'end': t['date_end'].isoformat()} for t in all_tentative],
        'members': [{'name': m['display_name'], 'is_flexible': m.get('is_flexible', False)} for m in members],
    }, default=_default_ser)

    return render_template('invite.html',
        plan=plan, destinations=destinations, members=members,
        vote_tallies=vote_tallies, my_votes=my_votes,
        user=user, is_member=is_member, member=member,
        blackouts=blackouts, tentative_dates=tentative_dates,
        member_airport=member_airport,
        member_flexible=member_flexible,
        needs_login=(user is None),
        profile_completed=profile_completed,
        destinations_json=destinations_json,
        calendar_json=calendar_json if not (user is None) else '{}',
    )


@app.route('/api/plan/<plan_id>/join-full', methods=['POST'])
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

    # Update airport + flexible
    home_airport = data.get('home_airport', '').strip()
    is_flexible = data.get('is_flexible', False)
    update_member_details(member['pk_id'], home_airport=home_airport or None, is_flexible=is_flexible)

    # Also save airport to user profile so it carries across plans
    if home_airport:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE crab.users SET home_airport = %s WHERE pk_id = %s AND (home_airport IS NULL OR home_airport = '')", (home_airport, user['id']))
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

    # Auto-vote yes for whoever suggests a destination
    upsert_vote(plan_id, user['id'], 'destination', suggestion['suggestion_id'], 1)

    plan = get_plan_by_id(plan_id)
    _research_destination(plan_id, suggestion['suggestion_id'], destination_name, plan)

    # Return immediately — card shows as "researching"
    return jsonify({
        'success': True,
        'data': {
            'suggestion_id': str(suggestion['suggestion_id']),
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


@app.route('/api/plan/<plan_id>/destination/<suggestion_id>', methods=['DELETE'])
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


@app.route('/api/plan/<plan_id>/destination/<suggestion_id>/pin', methods=['POST'])
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


@app.route('/api/plan/<plan_id>/destination/<suggestion_id>/pin', methods=['DELETE'])
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


@app.route('/api/plan/<plan_id>/destination/<suggestion_id>/media', methods=['POST'])
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


@app.route('/api/plan/<plan_id>/destination/<suggestion_id>/media/<int:media_idx>', methods=['DELETE'])
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
    vote_val = data.get('vote')  # 1, -1, or 0 (unvote)

    if target_type not in ('destination', 'date_window'):
        return jsonify({'error': 'Invalid target_type'}), 400
    if vote_val not in (1, -1, 0):
        return jsonify({'error': 'Vote must be 1, -1, or 0'}), 400
    if not target_id:
        return jsonify({'error': 'target_id required'}), 400

    if vote_val == 0:
        success = delete_vote(plan_id, user['id'], target_type, target_id)
    else:
        success = upsert_vote(plan_id, user['id'], target_type, target_id, vote_val)
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


@app.route('/api/plan/<plan_id>/stage', methods=['POST'])
@api_auth_required
def api_update_stage(plan_id):
    user = session['user']
    plan = get_plan_by_id(plan_id)
    if not plan or plan['organizer_id'] != user['id']:
        return jsonify({'error': 'Only the organizer can change the stage'}), 403
    data = request.get_json() or {}
    stage = data.get('stage')
    if stage not in ('voting', 'planning', 'locked'):
        return jsonify({'error': 'Invalid stage'}), 400
    update_plan_stage(plan_id, stage)
    return jsonify({'success': True, 'stage': stage})


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


@app.route('/api/plan/<plan_id>/generate/status')
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


# ── Search routes ────────────────────────────────────────────

@app.route('/api/plan/<plan_id>/search/trigger', methods=['POST'])
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


@app.route('/api/plan/<plan_id>/search/status')
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


@app.route('/api/plan/<plan_id>/search/results')
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


@app.route('/api/plan/<plan_id>/search/stream')
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


# ── Deals routes ─────────────────────────────────────────────

@app.route('/api/deals')
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


@app.route('/tasks/refresh-deals')
def task_refresh_deals():
    """Nightly cron job — refresh deals cache from all sources for all hubs."""
    # App Engine cron jobs set this header; reject external calls
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    try:
        total = refresh_deals_cache()
        logger.info(f"✅ /tasks/refresh-deals complete: {total} deals cached")
        return jsonify({'success': True, 'deals_upserted': total})
    except Exception as e:
        logger.error(f"❌ refresh-deals failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/plan/<plan_id>/deals')
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


# ── YouTube search ─────────────────────────────────────────────

@app.route('/api/youtube-search')
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


# ── Pexels photo search ───────────────────────────────────────

@app.route('/api/photo-search')
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


# ── Chat / Messages routes ────────────────────────────────────

@app.route('/api/plan/<plan_id>/messages')
@api_auth_required
def api_get_messages(plan_id):
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


@app.route('/api/plan/<plan_id>/messages', methods=['POST'])
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

    # Send SMS notifications to members (async, don't block response)
    try:
        from utilities.sms_utils import notify_plan_members_sms
        import threading
        threading.Thread(
            target=notify_plan_members_sms,
            args=(plan_id, display_name, content, user['id']),
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning(f"SMS notification dispatch failed: {e}")

    return jsonify({'success': True, 'data': {'message': msg}})


@app.route('/api/plan/<plan_id>/messages/<message_id>', methods=['DELETE'])
@api_auth_required
def api_delete_message(plan_id, message_id):
    user = session['user']
    success = delete_message(message_id, user['id'])
    return jsonify({'success': success})


# ── Twilio SMS Webhook (inbound) ─────────────────────────────

@app.route('/api/sms/inbound', methods=['POST'])
def sms_inbound():
    """Handle inbound SMS replies from Twilio and post them to the user's most recent plan chat."""
    from_number = request.form.get('From', '')
    body = request.form.get('Body', '').strip()
    if not from_number or not body:
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Find user by phone number
        cur.execute("SELECT pk_id, full_name FROM crab.users WHERE phone_number = %s", (from_number,))
        user_row = cur.fetchone()
        if user_row:
            user_id, display_name = user_row
            # Find their most recent plan
            cur.execute("""
                SELECT m.plan_id FROM crab.plan_members m
                JOIN crab.plans p ON p.plan_id = m.plan_id
                WHERE m.user_id = %s
                ORDER BY p.updated_at DESC LIMIT 1
            """, (user_id,))
            plan_row = cur.fetchone()
            if plan_row:
                plan_id = str(plan_row[0])
                create_message(plan_id, user_id, display_name, f"[via SMS] {body}")
                logger.info(f"📱 SMS→Chat: {display_name} in plan {plan_id}")
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"SMS inbound error: {e}")

    # Always return valid TwiML
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')


# ── Run ──────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=True)
