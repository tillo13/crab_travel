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

# ─── First Names Pool (for random persona generation) ────────────────────────

FIRST_NAMES = [
    'Marcus', 'Sarah', 'David', 'Emily', 'Jake', 'Priya', 'Tom', 'Lisa',
    'Carlos', 'Amy', 'Wei', 'Fatima', 'Kenji', 'Sofia', 'Olga', 'Mateo',
    'Aisha', 'Liam', 'Yuki', 'Nina', 'Ravi', 'Chloe', 'Omar', 'Hana',
    'Felix', 'Zara', 'Dmitri', 'Luna', 'Kofi', 'Maya', 'Sven', 'Amara',
    'Jin', 'Isla', 'Diego', 'Nia', 'Kai', 'Rosa', 'Theo', 'Leila',
]
LAST_NAMES = [
    'Chen', 'Kim', 'Okafor', 'Rodriguez', 'Thompson', 'Patel', 'Nguyen',
    'Washington', 'Mendez', 'Foster', 'Zhang', 'Ali', 'Tanaka', 'Petrov',
    'Garcia', 'Ibrahim', 'Ito', 'Santos', 'Johansson', 'Mbeki', 'Singh',
    'Murphy', 'Costa', 'Yamamoto', 'Osei', 'Berg', 'Reyes', 'Volkov',
    'Lee', 'Hassan', 'Schmidt', 'Nakamura', 'Torres', 'Andersen', 'Diallo',
]
AIRPORTS = [
    'SEA', 'LAX', 'ORD', 'DFW', 'JFK', 'SFO', 'ATL', 'DEN', 'MIA', 'BOS',
    'PHX', 'IAH', 'MSP', 'DTW', 'PHL', 'CLT', 'SAN', 'TPA', 'PDX', 'SLC',
    'AUS', 'RDU', 'BNA', 'STL', 'MCI', 'IND', 'CLE', 'PIT', 'CMH', 'OAK',
]
INTERESTS_POOL = [
    'hiking', 'food', 'photography', 'beach', 'nightlife', 'shopping',
    'history', 'museums', 'spa', 'wine', 'yoga', 'surfing', 'adventure',
    'cooking', 'art', 'theater', 'golf', 'sports', 'bbq', 'skiing',
    'cocktails', 'live music', 'diving', 'fishing', 'nature', 'running',
    'coffee', 'bookshops', 'architecture', 'street food', 'temples',
    'markets', 'cycling', 'kayaking', 'stargazing', 'wildlife',
]
DIETARY_OPTIONS = ['', '', '', '', 'vegetarian', 'vegan', 'gluten-free', 'pescatarian', 'lactose-free', 'halal', 'kosher']
ACCOMMODATION_OPTIONS = ['hotel', 'hotel', 'airbnb', 'airbnb', 'resort', 'hostel', 'flexible']
MOBILITY_OPTIONS = ['', '', '', '', '', '', '', 'wheelchair accessible', 'limited walking', 'no stairs']


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
PHASES_FULL = PHASES_QUICK + ['ai_research', 'lock_search', 'watch_create', 'watch_check', 'browse', 'stop']

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
    'watch_create': phase_watch_create,
    'watch_check': phase_watch_check,
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


# ─── Random Trip Generator (Haiku-powered) ──────────────────────────────────

import random
from datetime import date, timedelta


def _random_blackouts():
    """Generate 0-3 random blackout date ranges for a crawl bot."""
    blackouts = []
    if random.random() < 0.6:  # 60% chance of having blackouts
        for _ in range(random.randint(1, 3)):
            start_offset = random.randint(14, 120)
            duration = random.randint(1, 5)
            start = (date.today() + timedelta(days=start_offset)).isoformat()
            end = (date.today() + timedelta(days=start_offset + duration)).isoformat()
            blackouts.append({'start': start, 'end': end})
    return blackouts


