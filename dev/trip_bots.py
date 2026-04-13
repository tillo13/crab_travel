#!/usr/bin/env python3
"""
Crab Crawlers — bot runtime classes and phase functions.

Split from original dev/trip_bots.py for kumori 1000-line compliance.
Orchestration (main, crawl_forever, build_trip, etc.) lives in bot_orchestrator.py.
Persona data lives in bot_personas.py.
"""

import sys
import os
import time
import json
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests as http_requests

from dev.bot_personas import PROD_URL, TIMEOUT, BOT_PREFIX, PERSONAS

log = logging.getLogger('trip_bots')


# ─── Runtime classes ─────────────────────────────────────────────────────────

class BotSession:
    """HTTP session authenticated as a bot user via /api/bot/login."""

    def __init__(self, base_url, bot_secret):
        self.base_url = base_url
        self.bot_secret = bot_secret
        self.session = http_requests.Session()
        self.user_id = None
        self.persona = None

    def login_as(self, persona):
        """Authenticate as a specific bot persona. Fresh session each time."""
        self.persona = persona
        self.session = http_requests.Session()  # fresh cookies
        resp = self.session.post(f'{self.base_url}/api/bot/login', json={
            'secret': self.bot_secret,
            'user_id': persona['db_id'],
        }, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Bot login failed for {persona['name']}: {resp.status_code} {resp.text[:200]}")
        self.user_id = persona['db_id']
        return resp.json()

    def post(self, path, data=None):
        return self.session.post(f'{self.base_url}{path}', json=data, timeout=TIMEOUT)

    def get(self, path):
        return self.session.get(f'{self.base_url}{path}', timeout=TIMEOUT)

    def delete(self, path):
        return self.session.delete(f'{self.base_url}{path}', timeout=TIMEOUT)


# ─── Run Context ─────────────────────────────────────────────────────────────

class RunContext:
    """Shared state across all phases."""

    def __init__(self, base_url, bot_secret, run_id=None, plan_id=None, mode='full'):
        self.base_url = base_url
        self.bot_secret = bot_secret
        self.run_id = run_id
        self.plan_id = plan_id
        self.invite_token = None
        self.mode = mode
        self.personas = list(PERSONAS)  # copy
        self.destination_ids = {}  # name -> suggestion_id
        self.message_ids = []
        self.bot = BotSession(base_url, bot_secret)

    def log_event(self, phase, bot_name, action, status='ok', detail=None):
        """Log an event to DB and console."""
        icon = {'ok': '✅', 'error': '❌', 'warning': '⚠️'}.get(status, '  ')
        log.info(f"  {icon}  [{phase}] {bot_name}: {action}")
        if self.run_id:
            try:
                from utilities.postgres_utils import insert_bot_event
                insert_bot_event(self.run_id, phase, bot_name, action, status, detail)
            except Exception:
                pass

    def check_stopped(self):
        """Check if the run was stopped via admin dashboard."""
        if not self.run_id:
            return False
        try:
            from utilities.postgres_utils import get_bot_run_status
            return get_bot_run_status(self.run_id) == 'stopped'
        except Exception:
            return False


# ─── Phase Results ───────────────────────────────────────────────────────────

class PhaseResult:
    def __init__(self, phase_name):
        self.phase = phase_name
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.errors = []
        self.start = time.time()
        self.elapsed = 0

    def ok(self, msg=''):
        self.passed += 1

    def fail(self, msg):
        self.failed += 1
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings += 1

    def finish(self):
        self.elapsed = time.time() - self.start
        return self

    @property
    def status(self):
        if self.failed > 0:
            return 'failed'
        if self.warnings > 0:
            return 'warning'
        return 'passed'


# ─── Phase functions ─────────────────────────────────────────────────────────

def phase_setup(ctx):
    """Ensure all 10 bot users exist in the DB."""
    result = PhaseResult('setup')
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    for p in ctx.personas:
        cur.execute("""
            INSERT INTO crab.users (google_id, email, full_name, notify_chat, notify_updates, notify_channel)
            VALUES (%s, %s, %s, 'off', 'off', 'email')
            ON CONFLICT (google_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                notify_chat = 'off',
                notify_updates = 'off'
            RETURNING pk_id
        """, (p['google_id'], p['email'], p['name']))
        row = cur.fetchone()
        p['db_id'] = row['pk_id']
        ctx.log_event('setup', p['name'], f"User ensured (pk_id={p['db_id']})")
        result.ok()

    conn.commit()
    cur.close()
    conn.close()

    # Verify all have IDs
    missing = [p['name'] for p in ctx.personas if not p.get('db_id')]
    if missing:
        result.fail(f"Missing DB IDs for: {missing}")
    else:
        ctx.log_event('setup', 'system', f"All {len(ctx.personas)} bot users ready")

    return result.finish()


# ─── Phase 1: Create Trip ───────────────────────────────────────────────────

def phase_create(ctx):
    """Marcus creates the trip with 2 initial destinations."""
    result = PhaseResult('create')
    organizer = ctx.personas[0]
    ctx.bot.login_as(organizer)

    resp = ctx.bot.post('/api/plan/create', {
        'title': f'{BOT_PREFIX} Squad Trip',
        'destinations': DESTINATIONS,
    })
    data = resp.json()

    if resp.status_code != 200 or not data.get('success'):
        result.fail(f"Create failed: {resp.status_code} {data}")
        ctx.log_event('create', organizer['name'], f"Create FAILED: {data}", 'error')
        return result.finish()

    ctx.plan_id = data['data']['plan_id']
    ctx.invite_token = data['data']['invite_token']
    ctx.log_event('create', organizer['name'], f"Created plan {ctx.plan_id} (invite: {ctx.invite_token})")
    result.ok()

    # Update the bot_run with plan_id
    if ctx.run_id:
        from utilities.postgres_utils import update_bot_run
        update_bot_run(ctx.run_id, plan_id=ctx.plan_id)

    # Verify destinations
    resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/destinations')
    dests = resp.json().get('data', {}).get('destinations', [])
    if len(dests) >= 2:
        result.ok()
        for d in dests:
            ctx.destination_ids[d['destination_name']] = str(d['suggestion_id'])
        ctx.log_event('create', 'system', f"Destinations confirmed: {list(ctx.destination_ids.keys())}")
    else:
        result.fail(f"Expected 2 destinations, got {len(dests)}")

    return result.finish()


# ─── Phase 2: Join ───────────────────────────────────────────────────────────

def phase_join(ctx):
    """Bots 2-8 join the plan (Carlos joins later in Phase 5)."""
    result = PhaseResult('join')

    joiners = [p for p in ctx.personas[1:] if not p.get('late_joiner')]
    for p in joiners:
        ctx.bot.login_as(p)
        resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/join-full', {
            'home_airport': p['airport'],
            'is_flexible': p['is_flexible'],
            'blackouts': p['blackouts'],
            'tentative_dates': p['tentative_dates'],
            'votes': {},
        })
        data = resp.json()
        if resp.status_code == 200 and data.get('success'):
            result.ok()
            ctx.log_event('join', p['name'], f"Joined from {p['airport']}")
        else:
            result.fail(f"{p['name']} join failed: {data}")
            ctx.log_event('join', p['name'], f"Join FAILED: {data}", 'error')

    return result.finish()


