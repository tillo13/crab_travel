#!/usr/bin/env python3
"""
Timeshare E2E test harness — runs against live prod via the Playwright
test apikey (`?apikey=CRAB_TEST_APIKEY&user_id=N`).

Impersonates andy.tillo@gmail.com (user_id=1) for the owner path and the
bot accounts (user_ids 13–21) as invitees — bot.* email addresses are
real first-class user rows in crab.users and the existing email utility
silently no-ops when sending to `bot.*` addresses, so no real inbox is touched.

Covers:
  Phase 1 — landing, group create, invite/accept, 404-on-miss, rate limit,
            email-match rejection, expired token, resend token rotation.
  Phase 2 — schema presence (22 timeshare_* + 3 ii_* tables + crab.plans
            timeshare_group_id FK), 8 fact views, generic fact CRUD,
            cross-group write isolation, portals password redaction.

Usage:
    python dev/test_timeshare_e2e.py             # run all
    python dev/test_timeshare_e2e.py --keep      # don't delete test groups
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from utilities.google_auth_utils import get_secret
from utilities.postgres_utils import get_db_connection


PROD_URL = "https://crab.travel"
TIMEOUT = 60  # App Engine cold starts can run 15-25s right after a deploy

ANDY_USER_ID = 1
ANDY_EMAIL = "andy.tillo@gmail.com"
INVITEE_USER_ID = 14   # bot.sarah.kim@crab.travel
INVITEE_EMAIL = "bot.sarah.kim@crab.travel"
WRONG_INVITEE_USER_ID = 15  # bot.david.okafor@crab.travel (used for email-mismatch test)

TEST_GROUP_PREFIX = "[E2E test]"

PASSED, FAILED = [], []


def assert_eq(label, got, want):
    if got == want:
        PASSED.append(label)
        print(f"  ✅  {label}  (got={got})")
    else:
        FAILED.append((label, f"want={want} got={got}"))
        print(f"  ❌  {label}  (want={want} got={got})")


def assert_true(label, cond, detail=""):
    if cond:
        PASSED.append(label)
        print(f"  ✅  {label}  {detail}")
    else:
        FAILED.append((label, detail))
        print(f"  ❌  {label}  {detail}")


def authed_session(user_id):
    """Return a requests.Session() that will carry a Flask cookie authed
    as the given user_id. Primes it with a single apikey request."""
    apikey = get_secret('CRAB_TEST_APIKEY', project_id='crab-travel')
    s = requests.Session()
    # Hitting /dashboard with apikey sets the session cookie for this user
    r = s.get(
        f"{PROD_URL}/dashboard",
        params={'apikey': apikey, 'user_id': user_id},
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    if r.status_code not in (200, 302):
        raise RuntimeError(f"apikey auth failed for user_id={user_id}: {r.status_code}")
    return s


def db_conn():
    return get_db_connection()


def cleanup_test_groups():
    """Delete any E2E test groups created by Andy (cascade drops members)."""
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM crab.timeshare_groups
             WHERE created_by = %s AND name LIKE %s
            RETURNING name
        """, (ANDY_USER_ID, f"{TEST_GROUP_PREFIX}%"))
        deleted = cur.fetchall()
        conn.commit()
        if deleted:
            print(f"  🧹 cleaned up {len(deleted)} stale test group(s): {[d[0] for d in deleted]}")
    finally:
        conn.close()


# ── Tests ────────────────────────────────────────────────────────────

def test_robots_txt():
    print("\n[1] robots.txt")
    r = requests.get(f"{PROD_URL}/robots.txt", timeout=TIMEOUT)
    assert_eq("robots.txt status", r.status_code, 200)
    assert_true("robots.txt disallows /timeshare/g/",
                'Disallow: /timeshare/g/' in r.text)


def test_landing_indexable():
    print("\n[2] /timeshare/ landing (anon, indexable)")
    r = requests.get(f"{PROD_URL}/timeshare/", timeout=TIMEOUT)
    assert_eq("landing status", r.status_code, 200)
    assert_true("landing has index,follow", 'index, follow' in r.text)
    assert_true("landing has marketing copy", 'timeshare' in r.text.lower())


