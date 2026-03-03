import json
import logging
import os
from datetime import timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
from werkzeug.middleware.proxy_fix import ProxyFix

from utilities.google_auth_utils import get_secret
from utilities.postgres_utils import (
    init_database, upsert_user, get_user_profile, update_user_profile,
    create_plan, get_plans_for_user, get_plan_by_id, get_plan_by_invite_token,
    add_plan_member, get_plan_members, get_member_by_token, get_member_for_plan,
    get_plan_preferences, upsert_plan_preferences, get_all_plan_preferences,
    save_recommendations, get_recommendations, update_recommendation_status,
    delete_recommendations_for_plan,
)
from utilities.invite_utils import generate_token, set_member_cookie, get_member_token_from_cookie
from utilities.trip_ai import generate_recommendations

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
            client_kwargs={'scope': 'openid email profile'},
        )
except Exception as e:
    logger.warning(f"⚠️ Auth not configured: {e}")

AUTH_ENABLED = google_auth is not None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and 'user' not in session:
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
        token = google_auth.authorize_access_token()
        if token:
            user_info = token.get('userinfo')
            if user_info:
                db_user = upsert_user(user_info)
                if db_user:
                    session.permanent = True
                    session['user'] = {
                        'id': db_user['pk_id'],
                        'email': db_user['email'],
                        'name': db_user['full_name'],
                        'picture': db_user['picture_url'],
                    }
                    logger.info(f"🔑 Login: {db_user['email']}")
                    return redirect('/dashboard')
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

    if request.method == 'GET':
        # Check if already a member (via session or cookie)
        already_member = False
        user = session.get('user')
        if user:
            existing = get_member_for_plan(plan['plan_id'], user['id'])
            if existing:
                already_member = True
        elif get_member_token_from_cookie():
            member = get_member_by_token(get_member_token_from_cookie())
            if member and str(member['plan_id']) == str(plan['plan_id']):
                already_member = True
        return render_template('join.html', plan=plan, already_member=already_member, user=user)

    # POST — join the plan
    data = request.get_json()
    if not data or not data.get('display_name'):
        return jsonify({'error': 'Name is required'}), 400

    user = session.get('user')
    user_id = user['id'] if user else None

    # Check if authed user is already a member
    if user_id:
        existing = get_member_for_plan(plan['plan_id'], user_id)
        if existing:
            return jsonify({'success': True, 'data': {'already_member': True}})

    member_token = generate_token()
    member = add_plan_member(
        plan['plan_id'], data['display_name'], member_token,
        email=data.get('email'), user_id=user_id,
    )
    if not member:
        return jsonify({'error': 'Failed to join'}), 500

    logger.info(f"👋 Joined plan: {data['display_name']} → {plan['title']}")
    resp = make_response(jsonify({'success': True, 'data': {'member_token': member_token}}))
    if not user_id:
        set_member_cookie(resp, member_token)
    return resp


@app.route('/plan/<plan_id>')
def view_plan(plan_id):
    plan = get_plan_by_id(plan_id)
    if not plan:
        return redirect('/dashboard')

    user = session.get('user')
    is_organizer = user and user['id'] == plan['organizer_id']

    # Access check: must be organizer, authed member, or have member_token cookie
    has_access = is_organizer
    if not has_access and user:
        member = get_member_for_plan(plan_id, user['id'])
        has_access = member is not None
    if not has_access:
        token = get_member_token_from_cookie()
        if token:
            member = get_member_by_token(token)
            has_access = member and str(member['plan_id']) == str(plan_id)
    if not has_access:
        return redirect(f"/join/{plan['invite_token']}")

    members = get_plan_members(plan_id)
    all_prefs = get_all_plan_preferences(plan_id)
    recs = get_recommendations(plan_id)
    return render_template('plan.html', plan=plan, members=members, all_prefs=all_prefs, recs=recs, is_organizer=is_organizer, user=user)


def _resolve_member(plan_id):
    user = session.get('user')
    if user:
        return get_member_for_plan(plan_id, user['id'])
    token = get_member_token_from_cookie()
    if token:
        member = get_member_by_token(token)
        if member and str(member['plan_id']) == str(plan_id):
            return member
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