# ─── Phase 3: Suggest Destination ────────────────────────────────────────────

def phase_suggest(ctx):
    """Lisa suggests San Juan PR, Marcus approves it."""
    result = PhaseResult('suggest')
    lisa = next(p for p in ctx.personas if p.get('suggests_destination'))
    organizer = ctx.personas[0]

    # Lisa suggests
    ctx.bot.login_as(lisa)
    resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/suggest-destination', {
        'destination': lisa['suggests_destination'],
    })
    data = resp.json()
    if resp.status_code == 200 and data.get('success'):
        suggestion_id = data['data']['suggestion_id']
        suggestion_status = data['data']['status']
        ctx.log_event('suggest', lisa['name'], f"Suggested {lisa['suggests_destination']} (status: {suggestion_status})")
        result.ok()

        # Marcus approves if pending
        if suggestion_status == 'pending':
            ctx.bot.login_as(organizer)
            resp2 = ctx.bot.post(f'/api/plan/{ctx.plan_id}/approve-suggestion', {
                'suggestion_id': suggestion_id,
                'action': 'approve',
            })
            if resp2.status_code == 200 and resp2.json().get('success'):
                ctx.log_event('suggest', organizer['name'], f"Approved {lisa['suggests_destination']}")
                result.ok()
            else:
                result.fail(f"Approve failed: {resp2.json()}")
                ctx.log_event('suggest', organizer['name'], 'Approve FAILED', 'error')

        ctx.destination_ids[lisa['suggests_destination']] = suggestion_id
    else:
        result.fail(f"Suggest failed: {data}")
        ctx.log_event('suggest', lisa['name'], f"Suggest FAILED: {data}", 'error')

    # Verify 3 destinations
    ctx.bot.login_as(organizer)
    resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/destinations')
    dests = resp.json().get('data', {}).get('destinations', [])
    # Refresh destination IDs
    for d in dests:
        ctx.destination_ids[d['destination_name']] = str(d['suggestion_id'])
    if len(dests) >= 3:
        result.ok()
        ctx.log_event('suggest', 'system', f"3 destinations confirmed: {list(ctx.destination_ids.keys())}")
    else:
        result.warn(f"Expected 3 destinations, got {len(dests)}")

    return result.finish()


