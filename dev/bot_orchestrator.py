#!/usr/bin/env python3
"""
Crab Crawlers orchestrator — cleanup, phase runner, trip builder, crawl loop, main.

Split from original dev/trip_bots.py for kumori 1000-line compliance.
Runtime classes + phase functions live in trip_bots.py.
Persona data lives in bot_personas.py.

Usage:
    python dev/bot_orchestrator.py                     # Full run (all phases)
    python dev/bot_orchestrator.py --quick             # Phases 0-6 only (no AI/search)
    python dev/bot_orchestrator.py --phase vote,chat   # Specific phases (needs --plan-id)
    python dev/bot_orchestrator.py --cleanup           # Delete bot trip data
    python dev/bot_orchestrator.py --plan-id <uuid>    # Resume existing bot plan
"""

import sys
import os
import time
import json
import argparse
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests as http_requests

from dev.bot_personas import (
    PROD_URL, TIMEOUT, BOT_PREFIX, PERSONAS, DESTINATIONS,
    FIRST_NAMES, LAST_NAMES, AIRPORTS, INTERESTS_POOL,
    DIETARY_OPTIONS, ACCOMMODATION_OPTIONS, MOBILITY_OPTIONS,
)
from dev.trip_bots import (
    BotSession, RunContext, PhaseResult,
    phase_setup, phase_create, phase_join, phase_suggest, phase_preferences,
    phase_vote, phase_chat, phase_ai_research, phase_lock_search, phase_browse,
    phase_watch_create, phase_watch_check, phase_stop,
)

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger('trip_bots')


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


# ─── Phase map + runner ──────────────────────────────────────────────────────

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


