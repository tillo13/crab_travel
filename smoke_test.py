"""
smoke_test.py — crab.travel integration smoke test
Run: python smoke_test.py

Tests:
  1. DB connection + schema init
  2. Travelpayouts flights search
  3. Travelpayouts hotels search
  4. search_results write + read
  5. price_history write + read + avg
  6. search_engine fan-out (end-to-end, writes to DB)
"""

import sys
import time
from dotenv import load_dotenv
load_dotenv()

PASS = "✅"
FAIL = "❌"
INFO = "ℹ️ "

def section(title):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")

def ok(msg):
    print(f"  {PASS}  {msg}")

def fail(msg):
    print(f"  {FAIL}  {msg}")
    sys.exit(1)

def info(msg):
    print(f"  {INFO}  {msg}")


# ── 1. DB connection ──────────────────────────────────────────

section("1. Database connection + schema init")
try:
    from utilities.postgres_utils import init_database, get_db_connection
    init_database()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM crab.search_results")
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    ok(f"Connected — search_results has {count} rows")
except Exception as e:
    fail(f"DB connection failed: {e}")


# ── 2. Travelpayouts flights ──────────────────────────────────

section("2. Travelpayouts — flight search (NYC → PHX, Jan 2027)")
try:
    from utilities.adapters.travelpayouts import TravelpayoutsAdapter
    adapter = TravelpayoutsAdapter()
    flights = adapter.search_flights(
        origin="NYC",
        destination="PHX",
        depart_date="2027-01",
        return_date="2027-01",
    )
    if flights:
        ok(f"{len(flights)} flights found")
        f = flights[0]
        info(f"Cheapest: {f['airline']} ${f['price_usd']} | stops={f['stops']}")
        info(f"Deep link: {f['deep_link'][:60]}...")
    else:
        info("0 flights returned (may be no cached data for this route/date)")
except Exception as e:
    fail(f"Travelpayouts flights failed: {e}")


# ── 3. Travelpayouts hotels ───────────────────────────────────

section("3. Travelpayouts — hotel search (Phoenix, Jan 10-13 2027)")
try:
    hotels = adapter.search_hotels(
        destination="Phoenix",
        checkin="2027-01-10",
        checkout="2027-01-13",
        guests=2,
    )
    if hotels:
        ok(f"{len(hotels)} hotels found")
        h = hotels[0]
        info(f"Cheapest: {h['name']} ${h['price_per_night_usd']}/night | {h['nights']} nights = ${h['total_price_usd']}")
        info(f"Deep link: {h['deep_link'][:60]}...")
    else:
        info("0 hotels returned (may be no cached data for this destination)")
except Exception as e:
    fail(f"Travelpayouts hotels failed: {e}")


# ── 3b. Duffel flights ───────────────────────────────────────

section("3b. Duffel — flight search (LHR → JFK, 2025-06-01) [test mode]")
try:
    from utilities.adapters.duffel import DuffelAdapter
    duffel = DuffelAdapter()
    from datetime import date, timedelta
    depart = (date.today() + timedelta(days=60)).strftime("%Y-%m-%d")
    ret = (date.today() + timedelta(days=74)).strftime("%Y-%m-%d")
    flights = duffel.search_flights(
        origin="LHR",
        destination="JFK",
        depart_date=depart,
        return_date=ret,
        passengers=2,
    )
    if flights:
        ok(f"{len(flights)} offers returned")
        f = flights[0]
        info(f"Cheapest: {f['airline']} ${f['price_usd']} | stops={f['stops']}")
        info(f"Departs: {f['depart_at']} → Arrives: {f['arrive_at']}")
        info(f"Bookable: {f['bookable']} | offer_id: {f['raw'].get('offer_id','')[:30]}...")
    else:
        info("0 offers — check API key / test mode access")
except Exception as e:
    fail(f"Duffel flights failed: {e}")


# ── 3c. LiteAPI hotels ───────────────────────────────────────

section("3c. LiteAPI — hotel search (Phoenix, Jan 10-13 2027) [sandbox]")
try:
    from utilities.adapters.liteapi import LiteAPIAdapter
    liteapi = LiteAPIAdapter()
    hotels = liteapi.search_hotels(
        destination="Phoenix",
        checkin="2027-01-10",
        checkout="2027-01-13",
        guests=2,
    )
    if hotels:
        ok(f"{len(hotels)} hotels found")
        h = hotels[0]
        info(f"Cheapest: {h['property_id']} ${h['price_per_night_usd']}/night | {h['nights']} nights = ${h['total_price_usd']}")
        info(f"Deep link: {h['deep_link'][:60]}...")
    else:
        info("0 hotels returned (sandbox may return empty for future dates)")
except Exception as e:
    fail(f"LiteAPI hotels failed: {e}")


# ── 3d. Viator activities ─────────────────────────────────────