def _random_tentative_dates():
    """Every crawl bot gets 2-4 tentative date ranges to fill the calendar."""
    tentative = []
    for _ in range(random.randint(2, 4)):
        start_offset = random.randint(7, 90)
        duration = random.randint(3, 14)
        start = (date.today() + timedelta(days=start_offset)).isoformat()
        end = (date.today() + timedelta(days=start_offset + duration)).isoformat()
        pref = random.choice(['ideal', 'ideal', 'works', 'if_needed'])  # bias toward ideal
        tentative.append({'start': start, 'end': end, 'preference': pref})
    return tentative


def generate_random_personas(count):
    """Generate N random bot personas with varied preferences."""
    used_names = set()
    personas = []
    for i in range(count):
        while True:
            first = random.choice(FIRST_NAMES)
            last = random.choice(LAST_NAMES)
            name = f"{first} {last}"
            if name not in used_names:
                used_names.add(name)
                break
        slug = f"{first.lower()}_{last.lower()}"
        budget_base = random.choice([8000, 10000, 12000, 15000, 18000, 20000, 25000])
        budget_range = random.choice([10000, 15000, 20000, 25000, 30000])
        interests = random.sample(INTERESTS_POOL, random.randint(2, 5))
        persona = {
            'slug': f'crawl_{slug}',
            'name': f'{BOT_PREFIX} {name}',
            'email': f'bot.{slug}@crab.travel',
            'google_id': f'bot_crawl_{slug}',
            'airport': random.choice(AIRPORTS),
            'budget_min': budget_base,
            'budget_max': budget_base + budget_range,
            'accommodation': random.choice(ACCOMMODATION_OPTIONS),
            'interests': interests,
            'dietary': random.choice(DIETARY_OPTIONS),
            'mobility': random.choice(MOBILITY_OPTIONS),
            'role': 'organizer' if i == 0 else 'member',
            'chat_messages': [],
            'vote_ranks': {},  # filled after destinations are known
            'blackouts': _random_blackouts(),
            'tentative_dates': _random_tentative_dates(),
            'is_flexible': random.random() < 0.1,  # most bots show specific dates
        }
        # ~80% of personas post chat messages — make it lively
        if random.random() < 0.8:
            msgs = [f"Excited for this trip! Love {random.choice(interests)}."]
            if random.random() < 0.5:
                msgs.append(random.choice([
                    "Who's handling the group dinner reservation?",
                    "I found some amazing spots we need to check out!",
                    "Can we make sure there's a chill day in the itinerary?",
                    f"Budget-wise I'm comfortable up to ${budget_base // 100 + budget_range // 100}",
                    "This is going to be legendary!",
                    f"Anyone else into {random.choice(interests)}? We should plan something around that.",
                    "I'll look into flights from my end",
                    "Hotel vs Airbnb? I'm flexible either way",
                ]))
            persona['chat_messages'] = msgs
        # ~20% are late joiners (not the organizer)
        if i > 0 and random.random() < 0.2:
            persona['late_joiner'] = True
        # ~15% suggest a destination (not the organizer)
        if i > 1 and random.random() < 0.15 and not persona.get('late_joiner'):
            persona['suggests_destination'] = True  # filled later with Haiku destination
        personas.append(persona)
    return personas


def haiku_pick_trip(group_size):
    """Ask Haiku to invent a trip — destinations, vibe, title."""
    from utilities.claude_utils import generate_text, _get_api_key, API_URL
    import requests as _req

    prompt = f"""You are generating a random group trip for {group_size} friends. Pick ANYWHERE on planet Earth — be creative and varied. Mix famous cities with unexpected places. Respond in JSON only:

{{
  "title": "short fun trip name (5-8 words max)",
  "destinations": ["Place 1, Country", "Place 2, Country"],
  "extra_destination": "Place 3, Country",
  "group_vibes": "2-4 word vibe description",
  "trip_length_days": number between 3 and 14,
  "chat_messages": ["excited message from organizer", "response from a member", "another member chiming in"]
}}

Pick 2 main destinations and 1 extra that a member might suggest. Be wildly varied — don't repeat popular tourist cities. Think: Faroe Islands, Oaxaca, Tbilisi, Hokkaido, Zanzibar, Cartagena, Ljubljana, Jeju Island, Oman, Patagonia, Transylvania, Azores, etc. Mix it up every time."""

    api_key = _get_api_key()
    body = {
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 500,
        'temperature': 1.0,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }
    r = _req.post(API_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    text = r.json()['content'][0]['text']

    # Parse JSON from response
    try:
        # Handle markdown code blocks
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text.strip())
    except (json.JSONDecodeError, IndexError):
        # Fallback
        return {
            'title': f'Adventure for {group_size}',
            'destinations': ['Reykjavik, Iceland', 'Marrakech, Morocco'],
            'extra_destination': 'Luang Prabang, Laos',
            'group_vibes': 'adventure and discovery',
            'trip_length_days': 7,
            'chat_messages': ['This is going to be incredible!', 'Cannot wait!', 'Already packing!'],
        }


