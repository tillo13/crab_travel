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

from route_helpers import set_auth_enabled, login_required, api_auth_required
set_auth_enabled(AUTH_ENABLED)


@app.before_request
def force_canonical_host():
    """301 www.crab.travel → crab.travel so Google sees one canonical host.
    Without this, both hostnames serve 200 and pages get marked 'Duplicate without user-selected canonical'."""
    host = request.host.split(':')[0]
    if host == 'www.crab.travel':
        return redirect(f'https://crab.travel{request.full_path}', code=301)


@app.before_request
def check_apikey_auth():
    """Allow ?apikey=SECRET&user_id=X to bypass OAuth (Playwright/admin testing).
    Sets session inline — no redirect needed, the request proceeds with auth."""
    if 'apikey' not in request.args:
        return
    if session.get('user'):
        return  # Already authed
    apikey = request.args.get('apikey')
    try:
        expected = get_secret('CRAB_TEST_APIKEY', project_id='crab-travel')
    except Exception as e:
        logger.warning(f"apikey auth: secret lookup failed: {e}")
        return
    if not expected or apikey != expected:
        logger.warning(f"apikey auth: key mismatch (expected={expected is not None})")
        return
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return
    try:
        from utilities.admin_utils import _get_user_session_data
        user_data = _get_user_session_data(user_id)
    except Exception as e:
        logger.warning(f"apikey auth: user lookup failed: {e}")
        return
    if not user_data:
        logger.warning(f"apikey auth: user {user_id} not found")
        return
    session.permanent = True
    session['user'] = user_data
    session.modified = True
    logger.info(f"apikey auth: logged in as user {user_id}")


@app.before_request
def check_demo_exit():
    """When a demo viewer navigates to a non-demo page, restore their real session."""
    if '_demo_stashed_user' not in session:
        return
    # Stay in demo mode for bot trips, invite pages, summary pages, static, and API calls
    path = request.path
    if (path.startswith('/to/') or path.startswith('/plan/') or
        path.startswith('/api/') or path.startswith('/static/') or
        path.startswith('/demo') or path.startswith('/_ah/')):
        return
    # Leaving demo — restore real user
    real_user = session.pop('_demo_stashed_user', None)
    session.pop('_demo_viewer', None)
    if real_user:
        session['user'] = real_user
    else:
        session.pop('user', None)
    session.modified = True
    logger.info(f"Demo exit: restored real user on {path}")






# ── Database init ────────────────────────────────────────────

try:
    init_database()
except Exception as e:
    logger.warning(f"⚠️ Database init deferred: {e}")

try:
    from utilities.shorturl_utils import ensure_table_exists as _ensure_short_urls_table
    _ensure_short_urls_table()
except Exception as e:
    logger.warning(f"⚠️ crab.short_urls init deferred: {e}")

try:
    from utilities.timeshare_schema import init_timeshare_schema
    init_timeshare_schema()
except Exception as e:
    logger.warning(f"⚠️ crab.timeshare_* schema init deferred: {e}")


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


@app.before_request
def force_canonical_host():
    """301 www.crab.travel → crab.travel so Google sees one canonical host.
    Without this, both hostnames serve 200 and pages get marked 'Duplicate without user-selected canonical'."""
    host = request.host.split(':')[0]
    if host == 'www.crab.travel':
        return redirect(f'https://crab.travel{request.full_path}', code=301)


@app.before_request
def check_apikey_auth():
    """Allow ?apikey=SECRET&user_id=X to bypass OAuth (Playwright/admin testing).
    Sets session inline — no redirect needed, the request proceeds with auth."""
    if 'apikey' not in request.args:
        return
    if session.get('user'):
        return  # Already authed
    apikey = request.args.get('apikey')
    try:
        expected = get_secret('CRAB_TEST_APIKEY', project_id='crab-travel')
    except Exception as e:
        logger.warning(f"apikey auth: secret lookup failed: {e}")
        return
    if not expected or apikey != expected:
        logger.warning(f"apikey auth: key mismatch (expected={expected is not None})")
        return
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return
    try:
        from utilities.admin_utils import _get_user_session_data
        user_data = _get_user_session_data(user_id)
    except Exception as e:
        logger.warning(f"apikey auth: user lookup failed: {e}")
        return
    if not user_data:
        logger.warning(f"apikey auth: user {user_id} not found")
        return
    session.permanent = True
    session['user'] = user_data
    session.modified = True
    logger.info(f"apikey auth: logged in as user {user_id}")