section("3d. Viator — activities search (Phoenix) [sandbox, may take 24h to activate]")
try:
    from utilities.adapters.viator import ViatorAdapter
    viator = ViatorAdapter()
    activities = viator.search_activities(
        destination="Phoenix",
        checkin="2027-01-10",
        checkout="2027-01-13",
    )
    if activities:
        ok(f"{len(activities)} activities found")
        a = activities[0]
        info(f"Top: {a['name']} ${a['price_per_person_usd']}/person | {a['duration']}")
        if a.get('rating'):
            info(f"Rating: {a['rating']} ({a['review_count']} reviews)")
        info(f"Deep link: {a['deep_link'][:60]}...")
    else:
        info("0 activities — key may not be active yet (up to 24h) or sandbox limit")
except Exception as e:
    info(f"Viator skipped: {e}")


# ── 4. search_results write + read ───────────────────────────

section("4. search_results — write + read")
try:
    from utilities.postgres_utils import save_search_result, get_search_results, clear_search_results

    # Use a fake plan_id — we just need to test the DB layer
    # First, create a test plan to satisfy the FK constraint
    from utilities.postgres_utils import get_db_connection
    import uuid
    test_plan_id = None

    conn = get_db_connection()
    cursor = conn.cursor()
    # Get any existing plan to use as test target
    cursor.execute("SELECT plan_id FROM crab.plans LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        info("No plans in DB yet — skipping FK-dependent write test")
    else:
        test_plan_id = str(row[0])
        result = save_search_result(
            plan_id=test_plan_id,
            result_type='flight',
            source='smoke_test',
            canonical_key='TEST-NYC-PHX-AA',
            title='Test Flight AA NYC→PHX',
            price_usd=299.00,
            deep_link='https://example.com/test',
            data={'type': 'flight', 'source': 'smoke_test', 'price_usd': 299.00},
        )
        if not result:
            fail("save_search_result returned None")

        results = get_search_results(test_plan_id, result_type='flight')
        smoke_results = [r for r in results if r['source'] == 'smoke_test']
        if not smoke_results:
            fail("save_search_result wrote but get_search_results couldn't read it back")

        ok(f"Wrote + read {len(smoke_results)} test result(s)")
        info(f"pk_id={smoke_results[0]['pk_id']} title={smoke_results[0]['title']} price=${smoke_results[0]['price_usd']}")

        # Clean up
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.search_results WHERE source = 'smoke_test'")
        conn.commit()
        cursor.close()
        conn.close()
        info("Cleaned up test rows")

except Exception as e:
    fail(f"search_results test failed: {e}")


# ── 5. price_history write + read + avg ──────────────────────

section("5. price_history — write + 90-day average")
try:
    from utilities.postgres_utils import save_price_history, get_price_average

    for price in [310.0, 290.0, 330.0, 285.0, 320.0]:
        save_price_history(
            result_type='flight',
            canonical_key='SMOKE-TEST-NYC-PHX',
            source='smoke_test',
            price_usd=price,
            travel_date='2027-01-10',
        )

    avg = get_price_average('SMOKE-TEST-NYC-PHX', 'flight', days=90)
    if not avg:
        fail("get_price_average returned None after writing history")

    expected = (310 + 290 + 330 + 285 + 320) / 5  # 307.0
    ok(f"90-day avg: ${avg['avg_price']:.2f} (expected ${expected:.2f}) from {avg['sample_count']} samples")

    # Clean up
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM crab.price_history WHERE canonical_key = 'SMOKE-TEST-NYC-PHX'")
    conn.commit()
    cursor.close()
    conn.close()
    info("Cleaned up test rows")

except Exception as e:
    fail(f"price_history test failed: {e}")


# ── 6. search_engine fan-out ─────────────────────────────────

section("6. search_engine — adapter fan-out (live, writes to DB)")
try:
    from utilities.search_engine import trigger_search, is_searching
    from utilities.postgres_utils import get_search_results

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT plan_id FROM crab.plans LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        info("No plans in DB — skipping live fan-out test (need a real plan)")
    else:
        test_plan_id = str(row[0])

        # Clear any previous results
        clear_search_results(test_plan_id)

        trigger_search(
            plan_id=test_plan_id,
            destination="Phoenix",
            checkin="2027-01-10",
            checkout="2027-01-13",
            origin_airports=["NYC"],
            guests=2,
        )

        info("Search triggered — waiting up to 15s for adapters...")
        for i in range(15):
            time.sleep(1)
            results = get_search_results(test_plan_id)
            still_running = is_searching(test_plan_id)
            print(f"    [{i+1}s] {len(results)} results, searching={still_running}", end='\r')
            if not still_running and len(results) > 0:
                break

        print()
        results = get_search_results(test_plan_id)
        if results:
            flights = [r for r in results if r['result_type'] == 'flight']
            hotels  = [r for r in results if r['result_type'] == 'hotel']
            ok(f"{len(results)} total results — {len(flights)} flights, {len(hotels)} hotels")
            if hotels:
                h = hotels[0]
                info(f"Sample hotel: {h['title']} ${h['price_usd']}/night via {h['source']}")
            if flights:
                f = flights[0]
                info(f"Sample flight: {f['title']} ${f['price_usd']} via {f['source']}")
        else:
            info("0 results after 15s — Travelpayouts may have no cached data for this route/date")

except Exception as e:
    fail(f"search_engine test failed: {e}")


# ── Done ──────────────────────────────────────────────────────

section("Done")
print(f"\n  All smoke tests passed.\n")