def build_random_trip(base_url, bot_secret):
    """Generate a fully random trip and run it through the quick pipeline."""
    from utilities.postgres_utils import insert_bot_run

    group_size = random.choice([5, 6, 7, 8, 10, 12, 15, 18, 20, 25])
    log.info(f"\n  🎲 Generating random trip for {group_size} people...")

    # Ask Haiku to invent the trip
    trip_data = haiku_pick_trip(group_size)
    log.info(f"  🌍 Haiku says: \"{trip_data['title']}\"")
    log.info(f"     Destinations: {trip_data['destinations']}")
    log.info(f"     Vibe: {trip_data['group_vibes']}")

    # Generate random personas
    personas = generate_random_personas(group_size)

    # Assign vote ranks based on actual destinations
    all_dests = trip_data['destinations'] + [trip_data.get('extra_destination', 'Surprise Destination')]
    for p in personas:
        num_votes = random.randint(1, len(trip_data['destinations']))
        ranked = random.sample(trip_data['destinations'], num_votes)
        p['vote_ranks'] = {dest: rank + 1 for rank, dest in enumerate(ranked)}

    # Assign chat messages from Haiku
    chatty_personas = [p for p in personas if random.random() < 0.5]
    haiku_msgs = trip_data.get('chat_messages', [])
    for i, p in enumerate(chatty_personas[:len(haiku_msgs)]):
        p['chat_messages'] = [haiku_msgs[i]]

    # Assign the extra destination suggestion to one persona
    suggester = next((p for p in personas if p.get('suggests_destination')), None)
    extra_dest = trip_data.get('extra_destination')
    if suggester and extra_dest:
        suggester['suggests_destination'] = extra_dest
    elif extra_dest and len(personas) > 2:
        # Pick a random non-organizer to suggest
        candidate = random.choice(personas[2:]) if len(personas) > 2 else personas[1]
        if not candidate.get('late_joiner'):
            candidate['suggests_destination'] = extra_dest

    # Create run context
    run_id = insert_bot_run('crawl')
    ctx = RunContext(base_url, bot_secret, run_id=run_id, mode='crawl')
    ctx.personas = personas

    # Override the create phase to use Haiku's trip data
    ctx._trip_data = trip_data

    # Run the phases
    phase_setup(ctx)

    # ── Create (custom for random trip) ──
    result = PhaseResult('create')
    organizer = ctx.personas[0]
    ctx.bot.login_as(organizer)
    resp = ctx.bot.post('/api/plan/create', {
        'title': f"{BOT_PREFIX} {trip_data['title']}",
        'destinations': trip_data['destinations'],
    })
    data = resp.json()
    if resp.status_code == 200 and data.get('success'):
        ctx.plan_id = data['data']['plan_id']
        ctx.invite_token = data['data']['invite_token']
        ctx.log_event('create', organizer['name'], f"Created: {trip_data['title']} → {', '.join(trip_data['destinations'])}")
        result.ok()
        from utilities.postgres_utils import update_bot_run
        update_bot_run(ctx.run_id, plan_id=ctx.plan_id)
        # Get destination IDs
        resp2 = ctx.bot.get(f'/api/plan/{ctx.plan_id}/destinations')
        for d in resp2.json().get('data', {}).get('destinations', []):
            ctx.destination_ids[d['destination_name']] = str(d['suggestion_id'])
    else:
        ctx.log_event('create', organizer['name'], f"Create FAILED: {data}", 'error')
        result.fail(f"Create failed: {data}")
        return False

    # ── Join ──
    phase_join(ctx)

    # ── Suggest (if anyone has a suggestion) ──
    has_suggester = any(isinstance(p.get('suggests_destination'), str) for p in ctx.personas)
    if has_suggester:
        phase_suggest(ctx)

    # ── Preferences, Vote, Chat ──
    phase_preferences(ctx)
    phase_vote(ctx)
    phase_chat(ctx)

    # ── Stop ──
    phase_stop(ctx)

    # Finalize
    from utilities.postgres_utils import update_bot_run
    update_bot_run(ctx.run_id,
                   status='passed',
                   finished_at='NOW()',
                   summary={
                       'title': trip_data['title'],
                       'destinations': all_dests,
                       'group_size': group_size,
                       'vibe': trip_data['group_vibes'],
                       'invite_token': ctx.invite_token,
                       'plan_id': ctx.plan_id,
                   })

    log.info(f"  🦀 Trip complete: \"{trip_data['title']}\" — {group_size} crabs, plan {ctx.plan_id}")
    return True