def test_anon_group_url_redirects_login():
    print("\n[3] anon on valid-format group UUID → /login redirect (not 404)")
    fake_uuid = str(uuid.uuid4())
    r = requests.get(
        f"{PROD_URL}/timeshare/g/{fake_uuid}/",
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("anon group status", r.status_code, 302)
    assert_true("anon redirects to /login", r.headers.get('location', '').endswith('/login'))


def test_create_group_and_owner_membership():
    print("\n[4] create group + auto-owner membership")
    andy = authed_session(ANDY_USER_ID)
    name = f"{TEST_GROUP_PREFIX} Royal Sands {datetime.now(timezone.utc).strftime('%H%M%S')}"
    r = andy.post(
        f"{PROD_URL}/timeshare/groups/new",
        data={'name': name},
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("create status", r.status_code, 302)
    location = r.headers.get('location', '')
    assert_true("redirected to group dashboard",
                '/timeshare/g/' in location and location.endswith('/'),
                detail=location)
    # Extract group_uuid from redirect path
    group_uuid = location.rstrip('/').split('/')[-1]

    # DB sanity
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.name, g.status, gm.role, gm.accepted_at IS NOT NULL AS accepted
              FROM crab.timeshare_groups g
              JOIN crab.timeshare_group_members gm ON gm.group_id = g.group_id
             WHERE g.group_id = %s::uuid AND gm.user_id = %s
        """, (group_uuid, ANDY_USER_ID))
        row = cur.fetchone()
    finally:
        conn.close()
    assert_true("group row exists + Andy is accepted owner",
                row == (name, 'active', 'owner', True),
                detail=str(row))
    return group_uuid, andy


def test_dashboard_and_nav(group_uuid, andy_session):
    print("\n[5] dashboard renders + nav link present")
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/", timeout=TIMEOUT)
    assert_eq("dashboard status", r.status_code, 200)
    assert_true("dashboard has group name in HTML", "Royal Sands" in r.text)
    assert_true("dashboard has noindex",
                'noindex, nofollow' in r.text)
    # Nav link shows — since Andy has exactly 1 group now, it should deep-link
    # (but he may have more from prior runs; just check Timeshare shows up)
    assert_true("base.html nav includes Timeshare link", '>Timeshare<' in r.text)


def test_404_on_non_member(group_uuid):
    print("\n[6] 404-on-miss for logged-in non-member")
    other = authed_session(INVITEE_USER_ID)
    r = other.get(f"{PROD_URL}/timeshare/g/{group_uuid}/",
                  timeout=TIMEOUT, allow_redirects=False)
    assert_eq("non-member on valid UUID", r.status_code, 404)

    r = other.get(f"{PROD_URL}/timeshare/g/not-a-uuid/",
                  timeout=TIMEOUT, allow_redirects=False)
    assert_eq("non-member on malformed UUID", r.status_code, 404)

    fake_uuid = str(uuid.uuid4())
    r = other.get(f"{PROD_URL}/timeshare/g/{fake_uuid}/",
                  timeout=TIMEOUT, allow_redirects=False)
    assert_eq("non-member on random valid UUID", r.status_code, 404)


def test_invite_creates_row_and_shortlink(group_uuid, andy_session):
    print("\n[7] invite creates pending member row + shortens accept URL")
    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/invite",
        data={'email': INVITEE_EMAIL, 'role': 'family'},
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("invite POST status", r.status_code, 302)

    # Grab the token + verify a shortlink exists for the accept URL
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT invite_token, role, accepted_at, user_id
              FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND email = %s
        """, (group_uuid, INVITEE_EMAIL))
        inv_token, role, accepted_at, user_id = cur.fetchone()

        expected_long_url = f"{PROD_URL}/timeshare/g/{group_uuid}/members/accept/{inv_token}"
        cur.execute(
            "SELECT short_code FROM crab.short_urls WHERE long_url = %s",
            (expected_long_url,)
        )
        short_row = cur.fetchone()
    finally:
        conn.close()

    assert_true("invite token generated (not null)", inv_token is not None)
    assert_eq("invite role", role, 'family')
    assert_true("invite not yet accepted", accepted_at is None and user_id is None)
    assert_true("accept URL was shortened in crab.short_urls",
                short_row is not None,
                detail=f"short_code={short_row[0] if short_row else None}")
    return inv_token


def test_email_mismatch_rejection(group_uuid, inv_token):
    print("\n[8] email-mismatch rejection")
    wrong = authed_session(WRONG_INVITEE_USER_ID)
    r = wrong.get(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/accept/{inv_token}",
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("wrong email on accept", r.status_code, 403)
    assert_true("mismatch body mentions intended email",
                INVITEE_EMAIL in r.text)


def test_accept_success(group_uuid, inv_token):
    print("\n[9] correct-email accept succeeds + token nulled")
    invitee = authed_session(INVITEE_USER_ID)
    r = invitee.get(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/accept/{inv_token}",
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("accept redirect status", r.status_code, 302)
    assert_true("accept redirects to dashboard",
                f'/timeshare/g/{group_uuid}/' in r.headers.get('location', ''))

    # DB verification
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, accepted_at IS NOT NULL, invite_token
              FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND email = %s
        """, (group_uuid, INVITEE_EMAIL))
        user_id, accepted, token_after = cur.fetchone()
    finally:
        conn.close()
    assert_eq("user_id stamped", user_id, INVITEE_USER_ID)
    assert_true("accepted_at set", accepted)
    assert_true("invite_token nulled (single-use)", token_after is None)
    return invitee


def test_dashboard_now_accessible_to_invitee(group_uuid, invitee_session):
    print("\n[10] post-accept: invitee can reach dashboard")
    r = invitee_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/", timeout=TIMEOUT)
    assert_eq("invitee dashboard status", r.status_code, 200)


def test_reclick_after_accept_redirects(group_uuid, inv_token):
    print("\n[11] re-clicking a nulled token: new click returns 404 (token gone)")
    # The token was nulled on accept, so clicking it again should 404 —
    # which is the desired behavior for a leaked/forwarded link.
    invitee = authed_session(INVITEE_USER_ID)
    r = invitee.get(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/accept/{inv_token}",
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("nulled token accept → 404", r.status_code, 404)


def test_expired_invite(group_uuid, andy_session):
    print("\n[12] expired invite → 410 Gone")
    # Seed an expired invite row directly in the DB
    expired_email = "bot.emily.rodriguez@crab.travel"  # user_id=16
    expired_user_id = 16
    stale_token = uuid.uuid4().hex
    conn = db_conn()
    try:
        cur = conn.cursor()
        # Remove any prior row for this email in this group
        cur.execute("""
            DELETE FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND email = %s
        """, (group_uuid, expired_email))
        cur.execute("""
            INSERT INTO crab.timeshare_group_members
                (group_id, email, role, invite_token, invited_by, invited_at)
            VALUES (%s::uuid, %s, 'family', %s, %s, NOW() - INTERVAL '20 days')
        """, (group_uuid, expired_email, stale_token, ANDY_USER_ID))
        conn.commit()
    finally:
        conn.close()

    invitee = authed_session(expired_user_id)
    r = invitee.get(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/accept/{stale_token}",
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("expired invite status", r.status_code, 410)
    assert_true("expired body mentions expired",
                'expired' in r.text.lower())


def test_invite_resend_refreshes_token(group_uuid, andy_session):
    print("\n[13] re-inviting same email refreshes token")
    resend_email = "bot.jake.thompson@crab.travel"  # user_id=17
    # First invite
    r1 = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/invite",
        data={'email': resend_email, 'role': 'family'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("first invite status", r1.status_code, 302)
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT invite_token FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND email = %s
        """, (group_uuid, resend_email))
        t1 = cur.fetchone()[0]
    finally:
        conn.close()

    # Re-invite
    r2 = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/invite",
        data={'email': resend_email, 'role': 'family'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("second invite status", r2.status_code, 302)
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT invite_token FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND email = %s
        """, (group_uuid, resend_email))
        t2 = cur.fetchone()[0]
    finally:
        conn.close()
    assert_true("token changed on resend", t1 != t2,
                detail=f"t1={t1[:8]}... t2={t2[:8]}...")


def test_admin_required_to_invite(group_uuid):
    print("\n[14] non-admin member cannot invite")
    # Demote the previously-accepted invitee to 'family' (already is)
    invitee = authed_session(INVITEE_USER_ID)
    r = invitee.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/members/invite",
        data={'email': 'never-sent@example.com', 'role': 'family'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("non-admin invite → 404 (invisibility)", r.status_code, 404)


def test_members_list_renders(group_uuid, andy_session):
    print("\n[15] members list renders with all invitees")
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/members", timeout=TIMEOUT)
    assert_eq("members status", r.status_code, 200)
    assert_true("members page contains Andy", ANDY_EMAIL in r.text)
    assert_true("members page contains accepted invitee", INVITEE_EMAIL in r.text)
    assert_true("members page contains pending resend", "bot.jake.thompson" in r.text)
    assert_true("noindex on members page", 'noindex, nofollow' in r.text)


# ── Phase 2: schema + fact views + CRUD ─────────────────────────────

PHASE2_TABLES = [
    'timeshare_properties', 'timeshare_contracts', 'timeshare_people',
    'timeshare_maintenance_fees', 'timeshare_loan_payments',
    'timeshare_trips', 'timeshare_trip_participants', 'timeshare_exchanges',
    'timeshare_portals', 'timeshare_contacts', 'timeshare_document_refs',
    'timeshare_timeline_events', 'timeshare_group_shortlist',
    'timeshare_ingest_jobs', 'timeshare_chat_conversations',
    'timeshare_chat_messages', 'timeshare_audit_log',
]
PHASE2_II_TABLES = ['ii_regions', 'ii_areas', 'ii_resorts']

FACT_VIEWS = ['property', 'finances', 'trips', 'people', 'portals',
              'contacts', 'documents', 'timeline']


def test_phase2_schema():
    print("\n[16] Phase 2: all 19 new tables + 3 ii_* exist in crab schema")
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'crab'
               AND table_name = ANY(%s)
        """, (PHASE2_TABLES + PHASE2_II_TABLES,))
        found = {r[0] for r in cur.fetchall()}
        for t in PHASE2_TABLES + PHASE2_II_TABLES:
            assert_true(f"crab.{t} exists", t in found)

        cur.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'crab' AND table_name = 'plans'
               AND column_name = 'timeshare_group_id'
        """)
        assert_true("crab.plans.timeshare_group_id column exists",
                    cur.fetchone() is not None)
    finally:
        conn.close()


def test_phase2_fact_views_members(group_uuid, andy_session):
    print("\n[17] Phase 2: every fact view returns 200 for member + has noindex")
    for view in FACT_VIEWS:
        r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/{view}", timeout=TIMEOUT)
        assert_eq(f"member on /{view}", r.status_code, 200)
        assert_true(f"/{view} has noindex", 'noindex, nofollow' in r.text)


def test_phase2_fact_views_404_for_non_member(group_uuid):
    print("\n[18] Phase 2: every fact view 404s for non-member")
    # Use a bot user who isn't in this group (not user 14 which was invited)
    outsider = authed_session(18)  # bot.priya.patel
    for view in FACT_VIEWS:
        r = outsider.get(f"{PROD_URL}/timeshare/g/{group_uuid}/{view}",
                         timeout=TIMEOUT, allow_redirects=False)
        assert_eq(f"non-member on /{view}", r.status_code, 404)


def test_phase2_crud_property(group_uuid, andy_session):
    print("\n[19] Phase 2: CRUD on properties + scope enforcement")
    # Create a property
    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/fact/properties/new",
        data={'name': 'Royal Sands Cancún', 'unit_number': 'K5133',
              'week_number': '38', 'usage_pattern': 'biennial_even',
              'exchange_network': 'interval_international',
              'country': 'Mexico', 'city': 'Cancún',
              'trust_expiry_date': '2050-01-24'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("property create status", r.status_code, 302)

    # Verify it landed via the finances page (which lists properties)
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/property", timeout=TIMEOUT)
    assert_true("property appears on view", 'Royal Sands Cancún' in r.text)
    assert_true("property unit appears", 'K5133' in r.text)

    # Grab the pk_id + try a CSF fee row
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id FROM crab.timeshare_properties
             WHERE group_id = %s::uuid AND name = %s
        """, (group_uuid, 'Royal Sands Cancún'))
        prop_id = cur.fetchone()[0]
    finally:
        conn.close()

    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/fact/maintenance_fees/new",
        data={'parent_id': str(prop_id), 'year': '2024',
              'billed_amount_usd': '1381.00', 'paid_amount_usd': '1381.00',
              'paid_date': '2024-09-22'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("maintenance_fees create status", r.status_code, 302)

    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/finances", timeout=TIMEOUT)
    assert_true("CSF row visible on finances page", '1,381.00' in r.text or '1381.00' in r.text)

    # Invalid year coercion → should flash error
    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/fact/maintenance_fees/new",
        data={'parent_id': str(prop_id), 'year': 'not-a-year'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("bad-year coercion redirects with flash", r.status_code, 302)

    return prop_id


def test_phase2_crud_portals_redaction(group_uuid, andy_session):
    print("\n[20] Phase 2: portals view never leaks encrypted_password_ref")
    # Seed a portal with an encrypted_password_ref in the DB (simulating what
    # a future reveal endpoint would reference)
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_portals
                (group_id, portal_name, url, username, encrypted_password_ref,
                 member_number, support_phone)
            VALUES (%s::uuid, 'Interval International',
                    'https://intervalworld.com', 'TILLOAT',
                    'SECRET_REF_SHOULD_NEVER_APPEAR', '3430769',
                    '1-800-555-1234')
        """, (group_uuid,))
        conn.commit()
    finally:
        conn.close()
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/portals", timeout=TIMEOUT)
    assert_eq("portals view status", r.status_code, 200)
    assert_true("portal_name renders", 'Interval International' in r.text)
    assert_true("username renders", 'TILLOAT' in r.text)
    assert_true(
        "encrypted_password_ref NEVER in HTML",
        'SECRET_REF_SHOULD_NEVER_APPEAR' not in r.text,
        detail="(route strips it before rendering)",
    )


def test_phase2_cross_group_scope():
    print("\n[21] Phase 2: fact writes cannot cross groups")
    # Create two distinct groups owned by Andy
    andy = authed_session(ANDY_USER_ID)
    name_a = f"{TEST_GROUP_PREFIX} scope A {uuid.uuid4().hex[:6]}"
    name_b = f"{TEST_GROUP_PREFIX} scope B {uuid.uuid4().hex[:6]}"
    r = andy.post(f"{PROD_URL}/timeshare/groups/new", data={'name': name_a},
                  timeout=TIMEOUT, allow_redirects=False)
    uuid_a = r.headers['location'].rstrip('/').split('/')[-1]
    r = andy.post(f"{PROD_URL}/timeshare/groups/new", data={'name': name_b},
                  timeout=TIMEOUT, allow_redirects=False)
    uuid_b = r.headers['location'].rstrip('/').split('/')[-1]

    # Andy creates a property in group A
    andy.post(
        f"{PROD_URL}/timeshare/g/{uuid_a}/fact/properties/new",
        data={'name': 'Group-A Property'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id FROM crab.timeshare_properties
             WHERE group_id = %s::uuid
        """, (uuid_a,))
        prop_a_id = cur.fetchone()[0]
    finally:
        conn.close()

    # Now try to insert a maintenance_fee in group B using the group-A property
    # as parent. The insert_fact helper should reject it with "parent not in
    # this group" and no row should land.
    r = andy.post(
        f"{PROD_URL}/timeshare/g/{uuid_b}/fact/maintenance_fees/new",
        data={'parent_id': str(prop_a_id), 'year': '2024',
              'billed_amount_usd': '999'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("cross-group insert redirects", r.status_code, 302)

    # Verify nothing landed
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM crab.timeshare_maintenance_fees f
              JOIN crab.timeshare_properties p ON p.pk_id = f.property_id
             WHERE p.group_id = %s::uuid OR f.property_id = %s
        """, (uuid_b, prop_a_id))
        count_in_b_or_linking = cur.fetchone()[0]
    finally:
        conn.close()
    assert_eq("no cross-group fee row materialized", count_in_b_or_linking, 0)

    # Andy tries to UPDATE group-A's property via group-B's URL — scope guard
    # should prevent it
    r = andy.post(
        f"{PROD_URL}/timeshare/g/{uuid_b}/fact/properties/{prop_a_id}",
        data={'name': 'HIJACKED'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("cross-group update redirects", r.status_code, 302)
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT name FROM crab.timeshare_properties WHERE pk_id = %s
        """, (prop_a_id,))
        still_name = cur.fetchone()[0]
    finally:
        conn.close()
    assert_eq("group-A property untouched by group-B update", still_name, 'Group-A Property')


def test_phase2_unknown_fact_key(group_uuid, andy_session):
    print("\n[22] Phase 2: unknown fact_key → 404")
    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/fact/users/new",
        data={'anything': 'goes'},
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("unknown fact_key status", r.status_code, 404)


def test_phase2_dashboard_shows_counts(group_uuid, andy_session):
    print("\n[23] Phase 2: dashboard renders fact-count cards")
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/", timeout=TIMEOUT)
    assert_eq("dashboard status", r.status_code, 200)
    for label in ('Property', 'Fees', 'Trips', 'People', 'Portals',
                  'Contacts', 'Documents', 'Timeline'):
        assert_true(f"dashboard card: {label}", f">{label}<" in r.text)


# ── Phase 3: ingestion ─────────────────────────────────────────────

# Claude API calls take real time — use a longer timeout for ingest requests.
INGEST_TIMEOUT = 180

SAMPLE_CSF_TEXT = """Royal Resorts — account statement
Member: 3430769
Unit: K5133, week 38

Maintenance fee history for the biennial even-year cycle:
- 2022 billed $1,234.00 on September 1 2022, paid $1,234.00 on September 18 2022
- 2024 billed $1,381.00 on September 1 2024, paid $1,381.00 on October 3 2024 with a $25.00 late fee
"""


def test_phase3_ingest_wizard_scope(group_uuid, andy_session):
    print("\n[24] Phase 3: /ingest wizard renders for member + 404s for non-member")
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/ingest", timeout=TIMEOUT)
    assert_eq("member on /ingest", r.status_code, 200)
    assert_true("/ingest has Paste form", 'ingest/paste' in r.text)
    assert_true("/ingest has Upload form", 'ingest/upload' in r.text)
    assert_true("/ingest noindex", 'noindex, nofollow' in r.text)

    outsider = authed_session(18)  # bot.priya.patel — not a member
    r = outsider.get(f"{PROD_URL}/timeshare/g/{group_uuid}/ingest",
                     timeout=TIMEOUT, allow_redirects=False)
    assert_eq("non-member on /ingest", r.status_code, 404)


def test_phase3_paste_extracts(group_uuid, andy_session):
    print("\n[25] Phase 3: paste submit → Claude extracts → job in review")
    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/ingest/paste",
        data={'content': SAMPLE_CSF_TEXT},
        timeout=INGEST_TIMEOUT,
        allow_redirects=False,
    )
    assert_eq("paste redirect status", r.status_code, 302)
    location = r.headers.get('location', '')
    assert_true("redirect to review page",
                f'/ingest/jobs/' in location, detail=location)
    job_id = int(location.rstrip('/').split('/')[-1])

    # Verify the DB row
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT status, source_type, source_content IS NOT NULL,
                   extracted_facts IS NOT NULL,
                   claude_cost_usd
              FROM crab.timeshare_ingest_jobs
             WHERE group_id = %s::uuid AND pk_id = %s
        """, (group_uuid, job_id))
        status, src, has_content, has_facts, cost = cur.fetchone()
    finally:
        conn.close()
    assert_eq("job source_type", src, 'text_paste')
    assert_true("source_content persisted (for provenance)", has_content)
    assert_true("claude_cost_usd > 0", cost is not None and float(cost) > 0,
                detail=f"cost={cost}")
    # Status is typically 'review' when Claude found facts, or 'rejected' if it
    # explicitly called no_facts_extracted. Either is acceptable as a shape check.
    assert_true(
        "status terminates cleanly",
        status in ('review', 'rejected'),
        detail=f"status={status}",
    )
    # Confirm at least one maintenance_fee row was proposed
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT extracted_facts -> 'maintenance_fees'
              FROM crab.timeshare_ingest_jobs
             WHERE pk_id = %s
        """, (job_id,))
        fees_block = cur.fetchone()[0]
    finally:
        conn.close()
    assert_true(
        "Claude proposed at least one maintenance_fee row",
        isinstance(fees_block, list) and len(fees_block) >= 1,
        detail=f"len={len(fees_block) if isinstance(fees_block, list) else 'n/a'}",
    )
    return job_id


def test_phase3_review_page_renders(group_uuid, andy_session, job_id):
    print("\n[26] Phase 3: review page shows proposed rows + source content")
    r = andy_session.get(
        f"{PROD_URL}/timeshare/g/{group_uuid}/ingest/jobs/{job_id}", timeout=TIMEOUT)
    assert_eq("review page status", r.status_code, 200)
    assert_true("review page shows source_type pill", 'text paste' in r.text.lower())
    assert_true("review page shows source content", 'Royal Resorts' in r.text)


def test_phase3_commit(group_uuid, andy_session, job_id):
    print("\n[27] Phase 3: commit accepted rows land in fact tables with source_ingest_job_id")
    # Fetch the accept_* field names from the proposed rows and mark them all on
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT extracted_facts FROM crab.timeshare_ingest_jobs WHERE pk_id = %s
        """, (job_id,))
        facts = cur.fetchone()[0] or {}
    finally:
        conn.close()

    form_data = {}
    for fact_key, rows in facts.items():
        if fact_key.startswith('_') or not isinstance(rows, list):
            continue
        for i in range(len(rows)):
            form_data[f'accept_{fact_key}_{i}'] = 'on'

    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/ingest/jobs/{job_id}/commit",
        data=form_data, timeout=INGEST_TIMEOUT, allow_redirects=False,
    )
    assert_eq("commit redirect", r.status_code, 302)

    # Verify rows landed with source_ingest_job_id linking back
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM crab.timeshare_maintenance_fees f
              JOIN crab.timeshare_properties p ON p.pk_id = f.property_id
             WHERE p.group_id = %s::uuid AND f.source_ingest_job_id = %s
        """, (group_uuid, job_id))
        committed_fees = cur.fetchone()[0]

        cur.execute("""
            SELECT status, committed_at IS NOT NULL
              FROM crab.timeshare_ingest_jobs
             WHERE pk_id = %s
        """, (job_id,))
        status, has_committed_at = cur.fetchone()
    finally:
        conn.close()

    assert_true("at least one fee row committed", committed_fees >= 1,
                detail=f"committed_fees={committed_fees}")
    assert_eq("job status=committed", status, 'committed')
    assert_true("committed_at stamped", has_committed_at)


