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
import psycopg2.extras
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


# ── Refresh admin flag (once per session, or if missing) ─────
@app.before_request
def refresh_admin_flag():
    user = session.get('user')
    if user and 'id' in user and 'user_is_admin' not in session:
        from utilities.admin_utils import is_admin as check_admin
        session['user_is_admin'] = check_admin(user['id'])

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html', active_page=None), 404

# ── Public routes ────────────────────────────────────────────

@app.route('/sitemap.xml')
def sitemap():
    host = request.host_url.rstrip('/')
    skip = {'api', 'admin', 'auth', 'login', 'logout', 'callback', 'health', 'sitemap', 'robots', 'tasks', 'cron', 'debug', 'mimic'}
    urls = []
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.arguments:
            continue
        path = rule.rule
        parts = path.strip('/').split('/')
        if any(p in skip for p in parts):
            continue
        if path.startswith('/api/') or path.startswith('/admin'):
            continue
        priority = '1.0' if path == '/' else '0.6'
        urls.append(f'  <url><loc>{host}{path}</loc><priority>{priority}</priority></url>')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + '\n'.join(sorted(urls)) + '\n</urlset>'
    return Response(xml, mimetype='application/xml')

@app.route('/robots.txt')
def robots():
    host = request.host_url.rstrip('/')
    content = f'User-agent: *\nAllow: /\nSitemap: {host}/sitemap.xml\n'
    return Response(content, mimetype='text/plain')

@app.route('/')
def index():
    return render_template('index.html', active_page='home')


@app.route('/crab-animations')
def crab_animations():
    return render_template('crab_animations.html')


@app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200


@app.route('/privacy')
def privacy():
    return render_template('privacy.html', active_page=None)


@app.route('/terms')
def terms():
    return render_template('terms.html', active_page=None)


@app.route('/about')
def about():
    return render_template('about.html', active_page='about')


@app.route('/roadmap')
def roadmap():
    return render_template('roadmap.html', active_page='about')


@app.route('/api/roadmap/comments', methods=['GET'])
def api_roadmap_comments_get():
    """Fetch all comments for the roadmap page."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crab.roadmap_comments (
                id SERIAL PRIMARY KEY,
                section_idx INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                author_type TEXT DEFAULT 'anon',
                comment_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.execute("""
            SELECT id, section_idx, author_name, author_type, comment_text, created_at
            FROM crab.roadmap_comments ORDER BY section_idx, created_at ASC
        """)
        comments = [dict(r) for r in cur.fetchall()]
        for c in comments:
            if c.get('created_at'):
                c['created_at'] = c['created_at'].isoformat()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'comments': comments})
    except Exception as e:
        logger.error(f"Roadmap comments GET error: {e}")
        return jsonify({'success': False, 'comments': []})


@app.route('/api/roadmap/comments', methods=['POST'])
def api_roadmap_comments_post():
    """Post a comment to a roadmap section. Works for logged-in or anonymous users."""
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'success': False}), 400

    # Anti-bot: honeypot and timing check
    honeypot = data.get('honeypot', '').strip()
    time_open = data.get('time_open', 0)
    if honeypot:
        logger.warning(f"Roadmap spam blocked: honeypot from {request.remote_addr}")
        return jsonify({'success': True, 'comment': {'id': 0, 'section_idx': 0, 'author_name': 'Guest', 'author_type': 'anon', 'comment_text': '', 'created_at': ''}}), 200
    if time_open < 2000:
        logger.warning(f"Roadmap spam blocked: too fast ({time_open}ms) from {request.remote_addr}")
        return jsonify({'success': True, 'comment': {'id': 0, 'section_idx': 0, 'author_name': 'Guest', 'author_type': 'anon', 'comment_text': '', 'created_at': ''}}), 200

    section_idx = int(data.get('section_idx', 0))
    text = data['text'].strip()[:2000]
    if not text:
        return jsonify({'success': False}), 400

    # Determine author
    if 'user' in session and session['user'].get('name'):
        author_name = session['user']['name']
        author_type = 'user'
    else:
        anon_id = request.cookies.get('crab_anon')
        if not anon_id:
            import random
            anon_id = f"anon_{random.randint(10000, 99999)}"
        author_name = f"Guest {anon_id[-5:]}"
        author_type = 'anon'

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO crab.roadmap_comments (section_idx, author_name, author_type, comment_text)
            VALUES (%s, %s, %s, %s)
            RETURNING id, section_idx, author_name, author_type, comment_text, created_at
        """, (section_idx, author_name, author_type, text))
        conn.commit()
        row = dict(cur.fetchone())
        if row.get('created_at'):
            row['created_at'] = row['created_at'].isoformat()
        cur.close()
        conn.close()

        resp = jsonify({'success': True, 'comment': row})
        if author_type == 'anon':
            resp.set_cookie('crab_anon', anon_id, max_age=60*60*24*365, httponly=True, samesite='Lax')
        return resp
    except Exception as e:
        logger.error(f"Roadmap comment POST error: {e}")
        return jsonify({'success': False}), 500


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


