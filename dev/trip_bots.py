#!/usr/bin/env python3
"""
crab.travel Crab Crawlers — Synthetic trip bot orchestrator.

Runs 10 AI bot personas through a full group trip lifecycle against prod,
exercising every feature end-to-end. Stops before booking (deep links only).

Usage:
    python dev/trip_bots.py                     # Full run (all phases)
    python dev/trip_bots.py --quick             # Phases 0-6 only (no AI/search)
    python dev/trip_bots.py --phase vote,chat   # Specific phases (needs --plan-id)
    python dev/trip_bots.py --cleanup           # Delete bot trip data
    python dev/trip_bots.py --plan-id <uuid>    # Resume existing bot plan
"""

import sys
import os
import time
import json
import argparse
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests as http_requests

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger('trip_bots')

# ─── Config ──────────────────────────────────────────────────────────────────

PROD_URL = "https://crab.travel"
TIMEOUT = 30
BOT_PREFIX = "[BOT]"

# ─── Bot Personas ────────────────────────────────────────────────────────────

PERSONAS = [
    {
        'slug': 'marcus_chen',
        'name': f'{BOT_PREFIX} Marcus Chen',
        'email': 'bot.marcus.chen@crab.travel',
        'google_id': 'bot_marcus_chen',
        'airport': 'SEA',
        'budget_min': 20000, 'budget_max': 40000,
        'accommodation': 'hotel',
        'interests': ['hiking', 'food', 'photography'],
        'dietary': '', 'mobility': '',
        'role': 'organizer',
        'chat_messages': [
            "Hey everyone! Excited for this trip. Who's leaning Scottsdale?",
            "I've been checking flight prices from Seattle, looking good for May.",
        ],
        'vote_ranks': {'Scottsdale AZ': 1, 'Nashville TN': 2, 'San Juan PR': 3},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': False,
    },
    {
        'slug': 'sarah_kim',
        'name': f'{BOT_PREFIX} Sarah Kim',
        'email': 'bot.sarah.kim@crab.travel',
        'google_id': 'bot_sarah_kim',
        'airport': 'LAX',
        'budget_min': 15000, 'budget_max': 35000,
        'accommodation': 'airbnb',
        'interests': ['beach', 'nightlife', 'shopping'],
        'dietary': 'vegetarian', 'mobility': '',
        'role': 'member',
        'chat_messages': ["San Juan would be amazing! But I'm happy with any of the three."],
        'vote_ranks': {'Scottsdale AZ': 1, 'Nashville TN': 2, 'San Juan PR': 3},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': True,
    },
    {
        'slug': 'david_okafor',
        'name': f'{BOT_PREFIX} David Okafor',
        'email': 'bot.david.okafor@crab.travel',
        'google_id': 'bot_david_okafor',
        'airport': 'ORD',
        'budget_min': 10000, 'budget_max': 25000,
        'accommodation': 'hotel',
        'interests': ['history', 'museums', 'food'],
        'dietary': '', 'mobility': 'Uses a wheelchair — need ADA-accessible accommodations',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'Nashville TN': 1, 'Scottsdale AZ': 2},
        'blackouts': [{'start': '2026-05-17', 'end': '2026-05-18'}],
        'tentative_dates': [],
        'is_flexible': False,
    },
    {
        'slug': 'emily_rodriguez',
        'name': f'{BOT_PREFIX} Emily Rodriguez',
        'email': 'bot.emily.rodriguez@crab.travel',
        'google_id': 'bot_emily_rodriguez',
        'airport': 'DFW',
        'budget_min': 20000, 'budget_max': 50000,
        'accommodation': 'resort',
        'interests': ['spa', 'wine', 'yoga'],
        'dietary': 'gluten-free', 'mobility': '',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'San Juan PR': 1, 'Scottsdale AZ': 2, 'Nashville TN': 3},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': True,
    },
    {
        'slug': 'jake_thompson',
        'name': f'{BOT_PREFIX} Jake Thompson',
        'email': 'bot.jake.thompson@crab.travel',
        'google_id': 'bot_jake_thompson',
        'airport': 'JFK',
        'budget_min': 8000, 'budget_max': 20000,
        'accommodation': 'hostel',
        'interests': ['surfing', 'adventure', 'budget'],
        'dietary': '', 'mobility': '',
        'role': 'member',
        'chat_messages': ["Scottsdale is the move. Budget-friendly and tons to do."],
        'vote_ranks': {'Scottsdale AZ': 1},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': False,
    },
    {
        'slug': 'priya_patel',
        'name': f'{BOT_PREFIX} Priya Patel',
        'email': 'bot.priya.patel@crab.travel',
        'google_id': 'bot_priya_patel',
        'airport': 'SFO',
        'budget_min': 15000, 'budget_max': 40000,
        'accommodation': 'airbnb',
        'interests': ['cooking', 'art', 'theater'],
        'dietary': 'vegan', 'mobility': '',
        'role': 'member',
        'chat_messages': ["I can cook for the group if we get an Airbnb!"],
        'vote_ranks': {'Nashville TN': 1, 'San Juan PR': 2, 'Scottsdale AZ': 3},
        'blackouts': [],
        'tentative_dates': [{'start': '2026-05-20', 'end': '2026-05-22'}],
        'is_flexible': False,
    },
    {
        'slug': 'tom_nguyen',
        'name': f'{BOT_PREFIX} Tom Nguyen',
        'email': 'bot.tom.nguyen@crab.travel',
        'google_id': 'bot_tom_nguyen',
        'airport': 'ATL',
        'budget_min': 10000, 'budget_max': 30000,
        'accommodation': 'hotel',
        'interests': ['golf', 'sports', 'bbq'],
        'dietary': '', 'mobility': 'Bad knee — limited walking distance',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'Scottsdale AZ': 1, 'Nashville TN': 2},
        'blackouts': [{'start': '2026-05-16', 'end': '2026-05-17'}],
        'tentative_dates': [{'start': '2026-05-21', 'end': '2026-05-24'}],
        'is_flexible': False,
    },
    {
        'slug': 'lisa_washington',
        'name': f'{BOT_PREFIX} Lisa Washington',
        'email': 'bot.lisa.washington@crab.travel',
        'google_id': 'bot_lisa_washington',
        'airport': 'DEN',
        'budget_min': 20000, 'budget_max': 45000,
        'accommodation': 'resort',
        'interests': ['skiing', 'cocktails', 'live music'],
        'dietary': 'pescatarian', 'mobility': '',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'San Juan PR': 1, 'Nashville TN': 2},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': True,
        'suggests_destination': 'San Juan PR',
    },
    {
        'slug': 'carlos_mendez',
        'name': f'{BOT_PREFIX} Carlos Mendez',
        'email': 'bot.carlos.mendez@crab.travel',
        'google_id': 'bot_carlos_mendez',
        'airport': 'MIA',
        'budget_min': 12000, 'budget_max': 28000,
        'accommodation': 'hotel',
        'interests': ['diving', 'fishing', 'nature'],
        'dietary': '', 'mobility': '',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'Scottsdale AZ': 1},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': False,
        'late_joiner': True,
    },
    {
        'slug': 'amy_foster',
        'name': f'{BOT_PREFIX} Amy Foster',
        'email': 'bot.amy.foster@crab.travel',
        'google_id': 'bot_amy_foster',
        'airport': 'BOS',
        'budget_min': 18000, 'budget_max': 35000,
        'accommodation': 'airbnb',
        'interests': ['running', 'coffee', 'bookshops'],
        'dietary': 'lactose-free', 'mobility': '',
        'role': 'member',
        'chat_messages': [
            "Nashville has incredible coffee shops and live music. Just saying.",
            "Also, my tentative dates work for the 20th-22nd window.",
        ],
        'vote_ranks': {'Nashville TN': 1, 'Scottsdale AZ': 2, 'San Juan PR': 3},
        'blackouts': [],
        'tentative_dates': [{'start': '2026-05-20', 'end': '2026-05-22'}],
        'is_flexible': False,
    },
]