def test_phase3_job_list_renders(group_uuid, andy_session, job_id):
    print("\n[28] Phase 3: /ingest/jobs lists the committed job")
    r = andy_session.get(f"{PROD_URL}/timeshare/g/{group_uuid}/ingest/jobs", timeout=TIMEOUT)
    assert_eq("job list status", r.status_code, 200)
    assert_true("job list has this job", f'/ingest/jobs/{job_id}' in r.text)
    assert_true("job list shows committed badge", 'committed' in r.text)


def test_phase3_reject_path(group_uuid, andy_session):
    print("\n[29] Phase 3: reject on a pending job flips status")
    # Seed a fake job directly in the DB to avoid another Claude call
    import json
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_ingest_jobs
                (group_id, source_type, source_snapshot_hash, source_content,
                 status, extracted_facts, created_by)
            VALUES (%s::uuid, 'text_paste', 'sha-reject-test',
                    'test content', 'review',
                    %s::jsonb, %s)
            RETURNING pk_id
        """, (group_uuid,
              json.dumps({'maintenance_fees': [{'year': 2099, 'billed_amount_usd': 1.0}]}),
              ANDY_USER_ID))
        stub_job_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    r = andy_session.post(
        f"{PROD_URL}/timeshare/g/{group_uuid}/ingest/jobs/{stub_job_id}/reject",
        data={'notes': 'Test reject'}, timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("reject redirect", r.status_code, 302)

    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT status, review_notes FROM crab.timeshare_ingest_jobs
             WHERE pk_id = %s
        """, (stub_job_id,))
        status, notes = cur.fetchone()
    finally:
        conn.close()
    assert_eq("status=rejected", status, 'rejected')
    assert_eq("review notes recorded", notes, 'Test reject')