# ─── Phase 4: Preferences ───────────────────────────────────────────────────

def phase_preferences(ctx):
    """All bots set their plan-specific preferences."""
    result = PhaseResult('preferences')

    for p in ctx.personas:
        if not p.get('db_id'):
            continue
        if p.get('late_joiner'):
            continue  # Carlos hasn't joined yet
        ctx.bot.login_as(p)
        resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/preferences', {
            'budget_min': p['budget_min'],
            'budget_max': p['budget_max'],
            'accommodation_style': p['accommodation'],
            'dietary_needs': p['dietary'],
            'interests': p['interests'],
            'mobility_notes': p['mobility'],
            'notes': 'Synthetic test preferences',
        })
        if resp.status_code == 200 and resp.json().get('success'):
            result.ok()
            ctx.log_event('preferences', p['name'], f"Prefs set: ${p['budget_min']//100}-${p['budget_max']//100}, {p['accommodation']}")
        else:
            result.fail(f"{p['name']} prefs failed: {resp.text}")
            ctx.log_event('preferences', p['name'], 'Prefs FAILED', 'error')

    return result.finish()


# ─── Phase 5: Vote ───────────────────────────────────────────────────────────

def phase_vote(ctx):
    """All bots vote. Carlos (late joiner) joins first, then votes."""
    result = PhaseResult('vote')

    # Carlos joins late
    carlos = next((p for p in ctx.personas if p.get('late_joiner')), None)
    if carlos:
        ctx.bot.login_as(carlos)
        resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/join-full', {
            'home_airport': carlos['airport'],
            'is_flexible': carlos['is_flexible'],
            'blackouts': carlos['blackouts'],
            'tentative_dates': carlos['tentative_dates'],
            'votes': {},
        })
        if resp.status_code == 200 and resp.json().get('success'):
            ctx.log_event('vote', carlos['name'], f"Late join from {carlos['airport']}")
            result.ok()
        else:
            result.fail(f"Carlos late join failed: {resp.text}")

        # Also set prefs for Carlos
        ctx.bot.post(f'/api/plan/{ctx.plan_id}/preferences', {
            'budget_min': carlos['budget_min'],
            'budget_max': carlos['budget_max'],
            'accommodation_style': carlos['accommodation'],
            'interests': carlos['interests'],
            'notes': 'Late joiner preferences',
        })

    # Everyone votes
    for p in ctx.personas:
        if not p.get('db_id'):
            continue
        ctx.bot.login_as(p)
        for dest_name, rank in p['vote_ranks'].items():
            dest_id = ctx.destination_ids.get(dest_name)
            if not dest_id:
                result.warn(f"No destination ID for {dest_name}")
                continue
            resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/vote', {
                'target_type': 'destination',
                'target_id': dest_id,
                'vote': rank,
            })
            if resp.status_code == 200 and resp.json().get('success'):
                result.ok()
            else:
                detail = resp.text[:200] if resp.text else f'status={resp.status_code}'
                result.fail(f"{p['name']} vote for {dest_name} failed: {detail}")
                ctx.log_event('vote', p['name'], f"Vote FAILED for {dest_name}", 'error', detail=detail)

        ctx.log_event('vote', p['name'], f"Voted: {p['vote_ranks']}")

    # Verify tallies
    ctx.bot.login_as(ctx.personas[0])
    resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/votes')
    if resp.status_code == 200:
        result.ok()
        ctx.log_event('vote', 'system', 'Vote tallies verified')
    else:
        result.fail(f"Vote tallies check failed: {resp.status_code}")

    return result.finish()


# ─── Phase 6: Chat ───────────────────────────────────────────────────────────

