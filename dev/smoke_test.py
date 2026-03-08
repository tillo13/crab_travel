#!/usr/bin/env python3
"""
crab.travel smoke test — run from CLI to verify prod is healthy.

Usage:
    python dev/smoke_test.py              # Default: all tier 1+2 tests
    python dev/smoke_test.py --quick      # Tier 1 only (~10 critical tests)
    python dev/smoke_test.py --full       # All tiers including slow tests
    python dev/smoke_test.py --notify     # Test notification pipeline (email/sms)
    python dev/smoke_test.py --api        # API endpoints only
    python dev/smoke_test.py --speed      # Page speed benchmarks only
"""

import sys
import os
import time
import json
import argparse

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

PROD_URL = "https://crab.travel"
TIMEOUT = 15
SLOW_THRESHOLD = 3.0  # seconds
VERY_SLOW_THRESHOLD = 5.0

# Test accounts for authenticated tests
TEST_EMAIL = "andy.tillo@gmail.com"

# ─── Test registry ────────────────────────────────────────────────────────────

TESTS = []
PASSED = []
FAILED = []
SKIPPED = []
WARNINGS = []
TIMINGS = {}


def test(name, tier=2, tags=None):
    """Decorator to register a smoke test."""
    tags = tags or []
    def decorator(fn):
        fn._test_name = name
        fn._tier = tier
        fn._tags = tags
        TESTS.append(fn)
        return fn
    return decorator


def run_tests(max_tier=2, tag_filter=None):
    """Run all registered tests up to max_tier, optionally filtered by tag."""
    filtered = [t for t in TESTS if t._tier <= max_tier]
    if tag_filter:
        filtered = [t for t in filtered if tag_filter in t._tags]

    total = len(filtered)
    print(f"\n{'='*60}")
    print(f"  crab.travel smoke test — {total} tests (tier 1-{max_tier})")
    print(f"  Target: {PROD_URL}")
    print(f"{'='*60}\n")

    for i, t in enumerate(filtered, 1):
        name = t._test_name
        tags_str = f" [{','.join(t._tags)}]" if t._tags else ""
        try:
            start = time.time()
            result = t()
            elapsed = time.time() - start
            TIMINGS[name] = elapsed

            if result is True:
                print(f"  ✅  {name} ({elapsed:.2f}s){tags_str}")
                PASSED.append(name)
            elif isinstance(result, str):
                # Warning — passed but with note
                print(f"  ⚠️  {name}: {result} ({elapsed:.2f}s)")
                WARNINGS.append((name, result))
                PASSED.append(name)
            else:
                print(f"  ❌  {name}: unexpected result: {result}")
                FAILED.append((name, str(result)))
        except AssertionError as e:
            elapsed = time.time() - start
            TIMINGS[name] = elapsed
            print(f"  ❌  {name}: {e} ({elapsed:.2f}s)")
            FAILED.append((name, str(e)))
        except Exception as e:
            elapsed = time.time() - start
            TIMINGS[name] = elapsed
            print(f"  ❌  {name}: {type(e).__name__}: {e}")
            FAILED.append((name, f"{type(e).__name__}: {e}"))

    # Summary
    print(f"\n{'='*60}")
    print(f"  Results: {len(PASSED)} passed, {len(FAILED)} failed, "
          f"{len(SKIPPED)} skipped, {len(WARNINGS)} warnings")

    if TIMINGS:
        avg = sum(TIMINGS.values()) / len(TIMINGS)
        slowest = max(TIMINGS.items(), key=lambda x: x[1])
        print(f"  Avg response: {avg:.2f}s | Slowest: {slowest[0]} ({slowest[1]:.2f}s)")

    if FAILED:
        print(f"\n  FAILURES:")
        for name, err in FAILED:
            print(f"    ❌ {name}: {err}")

    if WARNINGS:
        print(f"\n  WARNINGS:")
        for name, msg in WARNINGS:
            print(f"    ⚠️  {name}: {msg}")

    print(f"{'='*60}\n")
    return len(FAILED) == 0


# =============================================================================
# TIER 1: CRITICAL — Site is up and serving pages
# =============================================================================

@test("Homepage loads", tier=1, tags=['page', 'critical'])
def test_homepage():
    resp = requests.get(PROD_URL, timeout=30)  # cold start tolerance
    assert resp.status_code == 200, f"Got {resp.status_code}"
    assert "crab.travel" in resp.text.lower() or "crab" in resp.text.lower(), "Missing branding"
    return True