def crawl_forever(base_url, bot_secret, interval=300, max_concurrent=5):
    """Continuously generate and run random trips. The crabs never stop crawling."""
    log.info(f"\n{'='*60}")
    log.info(f"  🦀🦀🦀 CRAB CRAWL MODE — continuous random trips")
    log.info(f"  Target: {base_url}")
    log.info(f"  Interval: {interval}s between trips")
    log.info(f"  Press Ctrl+C to stop")
    log.info(f"{'='*60}\n")

    trip_count = 0
    while True:
        trip_count += 1
        log.info(f"\n{'─'*60}")
        log.info(f"  🦀 Crawl #{trip_count}")
        log.info(f"{'─'*60}")

        try:
            build_random_trip(base_url, bot_secret)
        except Exception as e:
            log.error(f"  ❌ Trip #{trip_count} failed: {e}")

        # Check how many bot plans exist, clean old ones if over limit
        try:
            from utilities.postgres_utils import get_db_connection
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM crab.plans WHERE title LIKE '[BOT]%%'")
            bot_plan_count = cur.fetchone()[0]
            if bot_plan_count > max_concurrent * 3:
                # Delete oldest bot plans, keep the most recent max_concurrent
                cur.execute("""
                    DELETE FROM crab.plans WHERE plan_id IN (
                        SELECT plan_id FROM crab.plans
                        WHERE title LIKE '[BOT]%%'
                        ORDER BY created_at ASC
                        LIMIT %s
                    )
                """, (bot_plan_count - max_concurrent,))
                pruned = cur.rowcount
                conn.commit()
                log.info(f"  🧹 Pruned {pruned} old bot plans (keeping {max_concurrent})")
            cur.close()
            conn.close()
        except Exception:
            pass

        log.info(f"\n  ⏳ Next trip in {interval}s...")
        time.sleep(interval)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Crab Crawlers — synthetic trip bot testing')
    parser.add_argument('--full', action='store_true', help='Full run (all phases)')
    parser.add_argument('--quick', action='store_true', help='Quick run (phases 0-6, no AI/search)')
    parser.add_argument('--phase', type=str, help='Specific phases (comma-separated)')
    parser.add_argument('--plan-id', type=str, help='Resume with existing plan')
    parser.add_argument('--cleanup', action='store_true', help='Delete bot trip data')
    parser.add_argument('--deep-cleanup', action='store_true', help='Delete bot users + all data')
    parser.add_argument('--crawl', action='store_true', help='Continuous random trips (crabs never stop)')
    parser.add_argument('--interval', type=int, default=300, help='Seconds between crawl trips (default 300)')
    parser.add_argument('--max-trips', type=int, default=10, help='Max bot trips to keep in DB (default 10)')
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

    if args.crawl:
        crawl_forever(args.url, bot_secret, interval=args.interval, max_concurrent=args.max_trips)
        return  # never reached (crawl_forever loops until Ctrl+C)

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