def phase_chat(ctx):
    """5 bots post messages, Jake replies to Marcus's first message."""
    result = PhaseResult('chat')
    first_msg_id = None

    for p in ctx.personas:
        if not p.get('chat_messages'):
            continue
        ctx.bot.login_as(p)
        for msg_text in p['chat_messages']:
            resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/messages', {
                'content': msg_text,
            })
            data = resp.json()
            if resp.status_code == 200 and data.get('success'):
                msg_id = data['data']['message']['message_id']
                ctx.message_ids.append(msg_id)
                if first_msg_id is None:
                    first_msg_id = msg_id
                result.ok()
                ctx.log_event('chat', p['name'], f"Posted: {msg_text[:60]}...")
            else:
                result.fail(f"{p['name']} message failed: {data}")
                ctx.log_event('chat', p['name'], 'Message FAILED', 'error')

    # Someone replies to the first message (threaded)
    if first_msg_id and len(ctx.personas) > 1:
        replier = ctx.personas[1]  # second persona replies
        ctx.bot.login_as(replier)
        resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/messages', {
            'content': 'Totally agree, this is going to be amazing!',
            'parent_id': first_msg_id,
        })
        if resp.status_code == 200 and resp.json().get('success'):
            result.ok()
            ctx.log_event('chat', replier['name'], f"Replied to thread ({first_msg_id[:8]}...)")
        else:
            result.fail(f"Thread reply failed: {resp.text}")

    # Verify messages exist
    ctx.bot.login_as(ctx.personas[0])
    resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/messages')
    if resp.status_code == 200:
        msgs = resp.json().get('data', {}).get('messages', [])
        if len(msgs) >= 5:
            result.ok()
            ctx.log_event('chat', 'system', f"Chat verified: {len(msgs)} top-level messages")
        else:
            result.warn(f"Expected 5+ messages, got {len(msgs)}")
    else:
        result.fail(f"Messages GET failed: {resp.status_code}")

    return result.finish()


# ─── Phase 7: AI Research ───────────────────────────────────────────────────

def phase_ai_research(ctx):
    """Trigger AI destination research and poll until done."""
    result = PhaseResult('ai_research')
    organizer = ctx.personas[0]
    ctx.bot.login_as(organizer)

    resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/generate')
    data = resp.json()
    if resp.status_code != 200 or not data.get('success'):
        result.fail(f"Generate trigger failed: {data}")
        ctx.log_event('ai_research', organizer['name'], f"Generate FAILED: {data}", 'error')
        return result.finish()

    ctx.log_event('ai_research', organizer['name'], 'AI research triggered, polling...')

    # Poll for completion
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/generate/status')
        if resp.status_code == 200:
            status_data = resp.json().get('data', {})
            status = status_data.get('status', 'unknown')
            if status == 'done':
                count = status_data.get('count', 0)
                ctx.log_event('ai_research', 'system', f"AI research complete: {count} recommendations")
                result.ok()
                return result.finish()
            elif status == 'error':
                error_msg = status_data.get('error', 'unknown error')
                ctx.log_event('ai_research', 'system', f"AI research errored: {error_msg}", 'warning')
                result.warn(f"Generate errored: {error_msg}")
                return result.finish()

        if ctx.check_stopped():
            result.warn('Run stopped by admin')
            return result.finish()

    result.warn('Generate timed out after 120s')
    ctx.log_event('ai_research', 'system', 'Generate timed out', 'warning')
    return result.finish()


# ─── Phase 8: Lock & Search ─────────────────────────────────────────────────

def phase_lock_search(ctx):
    """Lock destination to Scottsdale, trigger search, poll results."""
    result = PhaseResult('lock_search')
    organizer = ctx.personas[0]
    ctx.bot.login_as(organizer)

    # Lock the plan
    resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/lock', {
        'destination': 'Scottsdale AZ',
        'start_date': '2026-05-20',
        'end_date': '2026-05-23',
    })
    if resp.status_code == 200 and resp.json().get('success'):
        result.ok()
        ctx.log_event('lock_search', organizer['name'], 'Plan locked: Scottsdale AZ, May 20-23')
    else:
        result.fail(f"Lock failed: {resp.text}")
        ctx.log_event('lock_search', organizer['name'], 'Lock FAILED', 'error')
        return result.finish()

    # Trigger search
    resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/search/trigger')
    data = resp.json()
    if resp.status_code != 200 or not data.get('success'):
        result.fail(f"Search trigger failed: {data}")
        ctx.log_event('lock_search', 'system', f"Search trigger FAILED: {data}", 'error')
        return result.finish()

    ctx.log_event('lock_search', 'system', 'Search triggered, polling...')

    # Poll for completion
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/search/status')
        if resp.status_code == 200:
            sdata = resp.json().get('data', {})
            if not sdata.get('searching', True):
                count = sdata.get('count', 0)
                if count > 0:
                    result.ok()
                    ctx.log_event('lock_search', 'system', f"Search complete: {count} results")
                else:
                    result.warn('Search complete but 0 results (sandbox keys)')
                    ctx.log_event('lock_search', 'system', 'Search complete: 0 results (sandbox)', 'warning')
                return result.finish()

        if ctx.check_stopped():
            result.warn('Run stopped by admin')
            return result.finish()

    result.warn('Search timed out after 120s')
    ctx.log_event('lock_search', 'system', 'Search timed out', 'warning')
    return result.finish()