# ── Admin routes ─────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_panel():
    from utilities.admin_utils import is_admin, get_admin_dashboard_data
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    data = get_admin_dashboard_data()
    data['mimic_active'] = '_real_uid' in session
    return render_template('admin.html', active_page='admin', **data)


@app.route('/admin/mimic', methods=['GET', 'POST'])
@login_required
def admin_mimic():
    from utilities.admin_utils import is_admin, handle_mimic_action
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    if request.method == 'POST':
        action = request.form.get('action')
        target = request.form.get('target_user_id')
        handle_mimic_action(session, action, target, real_uid)
        return redirect('/admin/mimic')
    # GET — show user list
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT u.pk_id, u.email, u.full_name,
               (SELECT COUNT(*) FROM crab.plan_members m WHERE m.user_id = u.pk_id) as plan_count
        FROM crab.users u ORDER BY u.pk_id
    """)
    users = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('admin_mimic.html', users=users,
                           mimic_active='_real_uid' in session)


@app.route('/api/admin/test-email', methods=['POST'])
@api_auth_required
def api_admin_test_email():
    from utilities.admin_utils import is_admin
    from utilities.gmail_utils import send_simple_email
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403
    email = session['user']['email']
    sent = send_simple_email(
        subject="[crab.travel] Admin test email",
        body="This is a test email from the crab.travel admin panel.\n\nIf you received this, the email pipeline is working.",
        to_email=email,
    )
    if sent:
        return jsonify({'success': True, 'to': email})
    return jsonify({'success': False, 'error': 'Email send failed'}), 500


@app.route('/api/admin/test-sms', methods=['POST'])
@api_auth_required
def api_admin_test_sms():
    from utilities.admin_utils import is_admin
    from utilities.sms_utils import send_sms
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT phone_number FROM crab.users WHERE pk_id = %s", (real_uid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    phone = row[0] if row else None
    if not phone:
        return jsonify({'success': False, 'error': 'No phone number on your admin account'})
    result = send_sms(phone, "[crab.travel] Admin test SMS — pipeline working")
    if result:
        return jsonify({'success': True, 'to': phone})
    return jsonify({'success': False, 'error': 'SMS send failed (A2P may be pending)'})


# ── Bot Login (secret-gated, no OAuth needed) ────────────────────────────────

@app.route('/api/bot/login', methods=['POST'])
def api_bot_login():
    """Authenticate as a bot user. Gated by CRAB_BOT_SECRET."""
    data = request.get_json(force=True)
    secret = data.get('secret', '')
    user_id = data.get('user_id')
    expected = get_secret('CRAB_BOT_SECRET')
    if not expected or secret != expected:
        return jsonify({'error': 'Forbidden'}), 403
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    from utilities.admin_utils import _get_user_session_data
    user_data = _get_user_session_data(int(user_id))
    if not user_data:
        return jsonify({'error': 'User not found'}), 404
    session.permanent = True
    session['user'] = user_data
    return jsonify({'success': True, 'user': user_data})


@app.route('/api/admin/smoke-test', methods=['POST'])
@api_auth_required
def api_admin_smoke_test():
    from utilities.admin_utils import is_admin
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403
    import subprocess, time
    start = time.time()
    try:
        result = subprocess.run(
            ['python3', 'dev/smoke_test.py', '--quick'],
            capture_output=True, text=True, timeout=30, cwd='/app' if os.path.exists('/app') else '.'
        )
        elapsed = round(time.time() - start, 1)
        output = result.stdout + result.stderr
        # Parse results from output
        passed = output.count('✅')
        failed = output.count('❌')
        return jsonify({
            'success': True, 'passed': passed, 'failed': failed,
            'total': passed + failed, 'time': elapsed, 'output': output[-500:]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/changelog')
@login_required
def admin_changelog():
    from utilities.admin_utils import is_admin
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    import requests as req
    commits = []
    try:
        resp = req.get(
            'https://api.github.com/repos/tillo13/crab_travel/commits',
            params={'per_page': 50},
            headers={'Accept': 'application/vnd.github.v3+json'},
            timeout=10
        )
        if resp.status_code == 200:
            for c in resp.json():
                msg = c['commit']['message']
                lines = msg.split('\n')
                title = lines[0]
                body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''
                commits.append({
                    'sha': c['sha'][:7],
                    'sha_full': c['sha'],
                    'title': title,
                    'body': body,
                    'author': c['commit']['author']['name'],
                    'date': c['commit']['author']['date'],
                    'url': c['html_url'],
                    'avatar': c['author']['avatar_url'] if c.get('author') else None,
                })
    except Exception:
        pass
    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return render_template('admin_changelog.html', active_page='admin', commits=commits, now_str=now_str)


@app.route('/admin/speed')
@login_required
def admin_speed():
    from utilities.admin_utils import is_admin
    import json as json_lib
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, tested_by, results, slowest_page, slowest_time, all_ok, tested_at FROM crab.speed_test_runs ORDER BY tested_at DESC LIMIT 30")
    history = cur.fetchall()
    cur.close()
    conn.close()
    for run in history:
        if isinstance(run['results'], str):
            run['results'] = json_lib.loads(run['results'])
    latest = history[0] if history else None
    return render_template('admin_speed.html', active_page='admin', latest=latest, history=history)


@app.route('/api/admin/speed-test', methods=['POST'])
@api_auth_required
def api_admin_speed_test():
    from utilities.admin_utils import is_admin, get_admin_dashboard_data
    import time as time_mod
    import json as json_lib
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403

    THRESHOLD = 3.0
    user_id = session['user']['id']
    tests = [
        ('Home /', 'index', lambda: render_template('index.html', active_page='home')),
        ('Dashboard', 'get_plans_for_user', lambda: get_plans_for_user(user_id)),
        ('Profile', 'get_user_profile', lambda: get_user_profile(user_id)),
        ('Admin', 'get_admin_dashboard_data', lambda: get_admin_dashboard_data()),
    ]
    # Add plan-specific tests if user has plans
    try:
        plans = get_plans_for_user(user_id)
        if plans:
            plan_id = plans[0]['plan_id']
            from utilities.postgres_utils import (
                get_plan_blackouts, get_plan_tentative_dates,
                get_vote_tallies as _gvt, get_user_votes as _guv,
                upsert_vote as _uv, delete_vote as _dv, clear_rank_from_others as _crfo,
                get_destination_suggestions as _gds,
            )
            tests.append(('Plan page', 'get_plan_by_id', lambda: get_plan_by_id(str(plan_id))))
            tests.append(('Blackouts', 'get_plan_blackouts', lambda: get_plan_blackouts(plan_id)))
            tests.append(('Tentative dates', 'get_plan_tentative_dates', lambda: get_plan_tentative_dates(plan_id)))
            tests.append(('Vote tallies', 'get_vote_tallies', lambda: _gvt(plan_id, 'destination')))
            tests.append(('My votes', 'get_user_votes', lambda: _guv(plan_id, user_id)))
            tests.append(('Destinations', 'get_destination_suggestions', lambda: _gds(plan_id)))

            # Round-trip rank vote: write, read, clean up
            def _rank_roundtrip():
                test_dest = '__smoke_test_dest__'
                _uv(plan_id, user_id, 'destination', test_dest, 1)
                v = _guv(plan_id, user_id)
                assert v.get(f'destination:{test_dest}') == 1
                _dv(plan_id, user_id, 'destination', test_dest)
                return True
            tests.append(('Rank vote roundtrip', 'upsert+read+delete', _rank_roundtrip))
    except Exception:
        pass

    results = []
    for label, func_name, fn in tests:
        start = time_mod.time()
        try:
            fn()
            elapsed = round(time_mod.time() - start, 3)
            status = 'ok'
        except Exception as e:
            elapsed = round(time_mod.time() - start, 3)
            status = str(e)[:100]
        results.append({'page': label, 'function': func_name, 'time_s': elapsed, 'status': status})

    results.sort(key=lambda x: x['time_s'], reverse=True)
    slowest = results[0] if results else None
    all_ok = all(r['status'] == 'ok' and r['time_s'] < THRESHOLD for r in results)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crab.speed_test_runs (tested_by, results, slowest_page, slowest_time, all_ok) VALUES (%s, %s, %s, %s, %s)",
        (real_uid, json_lib.dumps(results), slowest['page'] if slowest else None, slowest['time_s'] if slowest else 0, all_ok)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({'success': True, 'results': results, 'threshold_s': THRESHOLD, 'all_ok': all_ok})


# ── Bot Testing (Crab Crawlers) — public + admin endpoints ───

@app.route('/live')
def live_page():
    """Public page — anyone can watch the crabs crawl.
    Pre-fetches initial data so page renders instantly (no blank flash)."""
    import json as _json
    try:
        from utilities.postgres_utils import get_bot_runs, get_bot_events
        runs = get_bot_runs(limit=50)
        for r in runs:
            for k in ('started_at', 'finished_at'):
                if r.get(k):
                    r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
            if r.get('plan_id'):
                r['plan_id'] = str(r['plan_id'])
            if r.get('run_id'):
                r['run_id'] = str(r['run_id'])
            summary = r.get('summary') or {}
            r['trip_title'] = summary.get('title', '')
            r['trip_destinations'] = summary.get('destinations', [])
            r['trip_group_size'] = summary.get('group_size', 0)
            r['trip_vibe'] = summary.get('vibe', '')
            r['invite_token'] = summary.get('invite_token', '')
        events = []
        if runs:
            active_runs = [r for r in runs if r['status'] == 'running']
            target_runs = active_runs if active_runs else runs[:3]
            for run in target_runs:
                run_events = get_bot_events(run['run_id'], limit=50)
                for e in run_events:
                    if e.get('created_at'):
                        e['created_at'] = e['created_at'].isoformat() if hasattr(e['created_at'], 'isoformat') else str(e['created_at'])
                    if e.get('run_id'):
                        e['run_id'] = str(e['run_id'])
                events.extend(run_events)
            events.sort(key=lambda e: e.get('created_at', ''), reverse=True)
            events = events[:200]
        initial_data = _json.dumps({'runs': runs, 'events': events})
    except Exception:
        initial_data = None
    return render_template('admin_bots.html', active_page='live', initial_data=initial_data)


@app.route('/admin/bots')
@login_required
def admin_bots():
    from utilities.admin_utils import is_admin
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    return render_template('admin_bots.html', active_page='admin', is_admin=True)


@app.route('/api/live/status')
def api_live_status():
    """Public endpoint — bot run status for the /live page."""
    from utilities.postgres_utils import get_bot_runs, get_bot_events

    runs = get_bot_runs(limit=50)
    # Serialize datetimes + extract summary info
    for r in runs:
        for k in ('started_at', 'finished_at'):
            if r.get(k):
                r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
        if r.get('plan_id'):
            r['plan_id'] = str(r['plan_id'])
        if r.get('run_id'):
            r['run_id'] = str(r['run_id'])
        # Extract trip info from summary for the departures board
        summary = r.get('summary') or {}
        r['trip_title'] = summary.get('title', '')
        r['trip_destinations'] = summary.get('destinations', [])
        r['trip_group_size'] = summary.get('group_size', 0)
        r['trip_vibe'] = summary.get('vibe', '')
        r['invite_token'] = summary.get('invite_token', '')

    # Get events — prefer active run, fall back to most recent runs
    events = []
    if runs:
        active_runs = [r for r in runs if r['status'] == 'running']
        target_runs = active_runs if active_runs else runs[:3]
        for run in target_runs:
            run_events = get_bot_events(run['run_id'], limit=50)
            for e in run_events:
                if e.get('created_at'):
                    e['created_at'] = e['created_at'].isoformat() if hasattr(e['created_at'], 'isoformat') else str(e['created_at'])
                if e.get('run_id'):
                    e['run_id'] = str(e['run_id'])
            events.extend(run_events)
        # Sort by created_at descending, limit to 200
        events.sort(key=lambda e: e.get('created_at', ''), reverse=True)
        events = events[:200]

    return jsonify({'success': True, 'data': {'runs': runs, 'events': events}})


@app.route('/api/admin/bots/run', methods=['POST'])
@api_auth_required
def api_admin_bots_run():
    from utilities.admin_utils import is_admin
    from utilities.postgres_utils import get_bot_runs
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403

    # Check no run is already active
    runs = get_bot_runs(limit=1)
    if runs and runs[0]['status'] == 'running':
        return jsonify({'error': 'A bot run is already in progress', 'run_id': str(runs[0]['run_id'])}), 409

    data = request.get_json() or {}
    mode = data.get('mode', 'quick')
    if mode not in ('full', 'quick'):
        mode = 'quick'

    import subprocess
    cwd = '/app' if os.path.exists('/app') else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flag = '--full' if mode == 'full' else '--quick'
    subprocess.Popen(
        ['python3', 'dev/trip_bots.py', flag],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info(f"🦀 Bot run launched: {mode} mode by {session['user']['email']}")
    return jsonify({'success': True, 'mode': mode})


@app.route('/api/admin/bots/stop', methods=['POST'])
@api_auth_required
def api_admin_bots_stop():
    from utilities.admin_utils import is_admin
    from utilities.postgres_utils import get_bot_runs, update_bot_run
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403

    runs = get_bot_runs(limit=1)
    if not runs or runs[0]['status'] != 'running':
        return jsonify({'error': 'No active run to stop'}), 400

    update_bot_run(str(runs[0]['run_id']), status='stopped')
    logger.info(f"🛑 Bot run stopped by {session['user']['email']}")
    return jsonify({'success': True})


@app.route('/api/admin/bots/cleanup', methods=['POST'])
@api_auth_required
def api_admin_bots_cleanup():
    from utilities.admin_utils import is_admin
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM crab.plans WHERE title LIKE '[BOT]%%'")
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"🧹 Bot cleanup: deleted {deleted} bot plans")
    return jsonify({'success': True, 'deleted': deleted})


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


@app.route('/notifications/off/<member_token>')
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


@app.route('/api/profile', methods=['POST'])
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


@app.route('/api/airport/resolve')
def api_resolve_airport():
    """Live-resolve freeform text to nearest airport."""
    from utilities.airport_utils import resolve_airport
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'result': None})
    result = resolve_airport(q)
    return jsonify({'result': result})


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
            watches_data.append({
                'pk_id': w['pk_id'], 'member_id': w['member_id'],
                'member_name': w['member_name'], 'watch_type': w['watch_type'],
                'origin': w.get('origin'), 'destination': w['destination'],
                'status': w['status'],
                'best_price': float(w['best_price_usd']) if w.get('best_price_usd') else None,
                'last_price': float(w['last_price_usd']) if w.get('last_price_usd') else None,
                'deep_link': w.get('deep_link'),
                'last_checked': w['last_checked_at'].isoformat() if w.get('last_checked_at') else None,
                'history': [{'price': float(h['price_usd']), 'at': h['observed_at'].isoformat()} for h in history],
            })
        watches_json = json.dumps(watches_data, default=_default_ser)

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


@app.route('/api/plan/<plan_id>/calendar')
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


@app.route('/api/plan/<plan_id>/approve-suggestion', methods=['POST'])
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
        # Auto-create per-member price watches in background
        from utilities.watch_engine import create_watches_for_plan
        threading.Thread(target=create_watches_for_plan, args=(plan_id,), daemon=True).start()
        return jsonify({'success': True})
    return jsonify({'error': 'Lock failed'}), 500


# ── Watch routes ────────────────────────────────────────────

@app.route('/api/plan/<plan_id>/watches')
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


@app.route('/api/plan/<plan_id>/watches/<int:watch_id>/status', methods=['POST'])
@api_auth_required
def api_update_watch_status(plan_id, watch_id):
    """Update watch status (mark as booked, paused, or active)."""
    from utilities.postgres_utils import update_watch_status
    data = request.get_json()
    new_status = data.get('status') if data else None
    if new_status not in ('active', 'paused', 'booked'):
        return jsonify({'error': 'Invalid status'}), 400
    success = update_watch_status(watch_id, new_status)
    return jsonify({'success': success})


@app.route('/api/plan/<plan_id>/watches/<int:watch_id>/history')
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


@app.route('/tasks/crawl')
def task_crawl():
    """Cron job — run one random bot trip (Crab Crawlers)."""
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    try:
        import subprocess
        cwd = '/app' if os.path.exists('/app') else os.path.dirname(os.path.abspath(__file__))
        # Run one quick random trip (no AI research to keep it fast + cheap)
        subprocess.Popen(
            ['python3', '-c',
             'import sys; sys.path.insert(0,"."); '
             'from dev.trip_bots import build_random_trip; '
             'from utilities.google_auth_utils import get_secret; '
             'build_random_trip("https://crab.travel", get_secret("CRAB_BOT_SECRET"))'],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Prune old bot plans (keep last 100 — the more the merrier)
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM crab.plans WHERE plan_id IN (
                    SELECT plan_id FROM crab.plans
                    WHERE title LIKE '[BOT]%%'
                    ORDER BY created_at DESC
                    OFFSET 100
                )
            """)
            pruned = cur.rowcount
            if pruned:
                logger.info(f"🧹 Pruned {pruned} old bot plans")
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass
        logger.info("🦀 Crab Crawl cron triggered — random trip starting")
        return jsonify({'success': True, 'message': 'Crawl started'})
    except Exception as e:
        logger.error(f"❌ Crawl cron failed: {e}")
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


@app.route('/tasks/check-watches')
def task_check_watches():
    """Cron job — check all active member watches for price changes."""
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    try:
        from utilities.watch_engine import check_all_watches
        summary = check_all_watches()
        logger.info(f"✅ /tasks/check-watches complete: {summary}")
        return jsonify({'success': True, **summary})
    except Exception as e:
        logger.error(f"❌ check-watches failed: {e}")
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

    # Send notifications to members (async, don't block response)
    try:
        from utilities.sms_utils import notify_plan_members
        import threading
        threading.Thread(
            target=notify_plan_members,
            args=(plan_id, display_name, content, user['id']),
            kwargs={'message_id': msg['message_id']},
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning(f"Notification dispatch failed: {e}")

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