@app.before_request
def check_demo_exit():
    """When a demo viewer navigates to a non-demo page, restore their real session."""
    if '_demo_stashed_user' not in session:
        return
    # Stay in demo mode for bot trips, invite pages, summary pages, static, and API calls
    path = request.path
    if (path.startswith('/to/') or path.startswith('/plan/') or
        path.startswith('/api/') or path.startswith('/static/') or
        path.startswith('/demo') or path.startswith('/_ah/')):
        return
    # Leaving demo — restore real user
    real_user = session.pop('_demo_stashed_user', None)
    session.pop('_demo_viewer', None)
    if real_user:
        session['user'] = real_user
    else:
        session.pop('user', None)
    session.modified = True
    logger.info(f"Demo exit: restored real user on {path}")


@app.before_request
def refresh_admin_flag():
    user = session.get('user')
    if user and 'id' in user and 'user_is_admin' not in session:
        from utilities.admin_utils import is_admin as check_admin
        session['user_is_admin'] = check_admin(user['id'])


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html', active_page=None), 404


@app.route('/sitemap.xml')
def sitemap():
    host = request.host_url.rstrip('/')
    # Only public-facing content pages — no auth, admin, API, internal, or utility routes
    public_pages = {
        '/':                1.0,
        '/about':           0.8,
        '/live':            0.7,
        '/demo':            0.7,
        '/roadmap':         0.6,
        '/contact':         0.6,
        '/crab-animations': 0.5,
        '/privacy':         0.3,
        '/terms':           0.3,
    }
    lastmod = '2026-04-04'
    urls = []
    for path, priority in sorted(public_pages.items()):
        urls.append(
            f'  <url>\n'
            f'    <loc>{host}{path}</loc>\n'
            f'    <lastmod>{lastmod}</lastmod>\n'
            f'    <priority>{priority}</priority>\n'
            f'  </url>'
        )
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + '\n'.join(urls) + '\n</urlset>'
    return Response(xml, mimetype='application/xml')


@app.route('/robots.txt')
def robots():
    host = request.host_url.rstrip('/')
    content = (
        'User-agent: *\n'
        'Allow: /\n'
        'Disallow: /timeshare/g/\n'
        f'Sitemap: {host}/sitemap.xml\n'
        f'Feed: {host}/feed.xml\n'
    )
    return Response(content, mimetype='text/plain')


@app.route('/b4c9ebbc8faa4d7b8b2b8104b6511fee.txt')
def indexnow_key():
    return Response('b4c9ebbc8faa4d7b8b2b8104b6511fee', mimetype='text/plain')


@app.route('/feed.xml')
def atom_feed():
    """Atom feed of roadmap and feature updates for search engine discovery."""
    from datetime import datetime
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>crab.travel</title>
  <subtitle>Group trip planning without the chaos. Roadmap and feature updates.</subtitle>
  <link href="https://crab.travel/"/>
  <link href="https://crab.travel/feed.xml" rel="self"/>
  <id>https://crab.travel/</id>
  <updated>{now}</updated>
  <entry>
    <title>Interactive Demo Mode — shipped</title>
    <link href="https://crab.travel/roadmap"/>
    <id>https://crab.travel/roadmap#demo-mode</id>
    <updated>2026-03-20T00:00:00Z</updated>
    <summary>Experience the full platform as Judy Tunaboat without signing up. Stage switcher shows Voting, Planning, and Booked phases of real demo trips.</summary>
  </entry>
  <entry>
    <title>Travel Search &amp; Deals — shipped</title>
    <link href="https://crab.travel/roadmap"/>
    <id>https://crab.travel/roadmap#travel-search</id>
    <updated>2026-03-15T00:00:00Z</updated>
    <summary>Four search adapters (Duffel, LiteAPI, Viator, Travelpayouts) running in parallel with real-time streaming results and cross-provider deduplication.</summary>
  </entry>
  <entry>
    <title>CrabAI Destination Research — shipped</title>
    <link href="https://crab.travel/roadmap"/>
    <id>https://crab.travel/roadmap#crabai-research</id>
    <updated>2026-03-10T00:00:00Z</updated>
    <summary>AI-generated destination cards with stays, activities, food, and local events. Group vibes filter and compatibility scoring based on collective preferences.</summary>
  </entry>
  <entry>
    <title>Voting &amp; Availability — shipped</title>
    <link href="https://crab.travel/roadmap"/>
    <id>https://crab.travel/roadmap#voting</id>
    <updated>2026-03-05T00:00:00Z</updated>
    <summary>Rank-order destination voting with live tallies and visual group availability calendar with three-tier date preferences.</summary>
  </entry>
  <entry>
    <title>Trip Creation &amp; Invites — shipped</title>
    <link href="https://crab.travel/roadmap"/>
    <id>https://crab.travel/roadmap#trip-creation</id>
    <updated>2026-03-01T00:00:00Z</updated>
    <summary>Core trip loop: create a trip, share one link, everyone joins and fills out preferences. No app download or account required for members.</summary>
  </entry>
  <entry>
    <title>Live Trip Bots</title>
    <link href="https://crab.travel/live"/>
    <id>https://crab.travel/live</id>
    <updated>2026-03-25T00:00:00Z</updated>
    <summary>Watch AI-powered bots plan trips in real time. Live demo of the full group planning experience with destination research and group chat.</summary>
  </entry>
  <entry>
    <title>About crab.travel</title>
    <link href="https://crab.travel/about"/>
    <id>https://crab.travel/about</id>
    <updated>2026-03-01T00:00:00Z</updated>
    <summary>The story behind crab.travel: why group trip planning is broken and how we are fixing it with AI and collaborative tools.</summary>
  </entry>