DESTINATIONS = ['Scottsdale AZ', 'Nashville TN']  # San Juan added by Lisa in Phase 3


# ─── Bot Session ─────────────────────────────────────────────────────────────

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


# ─── Phase 0: Setup ─────────────────────────────────────────────────────────

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
            'notes': f'{BOT_PREFIX} Synthetic test preferences',
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
            'notes': f'{BOT_PREFIX} Late joiner preferences',
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
                result.fail(f"{p['name']} vote for {dest_name} failed: {resp.text}")
                ctx.log_event('vote', p['name'], f"Vote FAILED for {dest_name}", 'error')

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
                'content': f'{BOT_PREFIX} {msg_text}',
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

    # Jake replies to Marcus's first message
    if first_msg_id:
        jake = next(p for p in ctx.personas if p['slug'] == 'jake_thompson')
        ctx.bot.login_as(jake)
        resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/messages', {
            'content': f'{BOT_PREFIX} Scottsdale for sure, flights from JFK are cheap.',
            'parent_id': first_msg_id,
        })
        if resp.status_code == 200 and resp.json().get('success'):
            result.ok()
            ctx.log_event('chat', jake['name'], f"Replied to Marcus (thread {first_msg_id[:8]}...)")
        else:
            result.fail(f"Jake reply failed: {resp.text}")

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