# ─── Trip generators ─────────────────────────────────────────────────────────

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
        # Everyone chats — make it lively
        msgs = [random.choice([
            f"Excited for this trip! Love {random.choice(interests)}.",
            f"Can't wait! Flying in from {persona['airport']}",
            f"This is going to be amazing! I'm all about {random.choice(interests)}",
            "Count me in! When do we start planning the details?",
            f"Just booked time off work for this! Budget around ${budget_base // 100}-${(budget_base + budget_range) // 100}",
        ])]
        if random.random() < 0.6:
            msgs.append(random.choice([
                "Who's handling the group dinner reservation?",
                "I found some amazing spots we need to check out!",
                "Can we make sure there's a chill day in the itinerary?",
                "This is going to be legendary!",
                f"Anyone else into {random.choice(interests)}? We should plan something around that.",
                "I'll look into flights from my end",
                "Hotel vs Airbnb? I'm flexible either way",
                "What's everyone's dietary situation? I'm " + persona['dietary'],
                "Should we rent a car or just Uber everywhere?",
                "Who wants to share a room to save money?",
                "I vote we do at least one group dinner",
                "Anyone been there before? Tips welcome!",
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
    """Ask a free LLM to invent a trip — tries Groq/Cerebras/Mistral before Haiku."""
    from utilities.kumori_free_llms import generate

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

    text, backend = generate(prompt, max_tokens=500, temperature=1.0)
    if not text:
        log.error("All LLM backends failed for trip generation")
        return {
            'title': f'Adventure for {group_size}',
            'destinations': ['Reykjavik, Iceland', 'Marrakech, Morocco'],
            'extra_destination': 'Luang Prabang, Laos',
            'group_vibes': 'adventure and discovery',
            'trip_length_days': 7,
            'chat_messages': ['This is going to be incredible!', 'Cannot wait!', 'Already packing!'],
        }

    log.info(f"  🤖 Trip generated by {backend}")

    # Parse JSON from response
    try:
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text.strip())
    except (json.JSONDecodeError, IndexError):
        return {
            'title': f'Adventure for {group_size}',
            'destinations': ['Reykjavik, Iceland', 'Marrakech, Morocco'],
            'extra_destination': 'Luang Prabang, Laos',
            'group_vibes': 'adventure and discovery',
            'trip_length_days': 7,
            'chat_messages': ['This is going to be incredible!', 'Cannot wait!', 'Already packing!'],
        }


def _pick_trip_destiny():
    """Randomly decide how far a trip goes — mirrors real human behavior.
    ~45% stall at voting (group never decides)
    ~30% reach charting/locked (destination picked, shopping for deals)
    ~25% make it to booked (the wins)
    """
    roll = random.random()
    if roll < 0.45:
        return 'voting'
    elif roll < 0.75:
        return 'locked'
    else:
        return 'booked'


def build_random_trip(base_url, bot_secret):
    """Generate a fully random trip and run it through the pipeline.
    Each trip gets a random 'destiny' — how far it progresses, just like real humans."""
    from utilities.postgres_utils import insert_bot_run

    group_size = random.choice([2, 3, 5, 8, 10, 15, 20, 30, 50, 75, 100])
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
        # Write summary immediately so /live shows title while trip is still running
        update_bot_run(ctx.run_id, plan_id=ctx.plan_id, summary={
            'title': trip_data['title'],
            'destinations': trip_data['destinations'],
            'group_size': group_size,
            'vibe': trip_data['group_vibes'],
            'invite_token': ctx.invite_token,
            'plan_id': ctx.plan_id,
        })
        # Get destination IDs
        resp2 = ctx.bot.get(f'/api/plan/{ctx.plan_id}/destinations')
        for d in resp2.json().get('data', {}).get('destinations', []):
            ctx.destination_ids[d['destination_name']] = str(d['suggestion_id'])
    else:
        ctx.log_event('create', organizer['name'], f"Create FAILED: {data}", 'error')
        result.fail(f"Create failed: {data}")
        return False

    # Decide this trip's destiny — how far it'll go, just like real humans
    destiny = _pick_trip_destiny()
    log.info(f"  🎯 Trip destiny: {destiny}")

    # Pace the phases so trips stay "active" on /live for minutes, not seconds.
    # With 5-min cron, 1-2 trips overlap = always something live.
    PACE = 5  # seconds between phases

    # ── Join ──
    phase_join(ctx)
    time.sleep(PACE)

    # ── Suggest (if anyone has a suggestion) ──
    has_suggester = any(isinstance(p.get('suggests_destination'), str) for p in ctx.personas)
    if has_suggester:
        phase_suggest(ctx)
        time.sleep(PACE)

    # ── Preferences, Vote, Chat ──
    phase_preferences(ctx)
    time.sleep(PACE)
    phase_vote(ctx)
    time.sleep(PACE)
    phase_chat(ctx)
    time.sleep(PACE)

    # Voting-destiny trips stop here — group never decided
    if destiny == 'voting':
        log.info(f"  🗳️  Trip stalls at voting (group couldn't decide)")
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
        log.info(f"  🦀 Trip done (voting): \"{trip_data['title']}\" — {group_size} crabs")
        return True

    # ── Lock, Search, Watches — trips that made it past voting ──
    phase_lock_search(ctx)
    time.sleep(PACE)
    phase_watch_create(ctx)
    time.sleep(PACE)
    phase_watch_check(ctx)
    time.sleep(PACE)
    phase_browse(ctx)
    time.sleep(PACE)

    # ── Stop ──
    phase_stop(ctx)

    # Charting-destiny trips end at 'locked' — found deals but never pulled the trigger
    # Booked-destiny trips get promoted to 'booked' — the wins
    if destiny == 'booked' and ctx.plan_id:
        try:
            from utilities.postgres_utils import get_db_connection
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE crab.plans SET status = 'booked' WHERE plan_id = %s", (ctx.plan_id,))
            conn.commit()
            cur.close()
            conn.close()
            log.info(f"  ✈️  Trip promoted to BOOKED!")
        except Exception as e:
            log.warning(f"  ⚠️  Failed to promote to booked: {e}")

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

    log.info(f"  🦀 Trip done ({destiny}): \"{trip_data['title']}\" — {group_size} crabs, plan {ctx.plan_id}")
    return True


def nurture_past_trips(base_url, bot_secret, max_trips=5):
    """Revisit past bot trips and breathe life into them — like real humans checking in.

    Each cron run, pick a handful of non-booked bot trips and use a free LLM to
    decide what a real human would do: nudge voters, ask about dates, comment on
    deals, prod the organizer, advance the trip stage, etc.
    """
    from utilities.postgres_utils import get_db_connection
    from utilities.kumori_free_llms import generate as llm_generate
    import psycopg2.extras

    log.info(f"\n  🌱 Nurturing past trips...")

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Find bot trips that aren't booked yet — candidates for nurture
        cur.execute("""
            SELECT p.plan_id, p.title, p.status, p.created_at,
                   p.start_date, p.end_date, p.destination,
                   (SELECT COUNT(*) FROM crab.plan_members pm WHERE pm.plan_id = p.plan_id) as member_count,
                   (SELECT COUNT(*) FROM crab.messages m WHERE m.plan_id = p.plan_id) as msg_count,
                   (SELECT COUNT(*) FROM crab.votes v
                    JOIN crab.destination_suggestions ds ON ds.suggestion_id = v.suggestion_id
                    WHERE ds.plan_id = p.plan_id) as vote_count
            FROM crab.plans p
            WHERE p.title LIKE '[BOT]%%'
              AND p.status NOT IN ('booked', 'completed')
            ORDER BY RANDOM()
            LIMIT %s
        """, (max_trips,))
        trips = cur.fetchall()

        if not trips:
            log.info("  No trips to nurture")
            cur.close(); conn.close()
            return

        # Get bot members for each trip so we can post as them
        for trip in trips:
            cur.execute("""
                SELECT pm.user_id, u.display_name, u.google_id
                FROM crab.plan_members pm
                JOIN crab.users u ON u.user_id = pm.user_id
                WHERE pm.plan_id = %s AND u.display_name LIKE '[BOT]%%'
                ORDER BY pm.joined_at
                LIMIT 10
            """, (trip['plan_id'],))
            trip['members'] = cur.fetchall()

            # Get last few chat messages for context
            cur.execute("""
                SELECT m.content, u.display_name, m.created_at
                FROM crab.messages m
                JOIN crab.users u ON u.user_id = m.user_id
                WHERE m.plan_id = %s AND m.parent_id IS NULL
                ORDER BY m.created_at DESC
                LIMIT 5
            """, (trip['plan_id'],))
            trip['recent_messages'] = cur.fetchall()

        cur.close()
        conn.close()
    except Exception as e:
        log.warning(f"  ⚠️ Failed to load trips for nurture: {e}")
        return

    # For each trip, ask the LLM what a real human would do
    bot = BotSession(base_url, bot_secret)
    nurtured = 0

    for trip in trips:
        if not trip['members']:
            continue

        title = trip['title'].replace('[BOT] ', '')
        status = trip['status'] or 'planning'
        member_names = [m['display_name'].replace('[BOT] ', '') for m in trip['members']]
        organizer = member_names[0] if member_names else 'Unknown'
        recent_chat = '\n'.join([
            f"  {m['display_name'].replace('[BOT] ', '')}: {m['content']}"
            for m in reversed(trip['recent_messages'] or [])
        ]) or '  (no recent messages)'

        days_old = (date.today() - trip['created_at'].date()).days if trip.get('created_at') else 0
        trip_dates = ''
        if trip.get('start_date'):
            days_until = (trip['start_date'] - date.today()).days if hasattr(trip['start_date'], 'toordinal') else '?'
            trip_dates = f"Trip dates: {trip['start_date']} to {trip['end_date']} ({days_until} days away)"

        prompt = f"""You're simulating realistic group trip chat. A trip "{title}" has {trip['member_count']} members.
Status: {status} | Created {days_old} days ago | {trip['vote_count']} votes cast | {trip['msg_count']} messages total
{trip_dates}
Destination: {trip.get('destination') or 'not yet decided'}
Members: {', '.join(member_names[:8])}

Recent chat:
{recent_chat}

Based on the trip status and context, write 1-2 short, natural chat messages that a real member would post right now.
Pick different members (not just the organizer). Messages should feel human — casual, with personality.

Examples by status:
- voting: "Hey has everyone voted yet? We need to lock this down" / "I changed my vote to Kyoto, those pics sold me"
- planning: "So are we actually doing this or...?" / "I found cheap flights if we go in June!"
- locked: "Ok flights are looking $380 round trip, should I book?" / "Who still needs a hotel room?"

Respond in JSON only:
[{{"member": "FirstName LastName", "message": "the chat message"}}]

Pick from these members ONLY: {', '.join(member_names[:8])}
Keep messages under 120 chars. Be casual and realistic. Sometimes be pushy, sometimes excited, sometimes uncertain."""

        try:
            text, backend = llm_generate(prompt, max_tokens=300, temperature=1.0)
            if not text:
                continue

            # Parse JSON
            if '```' in text:
                text = text.split('```')[1]
                if text.startswith('json'):
                    text = text[4:]
            messages = json.loads(text.strip())
            if not isinstance(messages, list):
                continue

            # Post messages as the appropriate bot members
            posted = 0
            for msg_data in messages[:2]:  # max 2 messages per trip per nurture
                member_name = msg_data.get('member', '')
                content = msg_data.get('message', '').strip()
                if not content or not member_name:
                    continue

                # Find the matching bot member
                matching = [m for m in trip['members']
                            if member_name.lower() in m['display_name'].lower()]
                if not matching:
                    matching = [trip['members'][random.randint(0, len(trip['members']) - 1)]]

                member = matching[0]
                persona = {'db_id': member['user_id'], 'name': member['display_name']}

                try:
                    bot.login_as(persona)
                    resp = bot.post(f'/api/plan/{trip["plan_id"]}/messages', {'content': content})
                    if resp.status_code == 200 and resp.json().get('success'):
                        posted += 1
                        log.info(f"    💬 {member['display_name'].replace('[BOT] ', '')}: {content[:60]}...")
                except Exception as e:
                    log.warning(f"    ⚠️ Failed to post as {member['display_name']}: {e}")

            # Occasionally advance trip status (like a real organizer deciding to move forward)
            if posted > 0 and status in ('planning', 'voting') and random.random() < 0.15:
                # 15% chance the organizer decides to advance the trip
                next_status = 'voting' if status == 'planning' else 'locked'
                try:
                    organizer_member = trip['members'][0]
                    bot.login_as({'db_id': organizer_member['user_id'], 'name': organizer_member['display_name']})
                    resp = bot.post(f'/api/plan/{trip["plan_id"]}/stage', {'stage': next_status})
                    if resp.status_code == 200:
                        log.info(f"    📈 Trip advanced: {status} → {next_status}")
                        # Post a message about it
                        advance_msgs = {
                            'voting': "Alright everyone, let's vote! Time to pick a destination.",
                            'locked': "Ok I'm locking this in — let's start looking at flights and hotels!",
                        }
                        bot.post(f'/api/plan/{trip["plan_id"]}/messages',
                                 {'content': advance_msgs.get(next_status, 'Moving this forward!')})
                except Exception as e:
                    log.warning(f"    ⚠️ Failed to advance trip: {e}")

            if posted > 0:
                nurtured += 1

        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"    ⚠️ Nurture failed for {title}: {e}")
            continue

    log.info(f"  🌱 Nurtured {nurtured}/{len(trips)} trips")


# ─── Crawl loop + main ──────────────────────────────────────────────────────

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