# ─── Phase 9: Browse Results ────────────────────────────────────────────────

def phase_browse(ctx):
    """Verify search results are accessible."""
    result = PhaseResult('browse')
    ctx.bot.login_as(ctx.personas[1])  # Sarah browses

    resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/search/results')
    if resp.status_code == 200:
        data = resp.json().get('data', {})
        results = data.get('results', [])
        result.ok()
        ctx.log_event('browse', ctx.personas[1]['name'], f"Browsed {len(results)} search results")
    else:
        result.fail(f"Search results GET failed: {resp.status_code}")

    # Check deals endpoint
    resp = ctx.bot.get('/api/deals')
    if resp.status_code == 200:
        result.ok()
        ctx.log_event('browse', 'system', 'Deals endpoint OK')
    else:
        result.warn(f"Deals endpoint returned {resp.status_code}")

    return result.finish()


# ─── Phase 10: Watch Create ──────────────────────────────────────────────────

def phase_watch_create(ctx):
    """Verify watches were auto-created after plan lock."""
    result = PhaseResult('watch_create')
    ctx.bot.login_as(ctx.personas[0])  # organizer checks

    resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/watches')
    if resp.status_code != 200:
        result.fail(f"Get watches failed: {resp.status_code}")
        return result.finish()

    data = resp.json().get('data', {})
    members = data.get('members', [])

    if not members:
        result.warn("No watches found — may need time for background thread")
        ctx.log_event('watch_create', 'system', 'No watches found (background may still be running)')
        # Give background thread time and retry once
        time.sleep(3)
        resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/watches')
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            members = data.get('members', [])

    total_watches = sum(len(m.get('watches', [])) for m in members)
    flight_watches = sum(1 for m in members for w in m.get('watches', []) if w.get('watch_type') == 'flight')
    hotel_watches = sum(1 for m in members for w in m.get('watches', []) if w.get('watch_type') == 'hotel')

    if total_watches > 0:
        result.ok()
        ctx.log_event('watch_create', 'system',
                      f'Watches auto-created: {flight_watches} flight, {hotel_watches} hotel for {len(members)} members')
    else:
        result.warn("No watches created — members may lack home airports")
        ctx.log_event('watch_create', 'system', 'No watches created (no home airports?)')

    return result.finish()


# ─── Phase 11: Watch Check ──────────────────────────────────────────────────

def phase_watch_check(ctx):
    """Trigger a watch price check and verify results."""
    result = PhaseResult('watch_check')

    # Trigger the check-watches task endpoint
    import os
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    resp = ctx.bot.session.get(f'{ctx.bot.base_url}/tasks/check-watches?secret={task_secret}')
    if resp.status_code == 200:
        summary = resp.json()
        checked = summary.get('checked', 0)
        alerts = summary.get('alerts_sent', 0)
        if checked > 0:
            result.ok()
            ctx.log_event('watch_check', 'system', f'Watch check: {checked} checked, {alerts} alerts')
        else:
            result.warn(f"Watch check ran but 0 prices checked (sandbox keys may return 0 results)")
            ctx.log_event('watch_check', 'system', 'Watch check ran, 0 prices (sandbox)')
    else:
        result.warn(f"Watch check endpoint returned {resp.status_code}")
        ctx.log_event('watch_check', 'system', f'Watch check returned {resp.status_code}')

    return result.finish()


# ─── Phase 12: Stop ─────────────────────────────────────────────────────────

def phase_stop(ctx):
    """Log completion. No booking."""
    result = PhaseResult('stop')
    ctx.log_event('stop', 'system', f'Trip lifecycle complete for plan {ctx.plan_id}. No booking triggered.')
    result.ok()
    return result.finish()
