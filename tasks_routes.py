"""
Cron/background task routes
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

bp = Blueprint('tasks_routes', __name__)


# ── Demo viewer constants (also defined in plan_routes) ──
DEMO_VIEWER_GOOGLE_ID = 'demo_viewer_judy_tunaboat'
DEMO_VIEWER_NAME = 'Judy Tunaboat'


@bp.route('/tasks/crawl')
def task_crawl():
    """Cron job — run one random bot trip (Crab Crawlers)."""
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    try:
        # Auto-fail bot runs stuck in "running" for > 1 hour
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE crab.bot_runs SET status='failed', finished_at=NOW()
                WHERE status='running' AND started_at < NOW() - INTERVAL '1 hour'
            """)
            stuck = cur.rowcount
            if stuck:
                logger.info(f"🧹 Auto-failed {stuck} stuck bot runs")
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning(f"⚠️ Stuck run cleanup failed: {e}")

        import subprocess
        cwd = '/app' if os.path.exists('/app') else os.path.dirname(os.path.abspath(__file__))
        # Run one random trip + nurture past trips (block until done so App Engine
        # keeps the instance alive — Popen-detached subprocesses get SIGKILLed when
        # the instance scales down off-hours, leaving runs stuck and auto-failed).
        try:
            subprocess.run(
                ['python3', '-c',
                 'import sys; sys.path.insert(0,"."); '
                 'from dev.trip_bots import build_random_trip, nurture_past_trips; '
                 'from utilities.google_auth_utils import get_secret; '
                 's = get_secret("CRAB_BOT_SECRET"); '
                 'build_random_trip("https://crab.travel", s); '
                 'nurture_past_trips("https://crab.travel", s, max_trips=5)'],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=540,  # 9 min — under App Engine cron 10-min request deadline
            )
        except subprocess.TimeoutExpired:
            logger.warning("⚠️ Bot run subprocess hit 540s timeout")
        # Prune old bot plans (keep last 100, never prune booked trips)
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM crab.plans WHERE plan_id IN (
                    SELECT plan_id FROM crab.plans
                    WHERE title LIKE '[BOT]%%'
                      AND status NOT IN ('booked', 'completed')
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


@bp.route('/tasks/refresh-deals')
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


@bp.route('/tasks/vote-reminders')
def task_vote_reminders():
    """Cron job — email/SMS reminders to plan members who haven't voted yet.

    Idempotent via crab.notifications_sent (max one reminder per plan/user/day).
    """
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    try:
        from utilities.notification_utils import notify_vote_reminder
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Plans currently in voting/planning stage with at least one destination suggestion
        cur.execute("""
            SELECT DISTINCT p.plan_id
            FROM crab.plans p
            JOIN crab.destination_suggestions d ON d.plan_id = p.plan_id
            WHERE p.status IN ('planning', 'voting')
              AND p.title NOT LIKE '[BOT]%%'
        """)
        plan_ids = [r['plan_id'] for r in cur.fetchall()]
        cur.close()
        conn.close()

        total = 0
        for pid in plan_ids:
            try:
                total += notify_vote_reminder(pid) or 0
            except Exception as e:
                logger.warning(f"vote reminder failed for plan {pid}: {e}")
        logger.info(f"✅ /tasks/vote-reminders complete: {total} reminders across {len(plan_ids)} plans")
        return jsonify({'success': True, 'reminders_sent': total, 'plans_scanned': len(plan_ids)})
    except Exception as e:
        logger.error(f"❌ vote-reminders failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/tasks/check-watches')
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


@bp.route('/tasks/seed-demo-viewer')
def task_seed_demo_viewer():
    """One-time task: create demo viewer 'Judy Tunaboat' and add her to ALL trips with availability."""
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    try:
        from utilities.invite_utils import generate_token
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1. Upsert Judy Tunaboat user
        cur.execute("""
            INSERT INTO crab.users (google_id, email, full_name, picture_url, home_airport)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (google_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                home_airport = EXCLUDED.home_airport,
                updated_at = NOW()
            RETURNING pk_id
        """, (DEMO_VIEWER_GOOGLE_ID, 'judy@tunaboat.crab.travel', DEMO_VIEWER_NAME, None, 'SEA'))
        judy_id = cur.fetchone()['pk_id']

        # 1b. Ensure Scottsdale AZ is in the demo trip's destination suggestions
        demo_plan_id = '25438c20-0bb3-4137-9f5f-2ebdbeb0010b'
        cur.execute("""
            SELECT pk_id FROM crab.destination_suggestions
            WHERE plan_id = %s AND destination_name = 'Scottsdale, AZ'
        """, (demo_plan_id,))
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO crab.destination_suggestions (plan_id, suggested_by, destination_name, status)
                VALUES (%s, %s, 'Scottsdale, AZ', 'ready')
            """, (demo_plan_id, judy_id))

        # Ensure demo trip statuses match their stage
        cur.execute("UPDATE crab.plans SET status = 'booked' WHERE plan_id = %s AND status != 'booked'", (demo_plan_id,))
        # Set voting demo trip status
        cur.execute("""
            UPDATE crab.plans SET status = 'voting', locked_destination = NULL
            WHERE invite_token = 'xL2aRt-k'
        """)
        # Set planning demo trip status
        cur.execute("""
            UPDATE crab.plans SET status = 'planning'
            WHERE invite_token = 'TpPeETPm'
        """)

        # 1c. Seed destination card data for ALL destinations on demo trip
        demo_cards = {
            'Salvador, Brazil': {
                'card': {
                    'summary': 'Vibrant Afro-Brazilian culture meets stunning tropical beaches in Salvador, Bahia. The historic Pelourinho district is a UNESCO World Heritage Site with colorful colonial architecture, incredible street food, and live music on every corner. Perfect for groups who love culture, dancing, and beach days.',
                    'highlights': ['Pelourinho historic district', 'Beaches: Porto da Barra & Farol da Barra', 'Afro-Brazilian cuisine & acarajé', 'Capoeira & live samba'],
                    'best_dates': 'Year-round tropical weather. Carnival (February) is legendary but crowded. May-June ideal for smaller crowds.',
                    'weather_note': 'Tropical — 77-86°F year-round, brief afternoon showers. Pack light layers and sunscreen.',
                    'estimated_total_per_person': '$1,200-2,000',
                    'compatibility_score': 78,
                    'concerns': ['Language barrier (Portuguese)', 'Petty theft in tourist areas'],
                    'stays': [
                        {'name': 'Fera Palace Hotel', 'price_hint': '$$', 'description': 'Art deco landmark overlooking the Bay of All Saints. Rooftop pool with panoramic views.', 'location': 'Pelourinho'},
                        {'name': 'Vila Galé Salvador', 'price_hint': '$$', 'description': 'Beachfront resort with all-inclusive option, perfect for groups who want pool + beach access.', 'location': 'Ondina Beach'},
                        {'name': 'Casa do Amarelindo', 'price_hint': '$$$', 'description': 'Boutique pousada in a restored colonial mansion. Intimate rooftop terrace and homemade breakfast.', 'location': 'Santo Antônio'},
                    ],
                    'things_to_do': [
                        {'name': 'Pelourinho Walking Tour', 'category': 'culture', 'price_hint': '$', 'description': 'Wander the cobblestone streets of this UNESCO district — churches, street art, and live drumming.', 'group_vibe': 'chill'},
                        {'name': 'Capoeira Class', 'category': 'activity', 'price_hint': '$', 'description': 'Learn the Afro-Brazilian martial art/dance from local masters. No experience needed.', 'group_vibe': 'active'},
                        {'name': 'Mercado Modelo', 'category': 'culture', 'price_hint': 'Free', 'description': 'Iconic market for crafts, souvenirs, and local snacks. Haggling encouraged.', 'group_vibe': 'chill'},
                        {'name': 'Sunset at Farol da Barra', 'category': 'activity', 'price_hint': 'Free', 'description': 'Watch the sun set from the lighthouse point — Salvador\'s most iconic sunset spot.', 'group_vibe': 'chill'},
                        {'name': 'Baiana Cooking Class', 'category': 'food', 'price_hint': '$$', 'description': 'Learn to make moqueca, acarajé, and other Bahian dishes with a local chef.', 'group_vibe': 'chill'},
                    ],
                    'food_and_drink': [
                        {'name': 'Restaurante Paraíso Tropical', 'category': 'restaurant', 'price_hint': '$$', 'description': 'Best moqueca in Salvador — rich seafood stew with dendê oil and coconut milk.'},
                        {'name': 'Acarajé da Dinha', 'category': 'street_food', 'price_hint': '$', 'description': 'Legendary street-side acarajé (black-eyed pea fritters stuffed with shrimp and vatapá).'},
                        {'name': 'Bar Ulisses', 'category': 'bar', 'price_hint': '$', 'description': 'No-frills locals bar in Pelourinho. Cold beer, friendly crowd, and occasional live forró music.'},
                    ],
                    'upcoming_events': [],
                },
                'research': {'status': 'complete'},
            },
            'Lapland, Finland': {
                'card': {
                    'summary': 'Arctic wilderness at its most magical — Northern Lights, husky sledding, ice hotels, and endless snow-covered forests. Lapland is a once-in-a-lifetime group adventure for those who want something truly different. Summer offers midnight sun and hiking; winter is pure frozen wonderland.',
                    'highlights': ['Northern Lights viewing', 'Husky & reindeer sledding', 'Glass igloo stays', 'Arctic sauna + ice swimming'],
                    'best_dates': 'December-March for snow & Northern Lights. June-August for midnight sun. September for fall colors + early auroras.',
                    'weather_note': 'Winter: -4 to -22°F. Bring serious layers. Summer: 50-70°F with 24hr daylight.',
                    'estimated_total_per_person': '$2,500-4,000',
                    'compatibility_score': 72,
                    'concerns': ['Extreme cold requires proper gear', 'Remote location — limited nightlife', 'Expensive region'],
                    'stays': [
                        {'name': 'Arctic TreeHouse Hotel', 'price_hint': '$$$', 'description': 'Glass-walled suites perched in the treetops — watch the Northern Lights from bed.', 'location': 'Rovaniemi'},
                        {'name': 'Kakslauttanen Arctic Resort', 'price_hint': '$$$', 'description': 'Famous glass igloos and log cabins. Group-friendly with shared saunas and dining.', 'location': 'Saariselkä'},
                        {'name': 'Wilderness Hotel Muotka', 'price_hint': '$$', 'description': 'Cozy aurora cabins on a frozen lake. Includes guided husky safaris.', 'location': 'Inari'},
                    ],
                    'things_to_do': [
                        {'name': 'Husky Safari', 'category': 'activity', 'price_hint': '$$', 'description': 'Mush your own team of huskies across frozen landscapes. 2-4 hour options available.', 'group_vibe': 'active'},
                        {'name': 'Northern Lights Hunt', 'category': 'activity', 'price_hint': '$$', 'description': 'Guided snowmobile or bus chase across the tundra to find clear skies for aurora viewing.', 'group_vibe': 'adventurous'},
                        {'name': 'Ice Swimming & Sauna', 'category': 'activity', 'price_hint': '$', 'description': 'Traditional Finnish experience: hot sauna then plunge into a hole cut in a frozen lake.', 'group_vibe': 'active'},
                        {'name': 'Reindeer Farm Visit', 'category': 'culture', 'price_hint': '$', 'description': 'Meet Sámi reindeer herders, learn about indigenous culture, and take a reindeer sleigh ride.', 'group_vibe': 'chill'},
                        {'name': 'Snowmobile Safari', 'category': 'activity', 'price_hint': '$$', 'description': 'Rip across frozen lakes and through snowy forests at speed. Helmets and suits provided.', 'group_vibe': 'adventurous'},
                    ],
                    'food_and_drink': [
                        {'name': 'Nili Restaurant', 'category': 'restaurant', 'price_hint': '$$$', 'description': 'Lappish fine dining — reindeer, Arctic char, cloudberries. Atmospheric log cabin setting.'},
                        {'name': 'Café & Bar 21', 'category': 'bar', 'price_hint': '$$', 'description': 'Warm craft cocktails after a cold day. Try the cloudberry gin and tonic.'},
                        {'name': 'Kotahovi', 'category': 'restaurant', 'price_hint': '$$', 'description': 'Eat in a traditional Lappish kota (tent) around an open fire. Salmon cooked on cedar planks.'},
                    ],
                    'upcoming_events': [],
                },
                'research': {'status': 'complete'},
            },
            'Scottsdale, AZ': {
                'card': {
                    'summary': 'Sun-drenched desert paradise with world-class golf, spa resorts, and stunning Sonoran Desert landscapes. Old Town Scottsdale has a walkable nightlife and gallery scene, while the surrounding desert offers hiking, Jeep tours, and hot air balloon rides. Perfect for groups who want poolside relaxation mixed with adventure.',
                    'highlights': ['Old Town nightlife & galleries', 'Camelback Mountain hike', 'Desert Botanical Garden', 'Championship golf courses', 'Spa & resort pool days'],
                    'best_dates': 'October-April for ideal weather (70-85°F). May gets hot. Summer is 105°F+ but resort prices drop 50%.',
                    'weather_note': 'May: 90-100°F, sunny, low humidity. Perfect pool weather. Mornings ideal for hiking.',
                    'estimated_total_per_person': '$1,800-3,000',
                    'compatibility_score': 91,
                    'concerns': ['Hot in summer months', 'Need a car for some activities'],
                    'stays': [
                        {'name': 'The Scott Resort & Spa', 'price_hint': '$$', 'description': 'Stylish mid-century modern resort steps from Old Town. Two pools, great restaurant, walkable.', 'location': 'Old Town'},
                        {'name': 'Mountain Shadows', 'price_hint': '$$$', 'description': 'Sleek boutique resort at the base of Camelback Mountain. Infinity pool with sunset views.', 'location': 'Paradise Valley'},
                        {'name': 'Hotel Valley Ho', 'price_hint': '$$', 'description': 'Retro-chic landmark with massive pool scene and OH Pool bar. Walking distance to everything.', 'location': 'Old Town'},
                        {'name': 'Civana Wellness Resort', 'price_hint': '$$$', 'description': 'Full wellness resort with yoga, meditation, spa, and farm-to-table dining. Desert serenity.', 'location': 'Carefree'},
                    ],
                    'things_to_do': [
                        {'name': 'Camelback Mountain Sunrise Hike', 'category': 'activity', 'price_hint': 'Free', 'description': 'Iconic scramble with 360° views of the Valley. Go at sunrise to beat the heat.', 'group_vibe': 'active'},
                        {'name': 'Old Town Gallery Walk', 'category': 'culture', 'price_hint': 'Free', 'description': 'Thursday night art walks through 100+ galleries. Wine, art, and people-watching.', 'group_vibe': 'chill'},
                        {'name': 'Desert Jeep Tour', 'category': 'activity', 'price_hint': '$$', 'description': 'Off-road through the Sonoran Desert — saguaros, coyotes, and stunning red rock formations.', 'group_vibe': 'adventurous'},
                        {'name': 'Hot Air Balloon Ride', 'category': 'activity', 'price_hint': '$$$', 'description': 'Float over the desert at sunrise. Includes champagne toast on landing.', 'group_vibe': 'chill'},
                        {'name': 'Topgolf Scottsdale', 'category': 'activity', 'price_hint': '$$', 'description': 'Multi-level driving range with games, food, and drinks. Great group activity day or night.', 'group_vibe': 'active'},
                        {'name': 'Salt River Tubing', 'category': 'activity', 'price_hint': '$', 'description': 'Float down the Salt River on inner tubes with coolers. Classic Arizona group activity.', 'group_vibe': 'chill'},
                    ],
                    'food_and_drink': [
                        {'name': 'Citizen Public House', 'category': 'restaurant', 'price_hint': '$$', 'description': 'Craft cocktails and elevated pub food. Try the pork belly and smoked salmon flatbread.'},
                        {'name': 'Diego Pops', 'category': 'restaurant', 'price_hint': '$$', 'description': 'Colorful modern Mexican with an Instagram-worthy patio. Great margaritas and street tacos.'},
                        {'name': 'The Montauk', 'category': 'bar', 'price_hint': '$$', 'description': 'Rooftop bar in Old Town with fire pits, string lights, and craft cocktails. Perfect for groups.'},
                        {'name': 'Hash Kitchen', 'category': 'restaurant', 'price_hint': '$$', 'description': 'Brunch destination with a DIY Bloody Mary bar. Weekend wait is worth it.'},
                    ],
                    'upcoming_events': [
                        {'name': 'Scottsdale ArtWalk', 'date': 'Every Thursday', 'description': 'Free weekly gallery walk through the Arts District. Wine and live music at most galleries.'},
                    ],
                },
                'research': {'status': 'complete'},
            },
        }

        # Update each destination's card data
        for dest_name, dest_data in demo_cards.items():
            cur.execute("""
                UPDATE crab.destination_suggestions
                SET destination_data = %s
                WHERE plan_id = %s AND destination_name = %s AND (destination_data IS NULL OR destination_data->'card'->>'summary' = '' OR destination_data->'card'->>'summary' IS NULL)
            """, (psycopg2.extras.Json(dest_data), demo_plan_id, dest_name))

        # Add votes for destinations that don't have them (Salvador, Lapland, Scottsdale)
        cur.execute("SELECT suggestion_id, destination_name FROM crab.destination_suggestions WHERE plan_id = %s", (demo_plan_id,))
        demo_dests = {r['destination_name']: r['suggestion_id'] for r in cur.fetchall()}

        # Get some member user_ids to create votes from
        cur.execute("SELECT user_id FROM crab.plan_members WHERE plan_id = %s AND user_id IS NOT NULL LIMIT 12", (demo_plan_id,))
        voter_ids = [r['user_id'] for r in cur.fetchall()]

        # Clear and re-seed all votes for demo trip destinations
        cur.execute("DELETE FROM crab.votes WHERE plan_id = %s AND target_type = 'destination'", (demo_plan_id,))

        vote_weights = {
            'Scottsdale, AZ': [1,1,1,1,1,1,2,2,3],
            'Sagano, Japan': [1,1,2,2,2,3,3,4],
            'Salvador, Brazil': [1,2,2,3,3,3,4],
            'Lapland, Finland': [2,3,3,4,4],
        }
        for dest_name, sug_id in demo_dests.items():
            ranks = vote_weights.get(dest_name, [2,3])
            for i, uid in enumerate(voter_ids[:len(ranks)]):
                cur.execute("""
                    INSERT INTO crab.votes (plan_id, user_id, target_type, target_id, vote)
                    VALUES (%s, %s, 'destination', %s, %s)
                    ON CONFLICT (plan_id, user_id, target_type, target_id) DO NOTHING
                """, (demo_plan_id, uid, str(sug_id), ranks[i]))

        # 2. Reset any plans Judy currently owns back to a bot user
        cur.execute("""
            UPDATE crab.plans SET organizer_id = (
                SELECT u.pk_id FROM crab.users u WHERE u.google_id LIKE 'bot_%%' LIMIT 1
            ) WHERE organizer_id = %s
        """, (judy_id,))
        reset_count = cur.rowcount

        # 3. Get ALL plans
        cur.execute("SELECT plan_id, status, organizer_id FROM crab.plans ORDER BY created_at DESC")
        plans = cur.fetchall()

        joined = 0
        owned = 0
        for i, plan in enumerate(plans):
            plan_id = plan['plan_id']

            # Make Judy the organizer of every 20th plan (so demo visitors occasionally see organizer view)
            if i % 20 == 0 and plan['organizer_id'] != judy_id:
                cur.execute("UPDATE crab.plans SET organizer_id = %s WHERE plan_id = %s", (judy_id, plan_id))
                owned += 1

            # Skip if already a member
            cur.execute("SELECT pk_id FROM crab.plan_members WHERE plan_id = %s AND user_id = %s", (plan_id, judy_id))
            if cur.fetchone():
                continue

            cur.execute("""
                INSERT INTO crab.plan_members (plan_id, user_id, display_name, email, member_token, role, home_airport, is_flexible)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (plan_id, judy_id, DEMO_VIEWER_NAME, 'judy@tunaboat.crab.travel',
                  generate_token(), 'member', 'SEA', False))

            # Add tentative dates — grab the plan's date window from existing member data
            cur.execute("""
                SELECT MIN(date_start) as earliest, MAX(date_end) as latest
                FROM crab.member_tentative_dates WHERE plan_id = %s
            """, (plan_id,))
            dates = cur.fetchone()
            if dates and dates['earliest'] and dates['latest']:
                cur.execute("""
                    INSERT INTO crab.member_tentative_dates (plan_id, user_id, date_start, date_end, preference)
                    VALUES (%s, %s, %s, %s, 'ideal')
                """, (plan_id, judy_id, dates['earliest'], dates['latest']))

            # Add a blackout range too (a few days before the trip window)
            cur.execute("""
                SELECT MIN(date_start) as earliest FROM crab.member_tentative_dates
                WHERE plan_id = %s AND user_id != %s
            """, (plan_id, judy_id))
            trip_start = cur.fetchone()
            if trip_start and trip_start['earliest']:
                from datetime import timedelta
                blk_end = trip_start['earliest'] - timedelta(days=1)
                blk_start = blk_end - timedelta(days=3)
                cur.execute("""
                    INSERT INTO crab.member_blackouts (plan_id, user_id, blackout_start, blackout_end)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (plan_id, judy_id, blk_start, blk_end))

            joined += 1

        # (Chat messages are seeded via /tasks/seed-demo-chat which uses the LLM router)

        # Seed expenses on the booked demo trip
        cur.execute("SELECT COUNT(*) as cnt FROM crab.expenses WHERE plan_id = %s", (demo_plan_id,))
        if cur.fetchone()['cnt'] == 0:
            # Get some member pk_ids for paid_by
            cur.execute("""
                SELECT pk_id, display_name FROM crab.plan_members
                WHERE plan_id = %s AND user_id IS NOT NULL
                ORDER BY joined_at LIMIT 12
            """, (demo_plan_id,))
            demo_members = cur.fetchall()
            member_map = {m['display_name'].replace('[BOT] ', ''): m['pk_id'] for m in demo_members}

            # Pick a few members to be the ones who front money
            payers = list(member_map.items())[:5]
            demo_expenses = [
                (payers[0][1], 'Hotel — The Scott Resort (3 nights)', 1971, 'lodging'),
                (payers[1][1], 'Desert Jeep Tour (12 people)', 480, 'activity'),
                (payers[2][1], 'Costco run — drinks, snacks, sunscreen', 215, 'food'),
                (payers[3][1], 'D-backs tickets (12 seats, Section 130)', 600, 'tickets'),
                (payers[0][1], 'Dinner reservation deposit — Citizen Public House', 350, 'food'),
                (payers[4][1], 'SUV rental (Turo, 4 days)', 320, 'transport'),
            ]
            for paid_by_id, title, amount, category in demo_expenses:
                cur.execute("""
                    INSERT INTO crab.expenses (plan_id, paid_by, title, amount, category, split_type)
                    VALUES (%s, %s, %s, %s, %s, 'equal')
                """, (demo_plan_id, paid_by_id, title, amount, category))

        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✅ Demo viewer {DEMO_VIEWER_NAME} seeded (user_id={judy_id}, joined {joined}, owns {owned})")
        return jsonify({'success': True, 'user_id': judy_id, 'plans_joined': joined, 'plans_owned': owned, 'total_plans': len(plans)})
    except Exception as e:
        logger.error(f"❌ Seed demo viewer failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/tasks/seed-booked-trips')
def task_seed_booked_trips():
    """Promote a handful of bot-generated plans from 'locked' to 'booked' so /live has content."""
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403

    TARGET_BOOKED = 5  # how many plans to promote
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        import random as _rnd
        from datetime import timedelta

        def _humanize_watches(cur, plan_id):
            """Seed realistic prices + varied dates + flight times on watches.
            Real humans fly in/out on different days/times and don't all stay the full trip."""
            import json as _json
            # Get trip date range
            cur.execute("SELECT start_date, end_date FROM crab.plans WHERE plan_id = %s", (plan_id,))
            plan_row = cur.fetchone()
            trip_start = plan_row['start_date'] if plan_row and plan_row.get('start_date') else None
            trip_end = plan_row['end_date'] if plan_row and plan_row.get('end_date') else None
            trip_days = (trip_end - trip_start).days if trip_start and trip_end else 3

            # Realistic flight time windows (hour, minute) — weighted toward common slots
            DEPART_SLOTS = [
                (6, 0), (6, 30), (7, 15), (7, 45), (8, 20), (9, 0), (9, 50),
                (10, 30), (11, 15), (12, 0), (13, 10), (14, 0), (14, 45),
                (15, 30), (16, 15), (17, 0), (17, 45), (18, 30), (19, 20), (20, 15),
            ]

            def _rand_flight_time():
                h, m = _rnd.choice(DEPART_SLOTS)
                return f"{h:02d}:{m:02d}"

            def _arrival_time(dep_str, flight_hrs):
                dh, dm = int(dep_str[:2]), int(dep_str[3:])
                total = dh * 60 + dm + int(flight_hrs * 60)
                ah, am = (total // 60) % 24, total % 60
                return f"{ah:02d}:{am:02d}"

            cur.execute("""
                SELECT pk_id, watch_type, checkin, checkout
                FROM crab.member_watches WHERE plan_id = %s
            """, (plan_id,))
            watches = cur.fetchall()
            for w in watches:
                is_flight = w['watch_type'] == 'flight'
                price = _rnd.randint(180, 650) if is_flight else _rnd.randint(120, 400)
                conf = f"CRAB{_rnd.randint(100000, 999999)}"

                # Randomize dates — ~50% get varied dates for realism
                new_checkin = w['checkin']
                new_checkout = w['checkout']
                if trip_start and trip_end and trip_days >= 3:
                    roll = _rnd.random()
                    if is_flight:
                        if roll < 0.25:
                            new_checkin = trip_start - timedelta(days=1)
                        elif roll < 0.45:
                            new_checkin = trip_start + timedelta(days=1)
                        roll2 = _rnd.random()
                        if roll2 < 0.30:
                            new_checkout = trip_end - timedelta(days=1)
                        elif roll2 < 0.40:
                            new_checkout = trip_end + timedelta(days=1)
                    else:
                        if roll < 0.25:
                            new_checkin = trip_start - timedelta(days=1)
                        elif roll < 0.45:
                            new_checkin = trip_start + timedelta(days=1)
                        roll2 = _rnd.random()
                        if roll2 < 0.30:
                            new_checkout = trip_end - timedelta(days=1)
                        elif roll2 < 0.40:
                            new_checkout = trip_end + timedelta(days=1)
                        if _rnd.random() < 0.10 and trip_days >= 4:
                            mid = trip_start + timedelta(days=trip_days // 2)
                            if _rnd.random() < 0.5:
                                new_checkout = mid
                            else:
                                new_checkin = mid

                # Build data JSONB with times for flights
                extra_data = {'booked_price': price, 'confirmation': conf}
                if is_flight:
                    dep = _rand_flight_time()
                    flight_hrs = _rnd.uniform(1.5, 5.5)
                    arr = _arrival_time(dep, flight_hrs)
                    extra_data['departure_time'] = dep
                    extra_data['arrival_time'] = arr
                    # Return flight gets its own times
                    ret_dep = _rand_flight_time()
                    ret_arr = _arrival_time(ret_dep, flight_hrs + _rnd.uniform(-0.5, 0.5))
                    extra_data['return_departure_time'] = ret_dep
                    extra_data['return_arrival_time'] = ret_arr

                cur.execute("""
                    UPDATE crab.member_watches
                    SET status = 'booked',
                        best_price_usd = %s, last_price_usd = %s,
                        best_price_at = NOW(), last_checked_at = NOW(),
                        checkin = COALESCE(%s, checkin),
                        checkout = COALESCE(%s, checkout),
                        data = COALESCE(data, '{}') || %s::jsonb
                    WHERE pk_id = %s
                """, (price, price, new_checkin, new_checkout, _json.dumps(extra_data), w['pk_id']))
            return len(watches)

        # Fix existing booked plans missing flight times, varied dates, or $0 watches (batch of 10)
        cur.execute("""
            SELECT DISTINCT p.plan_id FROM crab.plans p
            JOIN crab.member_watches w ON w.plan_id = p.plan_id
            WHERE p.title LIKE '[BOT]%%' AND p.status = 'booked'
              AND (w.status = 'active' OR COALESCE(w.best_price_usd, 0) = 0
                   OR (w.watch_type = 'flight' AND NOT jsonb_exists(COALESCE(w.data, '{}'), 'departure_time')))
            LIMIT 25
        """)
        stale_plans = [r['plan_id'] for r in cur.fetchall()]
        stale_fixed = 0
        for pid in stale_plans:
            stale_fixed += _humanize_watches(cur, pid)
        if stale_fixed:
            logger.info(f"Humanized {stale_fixed} watches on {len(stale_plans)} existing booked plans")

        # Get all booked bot plans for itinerary seeding below
        cur.execute("""
            SELECT DISTINCT p.plan_id FROM crab.plans p
            WHERE p.title LIKE '[BOT]%%' AND p.status = 'booked'
        """)
        all_booked = [r['plan_id'] for r in cur.fetchall()]

        # Commit watches first so itinerary failures don't roll them back
        conn.commit()

        # Seed itineraries on booked plans that don't have one yet
        # Each trip gets its own connection so failures are isolated
        itineraries_added = 0
        from utilities.kumori_free_llms import generate as llm_generate
        import json as _json
        from datetime import date as _date
        import re as _re

        MAX_ITINERARIES_PER_RUN = 10  # YouTube quota now 100k/day (approved 2026-04-06); LLM is the bottleneck
        for pid in all_booked:
            if itineraries_added >= MAX_ITINERARIES_PER_RUN:
                break
            iconn = None
            try:
                iconn = get_db_connection()
                icur = iconn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                icur.execute("SELECT COUNT(*) as cnt FROM crab.itinerary_items WHERE plan_id = %s", (pid,))
                if icur.fetchone()['cnt'] > 0:
                    icur.close(); iconn.close()
                    continue
                icur.execute("""
                    SELECT p.title, p.destination, p.start_date, p.end_date,
                           br.summary->>'destinations' as summary_dests
                    FROM crab.plans p
                    LEFT JOIN crab.bot_runs br ON br.plan_id = p.plan_id
                    WHERE p.plan_id = %s LIMIT 1
                """, (pid,))
                pinfo = icur.fetchone()
                if not pinfo:
                    icur.close(); iconn.close()
                    continue
                dest = pinfo.get('destination') or ''
                if not dest and pinfo.get('summary_dests'):
                    try:
                        dests_list = _json.loads(pinfo['summary_dests'])
                        dest = dests_list[0] if dests_list else ''
                    except Exception:
                        pass
                if not dest:
                    icur.close(); iconn.close()
                    continue
                trip_title = (pinfo['title'] or '').replace('[BOT] ', '')
                s_date = pinfo['start_date'] or _date(2026, 5, 20)
                e_date = pinfo['end_date'] or (s_date + timedelta(days=3))
                num_days = max((e_date - s_date).days, 2)

                prompt = f"""Generate a {num_days}-day itinerary for a group trip to {dest}. Trip: "{trip_title}", dates {s_date} to {e_date}.

Create 3-5 items per day. Respond ONLY with a JSON array (no extra text):
[{{"day":1,"time":"09:00","title":"item","category":"activity","duration":60,"location":"place","notes":"note"}}]

Categories: activity, food, transport, culture, nightlife, relaxation.
Use REAL place names and restaurants for {dest}. Day 1 = arrival. Last day = departure."""

                # Try up to 2 LLM calls if JSON parsing fails
                items = None
                for attempt in range(2):
                    text, backend = llm_generate(prompt, max_tokens=1500, temperature=0.9)
                    if not text:
                        continue
                    try:
                        # Extract JSON from response
                        if '```' in text:
                            text = text.split('```')[1]
                            if text.startswith('json'):
                                text = text[4:]
                        # Try to find the JSON array even if there's extra text
                        match = _re.search(r'\[.*\]', text, _re.DOTALL)
                        if match:
                            text = match.group(0)
                        items = _json.loads(text.strip())
                        if isinstance(items, list) and len(items) > 0:
                            break
                        items = None
                    except (_json.JSONDecodeError, Exception):
                        items = None

                if not items:
                    icur.close(); iconn.close()
                    continue

                for item in items:
                    day_num = item.get('day', 1)
                    if not isinstance(day_num, int):
                        day_num = 1
                    sched_date = s_date + timedelta(days=min(day_num - 1, num_days - 1))
                    time_str = item.get('time', '')
                    # Validate time format
                    if time_str and not _re.match(r'^\d{1,2}:\d{2}', str(time_str)):
                        time_str = None
                    icur.execute("""
                        INSERT INTO crab.itinerary_items
                            (plan_id, title, category, scheduled_date, scheduled_time,
                             duration_minutes, location, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        pid,
                        str(item.get('title', 'Activity'))[:200],
                        str(item.get('category', 'activity'))[:30],
                        sched_date, time_str,
                        item.get('duration') if isinstance(item.get('duration'), (int, float)) else None,
                        str(item.get('location', ''))[:200],
                        str(item.get('notes', ''))[:500],
                    ))
                iconn.commit()
                itineraries_added += 1
                logger.info(f"  📅 Itinerary seeded for {trip_title}: {len(items)} items via {backend}")
            except Exception as e:
                logger.warning(f"  ⚠️ Itinerary failed for {pid}: {e}")
                if iconn:
                    try:
                        iconn.rollback()
                    except Exception:
                        pass
            finally:
                if iconn:
                    try:
                        iconn.close()
                    except Exception:
                        pass

        # Find bot-generated plans at 'locked' status that have bot_runs with 'passed'
        cur.execute("""
            SELECT p.plan_id, p.title, p.status
            FROM crab.plans p
            JOIN crab.bot_runs br ON br.plan_id = p.plan_id
            WHERE p.status = 'locked' AND br.status = 'passed'
            ORDER BY br.finished_at DESC
            LIMIT %s
        """, (TARGET_BOOKED,))
        candidates = cur.fetchall()

        promoted = []
        for plan in candidates:
            cur.execute("UPDATE crab.plans SET status = 'booked' WHERE plan_id = %s", (plan['plan_id'],))
            n = _humanize_watches(cur, plan['plan_id'])
            promoted.append({'plan_id': str(plan['plan_id']), 'title': plan['title'], 'watches_booked': n})

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'success': True, 'promoted': promoted, 'count': len(promoted),
                        'stale_fixed': stale_fixed, 'itineraries_added': itineraries_added})
    except Exception as e:
        logger.error(f"Seed booked trips failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/tasks/seed-demo-chat')
def task_seed_demo_chat():
    """Use the LLM router to generate fun chat messages for demo trips."""
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403

    from utilities.kumori_free_llms import generate as llm_generate
    results = {}

    demo_trip_configs = {
        'qL6zhRAI': {
            'stage': 'booked',
            'context': 'A group of 12 friends just booked a trip to Scottsdale, AZ for May 20-23. They voted on 4 destinations (Scottsdale won over Sagano Japan, Salvador Brazil, and Lapland Finland). Flights and hotels are booked. They\'re excited and planning activities.',
        },
        'xL2aRt-k': {
            'stage': 'voting',
            'context': 'A group of 50 coworkers is voting on where to go. The 3 options are Reykjavik Iceland, Marrakech Morocco, and Luang Prabang Laos. Voting is still open. People are debating and campaigning for their favorites.',
        },
        'TpPeETPm': {
            'stage': 'planning',
            'context': 'A group of 75 adventure lovers chose to explore the Andes — Ushuaia Argentina is the destination. Flights are being researched, people are figuring out logistics, packing, and activities like glacier treks.',
        },
    }

    for trip_token, config in demo_trip_configs.items():
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            cur.execute("SELECT plan_id, title FROM crab.plans WHERE invite_token = %s", (trip_token,))
            plan = cur.fetchone()
            if not plan:
                results[trip_token] = 'plan not found'
                cur.close(); conn.close()
                continue

            # Get member names
            cur.execute("""
                SELECT display_name, user_id FROM crab.plan_members
                WHERE plan_id = %s AND user_id IS NOT NULL
                ORDER BY joined_at LIMIT 15
            """, (plan['plan_id'],))
            members = cur.fetchall()
            member_names = [m['display_name'].replace('[BOT] ', '') for m in members]
            name_to_uid = {m['display_name'].replace('[BOT] ', ''): m['user_id'] for m in members}

            # Make sure Judy is in the list
            if DEMO_VIEWER_NAME not in member_names:
                member_names.append(DEMO_VIEWER_NAME)

            prompt = f"""Generate a group chat thread for a trip planning app. The trip: "{plan['title']}"

Context: {config['context']}

Members in the chat: {', '.join(member_names[:12])}

Write 15-20 chat messages as a JSON array. Each message is {{"name": "FirstName LastName", "text": "message"}}.

Rules:
- Use the EXACT member names listed above
- Include Judy Tunaboat in 2-3 messages
- Make it feel like a real group chat — casual, fun, some jokes, some practical planning
- Include at least 2-3 genuinely funny/witty messages (dry humor, playful roasts, running jokes)
- Mix short messages (1-5 words) with longer ones
- Some people should reply to each other, reference what others said
- No emojis in every message — use them sparingly like real people
- Stage is "{config['stage']}" so the tone should match (voting=debating, planning=logistics, booked=excited/celebrating)

Return ONLY the JSON array, no other text."""

            text, backend = llm_generate(prompt, max_tokens=1500, temperature=0.9, caller='demo_chat_seed')

            if not text:
                results[trip_token] = f'LLM returned None (backend={backend})'
                cur.close(); conn.close()
                continue

            # Parse the JSON from LLM response
            import re
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if not json_match:
                results[trip_token] = f'No JSON array found in LLM response (backend={backend}, len={len(text)})'
                cur.close(); conn.close()
                continue

            messages = json.loads(json_match.group(0))

            # Clear existing messages and insert new ones
            cur.execute("DELETE FROM crab.messages WHERE plan_id = %s", (plan['plan_id'],))

            inserted = 0
            for msg in messages:
                name = msg.get('name', '')
                text_content = msg.get('text', '')
                if not name or not text_content:
                    continue
                uid = name_to_uid.get(name, name_to_uid.get(DEMO_VIEWER_NAME))
                if not uid:
                    uid = members[0]['user_id'] if members else 1
                cur.execute("""
                    INSERT INTO crab.messages (plan_id, user_id, display_name, content)
                    VALUES (%s, %s, %s, %s)
                """, (plan['plan_id'], uid, name, text_content))
                inserted += 1

            conn.commit()
            cur.close()
            conn.close()
            results[trip_token] = f'OK: {inserted} messages via {backend}'

        except Exception as e:
            results[trip_token] = f'error: {str(e)[:200]}'

    return jsonify({'success': True, 'results': results})


# ── II catalog scraper ──────────────────────────────────────────────

def _ii_scrape_auth_ok():
    """Allow when called by App Engine cron OR by a logged-in admin OR with the task secret."""
    if request.headers.get('X-Appengine-Cron') == 'true':
        return True
    if session.get('user_is_admin'):
        return True
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    if request.args.get('secret') == task_secret and task_secret != 'dev':
        return True
    return False


@bp.route('/tasks/ii-scrape-seed', methods=['GET', 'POST'])
def ii_scrape_seed():
    if not _ii_scrape_auth_ok():
        return jsonify({'error': 'forbidden'}), 403
    from utilities.timeshare_ii_scraper import start_run
    try:
        run_id = start_run(triggered_by='cron' if request.headers.get('X-Appengine-Cron') else 'admin')
        return jsonify({'ok': True, 'run_id': run_id})
    except Exception as e:
        logger.exception(f'ii-scrape-seed failed: {e}')
        return jsonify({'error': str(e).split(chr(10))[0][:200]}), 500


@bp.route('/tasks/ii-scrape-next', methods=['GET', 'POST'])
def ii_scrape_next():
    if not _ii_scrape_auth_ok():
        return jsonify({'error': 'forbidden'}), 403
    from utilities.timeshare_ii_scraper import process_next
    try:
        # Default: 1 region per call = ~3-5 min of work. Admin can pass ?max=N.
        max_regions = int(request.args.get('max', 1))
        max_regions = max(1, min(max_regions, 5))   # hard cap: 5 per call
        summary = process_next(max_regions=max_regions)
        return jsonify({'ok': True, **summary})
    except Exception as e:
        logger.exception(f'ii-scrape-next failed: {e}')
        return jsonify({'error': str(e).split(chr(10))[0][:200]}), 500