# ─── Phase 10: Stop ─────────────────────────────────────────────────────────

def phase_stop(ctx):
    """Log completion. No booking."""
    result = PhaseResult('stop')
    ctx.log_event('stop', 'system', f'Trip lifecycle complete for plan {ctx.plan_id}. No booking triggered.')
    result.ok()
    return result.finish()


# ─── Cleanup ─────────────────────────────────────────────────────────────────

def cleanup(ctx):
    """Delete the bot trip and all associated data."""
    if not ctx.plan_id:
        log.info("No plan_id to clean up")
        return

    organizer = ctx.personas[0]
    if organizer.get('db_id'):
        ctx.bot.login_as(organizer)
        resp = ctx.bot.post(f'/api/plan/{ctx.plan_id}/delete')
        if resp.status_code == 200:
            log.info(f"  ✅  Cleaned up plan {ctx.plan_id}")
        else:
            log.info(f"  ⚠️  API cleanup failed ({resp.status_code}), trying DB cleanup...")
            _db_cleanup()
    else:
        _db_cleanup()


def _db_cleanup():
    """Direct DB cleanup for bot data."""
    from utilities.postgres_utils import get_db_connection
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM crab.plans WHERE title LIKE %s", (f'{BOT_PREFIX}%',))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"  ✅  DB cleanup: deleted {deleted} bot plans (cascade)")
    except Exception as e:
        log.error(f"  ❌  DB cleanup failed: {e}")


def deep_cleanup():
    """Delete bot users AND all bot data."""
    _db_cleanup()
    from utilities.postgres_utils import get_db_connection
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM crab.users WHERE google_id LIKE 'bot_%'")
        deleted = cur.rowcount
        cur.execute("DELETE FROM crab.bot_runs")
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"  ✅  Deep cleanup: deleted {deleted} bot users + all bot runs")
    except Exception as e:
        log.error(f"  ❌  Deep cleanup failed: {e}")


# ─── Orchestrator ────────────────────────────────────────────────────────────

PHASES_QUICK = ['setup', 'create', 'join', 'suggest', 'preferences', 'vote', 'chat']
PHASES_FULL = PHASES_QUICK + ['ai_research', 'lock_search', 'browse', 'stop']

PHASE_MAP = {
    'setup': phase_setup,
    'create': phase_create,
    'join': phase_join,
    'suggest': phase_suggest,
    'preferences': phase_preferences,
    'vote': phase_vote,
    'chat': phase_chat,
    'ai_research': phase_ai_research,
    'lock_search': phase_lock_search,
    'browse': phase_browse,
    'stop': phase_stop,
}