</feed>'''
    return Response(xml, mimetype='application/atom+xml')


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


@app.route('/sms')
def sms_info():
    return render_template('sms.html', active_page=None)


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
        data = request.get_json(silent=True) or {}
        email = (data.get('email') or '').strip()
        message = (data.get('message') or '').strip()

        from utilities.spam_guard import check_spam
        reason = check_spam(
            data, request.remote_addr, fields=['email', 'message'],
            origin=request.headers.get('Origin') or request.headers.get('Referer'),
            user_agent=request.headers.get('User-Agent'),
            expected_hosts=['crab.travel', 'www.crab.travel'],
        )
        if reason:
            logger.warning(f"Spam blocked: {reason} from {request.remote_addr}")
            return jsonify({'error': 'Invalid submission'}), 400

        if not email or not message:
            return jsonify({'error': 'Email and message are required'}), 400

        from utilities.gmail_utils import send_simple_email
        subject = f"[crab] Contact: {email}"
        body = f"""New contact from crab.travel:

From: {email}
IP: {request.remote_addr}
User Agent: {request.headers.get('User-Agent', 'Unknown')}

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


@app.route('/plan/<plan_id>/summary')
def trip_summary(plan_id):
    from utilities.postgres_utils import get_trip_summary, get_itinerary_items, get_expenses
    plan = get_plan_by_id(plan_id)
    if not plan:
        return render_template('404.html'), 404
    # Auto-login as Judy on bot trips
    _auto_login_demo_viewer(plan)
    is_bot_trip = plan.get('title', '').startswith('[BOT]')
    if not is_bot_trip:
        if AUTH_ENABLED and 'user' not in session:
            return redirect(url_for('auth_routes.login'))
    summary = get_trip_summary(plan_id)
    itinerary = get_itinerary_items(plan_id)
    expenses = get_expenses(plan_id)

    # Compute "who owes who" balances from expenses
    balances = []
    if expenses and summary.get('member_count', 0) > 0:
        member_count = summary['member_count']
        # Track how much each person paid vs their fair share
        paid_by = {}  # member_name -> total paid
        for exp in expenses:
            name = (exp.get('paid_by_name') or '').replace('[BOT] ', '')
            paid_by[name] = paid_by.get(name, 0) + float(exp.get('amount', 0))
        total_expenses = sum(paid_by.values())
        fair_share = total_expenses / member_count

        # People who paid more than their share are owed money
        creditors = []  # (name, amount_owed_to_them)
        debtors = []    # (name, amount_they_owe)
        for name, paid in paid_by.items():
            diff = paid - fair_share
            if diff > 1:  # they overpaid — they're owed money
                creditors.append([name, diff])
            elif diff < -1:  # they underpaid — they owe
                debtors.append([name, -diff])
        # Everyone who didn't pay anything owes their fair share
        all_member_names = set()
        for mid, m in summary.get('members', {}).items():
            clean = m['name'].replace('[BOT] ', '')
            all_member_names.add(clean)
        for name in all_member_names:
            if name not in paid_by:
                debtors.append([name, fair_share])

        # Match debtors to creditors (simplified: greedy)
        creditors.sort(key=lambda x: -x[1])
        debtors.sort(key=lambda x: -x[1])
        for debtor_name, debt in debtors:
            remaining = debt
            for cred in creditors:
                if remaining <= 0.50:
                    break
                if cred[1] <= 0.50:
                    continue
                payment = min(remaining, cred[1])
                balances.append({
                    'from_name': debtor_name,
                    'to_name': cred[0],
                    'amount': round(payment, 0),
                })
                cred[1] -= payment
                remaining -= payment

    return render_template('trip_summary.html',
        plan=plan, summary=summary, itinerary=itinerary, expenses=expenses, balances=balances)


@app.route('/api/plan/<plan_id>/itinerary', methods=['POST'])
@api_auth_required
def api_add_itinerary_item(plan_id):
    from utilities.postgres_utils import insert_itinerary_item
    data = request.get_json()
    if not data or not data.get('title') or not data.get('scheduled_date'):
        return jsonify({'error': 'title and scheduled_date are required'}), 400
    item = insert_itinerary_item(
        plan_id=plan_id,
        title=data['title'],
        category=data.get('category', 'activity'),
        scheduled_date=data['scheduled_date'],
        scheduled_time=data.get('scheduled_time'),
        duration_minutes=data.get('duration_minutes'),
        location=data.get('location'),
        url=data.get('url'),
        notes=data.get('notes'),
        added_by=data.get('added_by'),
    )
    if item:
        # Convert non-serializable types
        for k, v in item.items():
            if hasattr(v, 'isoformat'):
                item[k] = v.isoformat()
        return jsonify({'success': True, 'item': item})
    return jsonify({'error': 'Failed to add item'}), 500


@app.route('/api/plan/<plan_id>/itinerary/<item_id>', methods=['DELETE'])
@api_auth_required
def api_delete_itinerary_item(plan_id, item_id):
    from utilities.postgres_utils import delete_itinerary_item
    success = delete_itinerary_item(item_id)
    return jsonify({'success': success})


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


@app.route('/_ah/warmup')
def warmup():
    """App Engine warmup handler — pre-test the DB pool so first real requests don't fail."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        logger.info("✅ Warmup: DB pool healthy")
        return 'ok', 200
    except Exception as e:
        logger.error(f"❌ Warmup: DB pool test failed: {e}")
        return f'pool error: {e}', 500


# ── Blueprint registration ───────────────────────────────────

from auth_routes import bp as auth_bp
from plan_routes import bp as plan_bp
from destinations_routes import bp as destinations_bp
from watches_routes import bp as watches_bp
from admin_routes import bp as admin_bp
from tasks_routes import bp as tasks_bp
from opencrab_routes import bp as opencrab_bp
from shorturl_routes import bp as shorturl_bp
from timeshare_routes import bp as timeshare_bp

app.register_blueprint(auth_bp)
app.register_blueprint(plan_bp)
app.register_blueprint(destinations_bp)
app.register_blueprint(watches_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(tasks_bp)
app.register_blueprint(opencrab_bp)
app.register_blueprint(shorturl_bp)
app.register_blueprint(timeshare_bp)


@app.context_processor
def inject_timeshare_groups():
    """Expose my_timeshare_groups to every template so base.html can render
    a Timeshare nav link when the user belongs to ≥1 group."""
    user = session.get('user') or {}
    if not user.get('id'):
        return {'my_timeshare_groups': []}
    try:
        from utilities.timeshare_access import get_user_timeshare_groups
        return {'my_timeshare_groups': get_user_timeshare_groups(user['id'])}
    except Exception:
        return {'my_timeshare_groups': []}

# ── Eagerly init kumori_free_llms at startup ─────────────────

try:
    from utilities import kumori_free_llms
    from utilities.postgres_utils import db_cursor
    from utilities.claude_utils import log_api_usage
    kumori_free_llms.init(
        app_name='crab_travel',
        get_secret_fn=get_secret,
        db_cursor_fn=db_cursor,
        log_api_usage_fn=log_api_usage,
    )
    logger.info('kumori_free_llms initialized on startup')
except Exception as e:
    logger.warning(f'kumori_free_llms init deferred: {e}')


# ── WSGI entry ───────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