@test("Health endpoint", tier=1, tags=['api', 'critical'])
def test_health():
    resp = requests.get(f"{PROD_URL}/health", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    return True


@test("Login page loads", tier=1, tags=['page', 'critical'])
def test_login():
    resp = requests.get(f"{PROD_URL}/login", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    assert "Sign in" in resp.text or "Google" in resp.text, "Missing login content"
    return True


@test("Privacy page loads", tier=1, tags=['page', 'critical'])
def test_privacy():
    resp = requests.get(f"{PROD_URL}/privacy", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    assert "Privacy" in resp.text, "Missing privacy content"
    return True


@test("Terms page loads", tier=1, tags=['page', 'critical'])
def test_terms():
    resp = requests.get(f"{PROD_URL}/terms", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    assert "Terms" in resp.text, "Missing terms content"
    return True


@test("Contact page loads", tier=1, tags=['page', 'critical'])
def test_contact():
    resp = requests.get(f"{PROD_URL}/contact", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    assert "Contact" in resp.text, "Missing contact content"
    return True


@test("Favicon loads", tier=1, tags=['static', 'critical'])
def test_favicon():
    resp = requests.get(f"{PROD_URL}/static/favicon.ico", timeout=TIMEOUT)
    if resp.status_code == 404:
        resp = requests.get(f"{PROD_URL}/static/favicon.png", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Favicon returned {resp.status_code}"
    return True


@test("Logo image loads", tier=1, tags=['static', 'critical'])
def test_logo():
    resp = requests.get(f"{PROD_URL}/static/images/crab_logo.png", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Logo returned {resp.status_code}"
    return True


@test("No 500 on homepage", tier=1, tags=['critical'])
def test_no_500():
    resp = requests.get(PROD_URL, timeout=TIMEOUT)
    assert resp.status_code != 500, "500 Internal Server Error"
    assert resp.status_code != 502, "502 Bad Gateway"
    assert resp.status_code != 503, "503 Service Unavailable"
    return True


# =============================================================================
# TIER 2: DEFAULT — Auth-required pages redirect, APIs respond, speed checks
# =============================================================================

@test("Dashboard redirects when not logged in", tier=2, tags=['page', 'auth'])
def test_dashboard_redirect():
    resp = requests.get(f"{PROD_URL}/dashboard", timeout=TIMEOUT, allow_redirects=False)
    assert resp.status_code in [302, 303], f"Dashboard returned {resp.status_code} (expected redirect)"
    return True


@test("Profile redirects when not logged in", tier=2, tags=['page', 'auth'])
def test_profile_redirect():
    resp = requests.get(f"{PROD_URL}/profile", timeout=TIMEOUT, allow_redirects=False)
    assert resp.status_code in [302, 303], f"Profile returned {resp.status_code} (expected redirect)"
    return True


@test("Plan new redirects when not logged in", tier=2, tags=['page', 'auth'])
def test_plan_new_redirect():
    resp = requests.get(f"{PROD_URL}/plan/new", timeout=TIMEOUT, allow_redirects=False)
    assert resp.status_code in [302, 303], f"Plan new returned {resp.status_code}"
    return True


@test("Airport resolver API", tier=2, tags=['api'])
def test_airport_resolve():
    resp = requests.get(f"{PROD_URL}/api/airport/resolve?q=Seattle", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    data = resp.json()
    assert data.get('result'), "No result returned"
    assert data['result']['code'] == 'SEA', f"Expected SEA, got {data['result']['code']}"
    return True


@test("Airport resolver — city alias", tier=2, tags=['api'])
def test_airport_alias():
    resp = requests.get(f"{PROD_URL}/api/airport/resolve?q=Scottsdale", timeout=TIMEOUT)
    data = resp.json()
    assert data.get('result'), "No result for Scottsdale"
    assert data['result']['code'] == 'PHX', f"Expected PHX, got {data['result']['code']}"
    return True


@test("Airport resolver — empty query", tier=2, tags=['api'])
def test_airport_empty():
    resp = requests.get(f"{PROD_URL}/api/airport/resolve?q=", timeout=TIMEOUT)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get('result') is None, "Should return null for empty query"
    return True


@test("Contact form rejects empty POST", tier=2, tags=['api'])
def test_contact_empty():
    resp = requests.post(f"{PROD_URL}/api/contact",
                         json={"email": "", "message": ""},
                         timeout=TIMEOUT)
    assert resp.status_code in [400, 422], f"Expected 400, got {resp.status_code}"
    return True


@test("Contact form honeypot rejection", tier=2, tags=['api'])
def test_contact_honeypot():
    resp = requests.post(f"{PROD_URL}/api/contact",
                         json={"email": "test@test.com", "message": "hi",
                               "honeypot": "gotcha", "time_open": 10000},
                         timeout=TIMEOUT)
    # Should silently reject or return 200 (to not tip off bots)
    assert resp.status_code in [200, 400], f"Got {resp.status_code}"
    return True


@test("YouTube search API", tier=2, tags=['api', 'media'])
def test_youtube_search():
    resp = requests.get(f"{PROD_URL}/api/youtube-search?q=things+to+do+in+seattle&max_results=2",
                        timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    data = resp.json()
    assert data.get('success') or isinstance(data, list), "Unexpected response format"
    return True


@test("Photo search API", tier=2, tags=['api', 'media'])
def test_photo_search():
    resp = requests.get(f"{PROD_URL}/api/photo-search?q=seattle+travel&per_page=2",
                        timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    data = resp.json()
    assert data.get('photos') is not None or isinstance(data, list), "Expected photos in response"
    return True


@test("Deals API loads", tier=2, tags=['api'])
def test_deals():
    resp = requests.get(f"{PROD_URL}/api/deals", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Got {resp.status_code}"
    return True


@test("SMS inbound webhook exists", tier=2, tags=['api', 'sms'])
def test_sms_inbound():
    # POST with no data — should not 500
    resp = requests.post(f"{PROD_URL}/api/sms/inbound", data={}, timeout=TIMEOUT)
    assert resp.status_code != 500, f"SMS inbound returned 500"
    return True


@test("HTTPS redirect", tier=2, tags=['infra'])
def test_https():
    try:
        resp = requests.get("http://crab.travel", timeout=TIMEOUT, allow_redirects=False)
        assert resp.status_code in [301, 302, 307, 308], f"HTTP returned {resp.status_code}"
        location = resp.headers.get('Location', '')
        assert 'https' in location.lower(), f"Not redirecting to HTTPS: {location}"
        return True
    except requests.exceptions.RequestException:
        SKIPPED.append("HTTPS redirect (network)")
        return "Could not test HTTP redirect"


# ─── Page speed benchmarks ────────────────────────────────────────────────────

@test("Homepage speed", tier=2, tags=['speed'])
def test_homepage_speed():
    start = time.time()
    resp = requests.get(PROD_URL, timeout=TIMEOUT)
    elapsed = time.time() - start
    if elapsed > VERY_SLOW_THRESHOLD:
        return f"Homepage: {elapsed:.2f}s (SLOW, >{VERY_SLOW_THRESHOLD}s)"
    if elapsed > SLOW_THRESHOLD:
        return f"Homepage: {elapsed:.2f}s (moderate, >{SLOW_THRESHOLD}s)"
    return True


@test("Login page speed", tier=2, tags=['speed'])
def test_login_speed():
    start = time.time()
    resp = requests.get(f"{PROD_URL}/login", timeout=TIMEOUT)
    elapsed = time.time() - start
    if elapsed > VERY_SLOW_THRESHOLD:
        return f"Login: {elapsed:.2f}s (SLOW)"
    if elapsed > SLOW_THRESHOLD:
        return f"Login: {elapsed:.2f}s (moderate)"
    return True


@test("Contact page speed", tier=2, tags=['speed'])
def test_contact_speed():
    start = time.time()
    resp = requests.get(f"{PROD_URL}/contact", timeout=TIMEOUT)
    elapsed = time.time() - start
    if elapsed > SLOW_THRESHOLD:
        return f"Contact: {elapsed:.2f}s (slow)"
    return True


@test("Airport API speed", tier=2, tags=['speed', 'api'])
def test_airport_speed():
    start = time.time()
    resp = requests.get(f"{PROD_URL}/api/airport/resolve?q=New+York", timeout=TIMEOUT)
    elapsed = time.time() - start
    if elapsed > 1.0:
        return f"Airport resolve: {elapsed:.2f}s (should be <1s)"
    return True


# =============================================================================
# TIER 3: FULL — DB checks, notification pipeline, invite flow, deep tests
# =============================================================================

@test("Database connectivity (via health)", tier=3, tags=['db'])
def test_db():
    """Health endpoint should verify DB if implemented."""
    resp = requests.get(f"{PROD_URL}/health", timeout=TIMEOUT)
    assert resp.status_code == 200
    return True


@test("Invite link with bad token returns page", tier=3, tags=['page'])
def test_bad_invite():
    resp = requests.get(f"{PROD_URL}/to/FAKEINVITETOKEN123", timeout=TIMEOUT, allow_redirects=False)
    # Should return something graceful, not 500
    assert resp.status_code != 500, f"Bad invite token caused 500"
    return True


@test("Multiple airport resolves (batch speed)", tier=3, tags=['speed', 'api'])
def test_airport_batch():
    cities = ["Phoenix", "Brooklyn", "Jamaica", "Vegas", "NOLA", "Austin",
              "Nashville", "Denver", "Portland", "Miami"]
    start = time.time()
    results = {}
    for city in cities:
        resp = requests.get(f"{PROD_URL}/api/airport/resolve?q={city}", timeout=TIMEOUT)
        data = resp.json()
        if data.get('result'):
            results[city] = data['result']['code']
    elapsed = time.time() - start
    resolved = len(results)
    if resolved < 8:
        return f"Only resolved {resolved}/{len(cities)} cities: {results}"
    if elapsed > 5.0:
        return f"Batch resolve took {elapsed:.2f}s for {len(cities)} cities (slow)"
    return True


@test("All public pages under 5s", tier=3, tags=['speed'])
def test_all_pages_speed():
    pages = ['/', '/login', '/privacy', '/terms', '/contact']
    slow = []
    for page in pages:
        start = time.time()
        resp = requests.get(f"{PROD_URL}{page}", timeout=TIMEOUT)
        elapsed = time.time() - start
        if elapsed > VERY_SLOW_THRESHOLD:
            slow.append(f"{page}: {elapsed:.2f}s")
    if slow:
        return f"Slow pages: {', '.join(slow)}"
    return True


@test("Static assets cached (Cache-Control header)", tier=3, tags=['infra'])
def test_cache_headers():
    resp = requests.get(f"{PROD_URL}/static/images/crab_logo.png", timeout=TIMEOUT)
    cache = resp.headers.get('Cache-Control', '')
    if not cache:
        return "No Cache-Control header on static assets"
    return True


@test("Google fonts load", tier=3, tags=['static'])
def test_google_fonts():
    resp = requests.get("https://fonts.googleapis.com/css2?family=DM+Serif+Display&display=swap", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Google Fonts returned {resp.status_code}"
    return True


@test("Tailwind CDN loads", tier=3, tags=['static'])
def test_tailwind():
    resp = requests.get("https://cdn.tailwindcss.com", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Tailwind CDN returned {resp.status_code}"
    return True


# =============================================================================
# NOTIFICATION TESTS — only run with --notify flag
# =============================================================================

@test("Email notification pipeline", tier=99, tags=['notify'])
def test_email_notification():
    """Test the full email notification path by checking DB prefs and sending a test."""
    from dotenv import load_dotenv
    load_dotenv()

    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Check notification prefs for test user
    cur.execute("""
        SELECT pk_id, email, notify_chat, notify_updates, notify_channel, phone_number
        FROM crab.users WHERE LOWER(email) = LOWER(%s)
    """, (TEST_EMAIL,))
    user = cur.fetchone()
    assert user, f"Test user {TEST_EMAIL} not found in DB"

    prefs = {
        'notify_chat': user['notify_chat'],
        'notify_updates': user['notify_updates'],
        'notify_channel': user['notify_channel'],
        'phone': user['phone_number'],
    }
    print(f"\n         User: {user['email']} (pk_id={user['pk_id']})")
    print(f"         Prefs: chat={prefs['notify_chat']}, updates={prefs['notify_updates']}, "
          f"channel={prefs['notify_channel']}, phone={prefs['phone'] or 'none'}")

    # Check if they're in any plans
    cur.execute("""
        SELECT m.plan_id, p.title
        FROM crab.plan_members m
        JOIN crab.plans p ON p.plan_id = m.plan_id
        WHERE m.user_id = %s LIMIT 3
    """, (user['pk_id'],))
    plans = cur.fetchall()
    print(f"         Plans: {len(plans)} — {[p['title'] for p in plans]}")

    cur.close()
    conn.close()

    if prefs['notify_chat'] == 'off' and prefs['notify_updates'] == 'off':
        return f"Notifications are OFF for {TEST_EMAIL} — enable in profile to test delivery"

    # Test email sending directly
    from utilities.gmail_utils import send_simple_email
    sent = send_simple_email(
        subject="[crab.travel] Smoke test — email notifications working",
        body="This is an automated smoke test confirming the email notification pipeline is functional.\n\n— crab.travel smoke test",
        to_email=TEST_EMAIL,
    )
    assert sent, "Email send failed"
    return True


@test("SMS notification pipeline", tier=99, tags=['notify'])
def test_sms_notification():
    """Test SMS send capability (may fail if A2P not approved yet)."""
    from dotenv import load_dotenv
    load_dotenv()

    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT phone_number, notify_channel FROM crab.users WHERE LOWER(email) = LOWER(%s)", (TEST_EMAIL,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user or not user['phone_number']:
        return f"No phone number for {TEST_EMAIL} — add in profile to test SMS"

    if user['notify_channel'] not in ('sms', 'both'):
        return f"Channel is '{user['notify_channel']}', not sms/both — change in profile to test"

    from utilities.sms_utils import send_sms
    result = send_sms(user['phone_number'], "[crab.travel] Smoke test — SMS pipeline working")
    if result:
        return True
    return "SMS send returned None — check Twilio logs (A2P may still be pending)"


@test("Notification query (no UUID error)", tier=99, tags=['notify'])
def test_notification_query():
    """Verify the notification query doesn't crash on UUID plan IDs."""
    from dotenv import load_dotenv
    load_dotenv()

    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get a real plan ID
    cur.execute("SELECT plan_id, title FROM crab.plans LIMIT 1")
    plan = cur.fetchone()
    if not plan:
        cur.close()
        conn.close()
        return "No plans in DB to test"

    plan_id = str(plan['plan_id'])
    print(f"\n         Testing plan: {plan['title']} ({plan_id})")

    # Run the same query the notification system uses
    try:
        cur.execute("""
            SELECT DISTINCT u.pk_id, u.full_name, u.email, u.notify_chat, u.notify_channel
            FROM crab.plan_members m
            JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s::uuid
              AND u.notify_chat = 'realtime'
              AND u.notify_channel IN ('email', 'both')
        """, (plan_id,))
        email_members = cur.fetchall()

        cur.execute("""
            SELECT DISTINCT u.pk_id, u.full_name, u.phone_number, u.notify_chat, u.notify_channel
            FROM crab.plan_members m
            JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s::uuid
              AND u.phone_number IS NOT NULL
              AND u.phone_number != ''
              AND u.notify_chat = 'realtime'
              AND u.notify_channel IN ('sms', 'both')
        """, (plan_id,))
        sms_members = cur.fetchall()

        cur.execute("SELECT title FROM crab.plans WHERE plan_id = %s::uuid", (plan_id,))
        title_row = cur.fetchone()

        print(f"         Email recipients: {len(email_members)} — {[m['full_name'] for m in email_members]}")
        print(f"         SMS recipients: {len(sms_members)} — {[m['full_name'] for m in sms_members]}")
        print(f"         Plan title lookup: {title_row['title'] if title_row else 'FAILED'}")

    except Exception as e:
        cur.close()
        conn.close()
        raise AssertionError(f"Query failed: {e}")

    cur.close()
    conn.close()
    return True


# =============================================================================
# CLI
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='crab.travel smoke test')
    parser.add_argument('--quick', action='store_true', help='Tier 1 only (critical)')
    parser.add_argument('--full', action='store_true', help='All tiers')
    parser.add_argument('--notify', action='store_true', help='Run notification pipeline tests')
    parser.add_argument('--api', action='store_true', help='API tests only')
    parser.add_argument('--speed', action='store_true', help='Speed benchmarks only')
    args = parser.parse_args()

    if args.quick:
        success = run_tests(max_tier=1)
    elif args.notify:
        success = run_tests(max_tier=99, tag_filter='notify')
    elif args.api:
        success = run_tests(max_tier=3, tag_filter='api')
    elif args.speed:
        success = run_tests(max_tier=3, tag_filter='speed')
    elif args.full:
        success = run_tests(max_tier=3)
    else:
        success = run_tests(max_tier=2)

    sys.exit(0 if success else 1)
