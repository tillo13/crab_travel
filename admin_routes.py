"""
Admin panel routes
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

bp = Blueprint('admin_routes', __name__)


@bp.route('/admin')
@login_required
def admin_panel():
    from utilities.admin_utils import is_admin, get_admin_dashboard_data
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    data = get_admin_dashboard_data()
    data['mimic_active'] = '_real_uid' in session
    return render_template('admin.html', active_page='admin', **data)


@bp.route('/api/admin/users')
@api_auth_required
def api_admin_users():
    from utilities.admin_utils import is_admin, get_admin_users
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403
    result = get_admin_users(
        search=request.args.get('search'),
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 50, type=int),
        sort_by=request.args.get('sort', 'created_at'),
        sort_dir=request.args.get('dir', 'desc'),
    )
    for u in result['users']:
        for k in ('created_at', 'updated_at'):
            if u.get(k):
                u[k] = u[k].isoformat()
    return jsonify(result)


@bp.route('/api/admin/plans')
@api_auth_required
def api_admin_plans():
    from utilities.admin_utils import is_admin, get_admin_plans
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return jsonify({'error': 'Admin only'}), 403
    result = get_admin_plans(
        search=request.args.get('search'),
        status=request.args.get('status'),
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 50, type=int),
        sort_by=request.args.get('sort', 'created_at'),
        sort_dir=request.args.get('dir', 'desc'),
    )
    for p in result['plans']:
        if p.get('created_at'):
            p['created_at'] = p['created_at'].isoformat()
    return jsonify(result)


@bp.route('/admin/mimic', methods=['GET', 'POST'])
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


@bp.route('/api/admin/test-email', methods=['POST'])
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


@bp.route('/api/admin/test-sms', methods=['POST'])
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


@bp.route('/api/bot/login', methods=['POST'])
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


@bp.route('/api/admin/smoke-test', methods=['POST'])
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


@bp.route('/admin/changelog')
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


@bp.route('/admin/ops')
@login_required
def admin_ops():
    from utilities.admin_utils import is_admin, get_ops_data
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    data = get_ops_data()
    return render_template('admin_ops.html', active_page='admin', **data)


@bp.route('/admin/speed')
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


@bp.route('/api/admin/speed-test', methods=['POST'])
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


@bp.route('/live')
def live_page():
    """Public page — anyone can watch the crabs crawl.
    Pre-fetches initial data so page renders instantly (no blank flash).
    Uses a SINGLE db connection to avoid pool exhaustion."""
    import json as _json
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT * FROM crab.bot_runs ORDER BY started_at DESC LIMIT 50")
        runs = [dict(r) for r in cur.fetchall()]

        _plan_ids = [str(r['plan_id']) for r in runs if r.get('plan_id')]
        _plan_statuses = {}
        if _plan_ids:
            cur.execute("SELECT plan_id, status FROM crab.plans WHERE plan_id = ANY(%s::uuid[])", (_plan_ids,))
            _plan_statuses = {str(r['plan_id']): r['status'] for r in cur.fetchall()}

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
            r['plan_status'] = _plan_statuses.get(r.get('plan_id'), None)

        events = []
        if runs:
            active_runs = [r for r in runs if r['status'] == 'running']
            target_runs = active_runs[:3] if active_runs else runs[:3]
            target_ids = [r['run_id'] for r in target_runs]
            if target_ids:
                cur.execute("""
                    SELECT * FROM crab.bot_events
                    WHERE run_id = ANY(%s::uuid[])
                    ORDER BY event_id DESC LIMIT 200
                """, (target_ids,))
                events = [dict(e) for e in cur.fetchall()]
                for e in events:
                    if e.get('created_at'):
                        e['created_at'] = e['created_at'].isoformat() if hasattr(e['created_at'], 'isoformat') else str(e['created_at'])
                    if e.get('run_id'):
                        e['run_id'] = str(e['run_id'])

        cur.close()
        conn.close()
        initial_data = _json.dumps({'runs': runs, 'events': events})
    except Exception:
        initial_data = None
    return render_template('admin_bots.html', active_page='live', initial_data=initial_data)


@bp.route('/admin/bots')
@login_required
def admin_bots():
    from utilities.admin_utils import is_admin
    real_uid = session.get('_real_uid') or session['user']['id']
    if not is_admin(real_uid):
        return redirect('/dashboard')
    return render_template('admin_bots.html', active_page='admin', is_admin=True)


@bp.route('/api/live/status')
def api_live_status():
    """Public endpoint — bot run status for the /live page.
    Uses a SINGLE db connection to avoid pool exhaustion from 3-sec polling."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1) Bot runs
        cur.execute("""
            SELECT * FROM crab.bot_runs ORDER BY started_at DESC LIMIT 50
        """)
        runs = [dict(r) for r in cur.fetchall()]

        # 2) Plan statuses (single query, same connection)
        plan_ids = [str(r['plan_id']) for r in runs if r.get('plan_id')]
        plan_statuses = {}
        if plan_ids:
            cur.execute("SELECT plan_id, status FROM crab.plans WHERE plan_id = ANY(%s::uuid[])", (plan_ids,))
            plan_statuses = {str(r['plan_id']): r['status'] for r in cur.fetchall()}

        # 3) Serialize + extract summary info
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
            r['plan_status'] = plan_statuses.get(r.get('plan_id'), None)

        # 4) Events (same connection)
        events = []
        if runs:
            active_runs = [r for r in runs if r['status'] == 'running']
            target_runs = active_runs[:3] if active_runs else runs[:3]
            target_ids = [r['run_id'] for r in target_runs]
            if target_ids:
                cur.execute("""
                    SELECT * FROM crab.bot_events
                    WHERE run_id = ANY(%s::uuid[])
                    ORDER BY event_id DESC LIMIT 200
                """, (target_ids,))
                events = [dict(e) for e in cur.fetchall()]
                for e in events:
                    if e.get('created_at'):
                        e['created_at'] = e['created_at'].isoformat() if hasattr(e['created_at'], 'isoformat') else str(e['created_at'])
                    if e.get('run_id'):
                        e['run_id'] = str(e['run_id'])

        cur.close()
        conn.close()
        return jsonify({'success': True, 'data': {'runs': runs, 'events': events}})
    except Exception as e:
        logger.error(f"Live status API failed: {e}")
        return jsonify({'success': False, 'error': 'temporarily unavailable'}), 503


@bp.route('/api/admin/bots/run', methods=['POST'])
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


@bp.route('/api/admin/bots/stop', methods=['POST'])
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


@bp.route('/api/admin/bots/cleanup', methods=['POST'])
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