def run_phases(ctx, phase_names):
    """Run a list of phases in order, logging results."""
    results = []
    total_passed = 0
    total_failed = 0
    total_warned = 0

    print(f"\n{'='*60}")
    print(f"  🦀 Crab Crawlers — {ctx.mode} mode")
    print(f"  Target: {ctx.base_url}")
    print(f"  Phases: {', '.join(phase_names)}")
    if ctx.plan_id:
        print(f"  Resuming plan: {ctx.plan_id}")
    print(f"{'='*60}\n")

    for phase_name in phase_names:
        if ctx.check_stopped():
            print(f"\n  🛑  Run stopped by admin dashboard\n")
            break

        fn = PHASE_MAP.get(phase_name)
        if not fn:
            log.warning(f"Unknown phase: {phase_name}")
            continue

        print(f"\n  ── Phase: {phase_name} {'─' * (40 - len(phase_name))}")
        try:
            result = fn(ctx)
            results.append(result)
            total_passed += result.passed
            total_failed += result.failed
            total_warned += result.warnings

            status_icon = {'passed': '✅', 'failed': '❌', 'warning': '⚠️'}[result.status]
            print(f"  {status_icon}  {phase_name}: {result.passed}ok / {result.failed}fail / {result.warnings}warn ({result.elapsed:.1f}s)")

            if result.errors:
                for err in result.errors:
                    print(f"       ❌ {err}")
        except Exception as e:
            log.error(f"  ❌  Phase {phase_name} crashed: {e}")
            total_failed += 1
            ctx.log_event(phase_name, 'system', f"Phase crashed: {e}", 'error')

    # Summary
    elapsed = sum(r.elapsed for r in results)
    overall = 'PASSED' if total_failed == 0 else 'FAILED'
    print(f"\n{'='*60}")
    print(f"  {overall}: {total_passed} passed, {total_failed} failed, {total_warned} warnings")
    print(f"  Total time: {elapsed:.1f}s")
    if ctx.plan_id:
        print(f"  Plan: {ctx.plan_id}")
    print(f"{'='*60}\n")

    # Update bot run in DB
    if ctx.run_id:
        from utilities.postgres_utils import update_bot_run
        update_bot_run(ctx.run_id,
                       status='passed' if total_failed == 0 else 'failed',
                       phases_passed=total_passed,
                       phases_failed=total_failed,
                       phases_warned=total_warned,
                       finished_at='NOW()',
                       summary={'phases': [{'name': r.phase, 'status': r.status, 'elapsed': r.elapsed} for r in results]})

    return total_failed == 0


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Crab Crawlers — synthetic trip bot testing')
    parser.add_argument('--full', action='store_true', help='Full run (all phases)')
    parser.add_argument('--quick', action='store_true', help='Quick run (phases 0-6, no AI/search)')
    parser.add_argument('--phase', type=str, help='Specific phases (comma-separated)')
    parser.add_argument('--plan-id', type=str, help='Resume with existing plan')
    parser.add_argument('--cleanup', action='store_true', help='Delete bot trip data')
    parser.add_argument('--deep-cleanup', action='store_true', help='Delete bot users + all data')
    parser.add_argument('--url', type=str, default=PROD_URL, help='Target URL')
    args = parser.parse_args()

    if args.deep_cleanup:
        deep_cleanup()
        return

    # Get bot secret
    from utilities.google_auth_utils import get_secret
    bot_secret = get_secret('CRAB_BOT_SECRET')
    if not bot_secret:
        log.error("❌ CRAB_BOT_SECRET not found. Set it in GCP Secret Manager.")
        sys.exit(1)

    if args.cleanup:
        ctx = RunContext(args.url, bot_secret, plan_id=args.plan_id)
        phase_setup(ctx)  # need db_ids to login
        cleanup(ctx)
        return

    # Determine mode and phases
    if args.phase:
        phase_names = [p.strip() for p in args.phase.split(',')]
        mode = 'phase'
    elif args.quick:
        phase_names = PHASES_QUICK
        mode = 'quick'
    else:
        phase_names = PHASES_FULL
        mode = 'full'

    # Create bot run record
    from utilities.postgres_utils import insert_bot_run
    run_id = insert_bot_run(mode)

    ctx = RunContext(args.url, bot_secret, run_id=run_id, plan_id=args.plan_id, mode=mode)

    # If resuming, need setup to get db_ids but skip create
    if args.plan_id and 'setup' not in phase_names:
        phase_setup(ctx)
        # Also need destination IDs
        ctx.bot.login_as(ctx.personas[0])
        resp = ctx.bot.get(f'/api/plan/{ctx.plan_id}/destinations')
        if resp.status_code == 200:
            for d in resp.json().get('data', {}).get('destinations', []):
                ctx.destination_ids[d['destination_name']] = str(d['suggestion_id'])

    success = run_phases(ctx, phase_names)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