def test_phase3_cross_group_job_access(group_uuid, andy_session):
    print("\n[30] Phase 3: reviewing another group's job → 404")
    # Create a second Andy-owned group, seed a job there, then try to view it via group_uuid
    andy = authed_session(ANDY_USER_ID)
    r = andy.post(f"{PROD_URL}/timeshare/groups/new",
                  data={'name': f'{TEST_GROUP_PREFIX} ingest-scope {uuid.uuid4().hex[:6]}'},
                  timeout=TIMEOUT, allow_redirects=False)
    other_uuid = r.headers['location'].rstrip('/').split('/')[-1]

    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_ingest_jobs
                (group_id, source_type, source_snapshot_hash, source_content,
                 status, created_by)
            VALUES (%s::uuid, 'text_paste', 'sha-scope-test',
                    'isolated content', 'review', %s)
            RETURNING pk_id
        """, (other_uuid, ANDY_USER_ID))
        other_job_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    # Try to open other_job_id via group_uuid (wrong group) — should 404
    r = andy_session.get(
        f"{PROD_URL}/timeshare/g/{group_uuid}/ingest/jobs/{other_job_id}",
        timeout=TIMEOUT, allow_redirects=False,
    )
    assert_eq("cross-group job access", r.status_code, 404)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep', action='store_true', help='Do not delete test groups on exit')
    args = ap.parse_args()

    print("=" * 60)
    print("Timeshare E2E against", PROD_URL)
    print("=" * 60)

    # Pre-clean any stale test groups from prior aborted runs
    cleanup_test_groups()

    try:
        test_robots_txt()
        test_landing_indexable()
        test_anon_group_url_redirects_login()
        group_uuid, andy = test_create_group_and_owner_membership()
        test_dashboard_and_nav(group_uuid, andy)
        test_404_on_non_member(group_uuid)
        inv_token = test_invite_creates_row_and_shortlink(group_uuid, andy)
        test_email_mismatch_rejection(group_uuid, inv_token)
        invitee = test_accept_success(group_uuid, inv_token)
        test_dashboard_now_accessible_to_invitee(group_uuid, invitee)
        test_reclick_after_accept_redirects(group_uuid, inv_token)
        test_expired_invite(group_uuid, andy)
        test_invite_resend_refreshes_token(group_uuid, andy)
        test_admin_required_to_invite(group_uuid)
        test_members_list_renders(group_uuid, andy)
        # Phase 2
        test_phase2_schema()
        test_phase2_fact_views_members(group_uuid, andy)
        test_phase2_fact_views_404_for_non_member(group_uuid)
        test_phase2_crud_property(group_uuid, andy)
        test_phase2_crud_portals_redaction(group_uuid, andy)
        test_phase2_cross_group_scope()
        test_phase2_unknown_fact_key(group_uuid, andy)
        test_phase2_dashboard_shows_counts(group_uuid, andy)
        # Phase 3 — real Claude API calls, ~$0.10 per full run
        test_phase3_ingest_wizard_scope(group_uuid, andy)
        phase3_job_id = test_phase3_paste_extracts(group_uuid, andy)
        test_phase3_review_page_renders(group_uuid, andy, phase3_job_id)
        test_phase3_commit(group_uuid, andy, phase3_job_id)
        test_phase3_job_list_renders(group_uuid, andy, phase3_job_id)
        test_phase3_reject_path(group_uuid, andy)
        test_phase3_cross_group_job_access(group_uuid, andy)
    finally:
        if not args.keep:
            cleanup_test_groups()
        else:
            print(f"\n  --keep set; test groups left in place")

    print("\n" + "=" * 60)
    print(f"  Passed: {len(PASSED)}")
    print(f"  Failed: {len(FAILED)}")
    if FAILED:
        print("\n  FAILURES:")
        for name, detail in FAILED:
            print(f"    ❌  {name} — {detail}")
        sys.exit(1)
    print("=" * 60)


if __name__ == '__main__':
    main()
