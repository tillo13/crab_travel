"""Smoke tests for the price-drop alert pipeline.

Covers the production-grade alert rewrite:
  1. Adapter flap (Duffel $260 / TP $340) → 0 alerts, skip='adapter_disagreement'
  2. Real drop, both adapters cheap, honorable link wins → 1 alert
  3. Same alert re-fired same week (same price band) → ledger dedupe blocks 2nd send
  4. Duffel-only cheap quote (no honorable link, no corroboration) → 0 alerts
  5. Travelpayouts-only quote → 1 alert (single source but honorable link)
  6. baseline insufficient history → 0 alerts (quiet learning mode)

Run: python tests/test_watch_alerting.py
"""

import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utilities import watch_engine as we
from utilities.postgres_utils import get_db_connection
import psycopg2.extras


PASS = "✅"
FAIL = "❌"


def assertEq(actual, expected, label):
    if actual == expected:
        print(f"  {PASS} {label}")
        return True
    print(f"  {FAIL} {label}\n      expected={expected!r}\n      actual=  {actual!r}")
    sys.exit(1)


def assertTrue(cond, label):
    if cond:
        print(f"  {PASS} {label}")
        return True
    print(f"  {FAIL} {label}")
    sys.exit(1)


def section(title):
    print(f"\n── {title} ─────────────")


# Stub the baseline + skip recorder so tests are deterministic + DB-free for
# the pure decision-logic checks. Integration test at the bottom hits the DB.
_skips = []
_real_compute_baseline = we._compute_baseline
_real_record_skip = we._record_skip


def stub_baseline(_):
    return stub_baseline._value
stub_baseline._value = 300.0


def stub_record_skip(watch_id, reason, **kw):
    _skips.append({'watch_id': watch_id, 'reason': reason, **kw})


def install_stubs():
    we._compute_baseline = stub_baseline
    we._record_skip = stub_record_skip
    _skips.clear()


def restore_stubs():
    we._compute_baseline = _real_compute_baseline
    we._record_skip = _real_record_skip


WATCH = {'pk_id': 999_999_999, 'alert_threshold_pct': 10,
         'user_email': 'test@example.invalid', 'member_name': 'TestMember',
         'origin': 'SEA', 'destination': 'MIA', 'watch_type': 'flight'}


# ─── Test 1: adapter flap, disagreement >25% → skip ─────────────────────────

def test_flap_blocked():
    section("1. Adapter flap (duffel $260 + tp $340) blocks alert")
    install_stubs()
    stub_baseline._value = 300.0
    quotes = [
        {'source': 'duffel', 'price_usd': 259.98,
         'deep_link': 'k', 'deep_link_honors_price': False, 'data': {}},
        {'source': 'travelpayouts', 'price_usd': 340.00,
         'deep_link': 'a', 'deep_link_honors_price': True, 'data': {}},
    ]
    d = we._make_alert_decision(WATCH, quotes, 'email')
    assertEq(d, None, "decision is None")
    assertEq(len(_skips), 1, "one skip row written")
    assertEq(_skips[0]['reason'], 'adapter_disagreement', "skip reason")
    restore_stubs()


# ─── Test 2: real drop with corroboration → fires ────────────────────────────

def test_real_drop_fires():
    section("2. Real drop, both adapters within 20%, honorable link wins")
    install_stubs()
    stub_baseline._value = 300.0
    quotes = [
        {'source': 'duffel', 'price_usd': 200.0,
         'deep_link': 'kayak://', 'deep_link_honors_price': False, 'data': {}},
        {'source': 'travelpayouts', 'price_usd': 215.0,
         'deep_link': 'aviasales://', 'deep_link_honors_price': True, 'data': {}},
    ]
    d = we._make_alert_decision(WATCH, quotes, 'email')
    assertTrue(d is not None, "decision returned")
    assertEq(d['alert_usd'], 215.0, "advertised price is the honorable one")
    assertEq(d['deep_link_source'], 'travelpayouts', "deep_link from honorable adapter")
    assertEq(d['deep_link_honors_price'], True, "marked as honorable")
    assertEq(d['min_quote_source'], 'duffel', "min quote source recorded")
    assertEq(d['sources_corroborating'], 2, "2 corroborating sources")
    assertTrue(d['drop_pct'] > 10, "drop_pct above threshold")
    restore_stubs()


# ─── Test 3: insufficient history → quiet ────────────────────────────────────

def test_no_baseline_quiet():
    section("3. Insufficient history → no alert, no skip row")
    install_stubs()
    stub_baseline._value = None
    quotes = [
        {'source': 'travelpayouts', 'price_usd': 100.0,
         'deep_link': 'a', 'deep_link_honors_price': True, 'data': {}},
    ]
    d = we._make_alert_decision(WATCH, quotes, 'email')
    assertEq(d, None, "no alert when baseline unavailable")
    assertEq(_skips, [], "no skip rows (quiet learning mode)")
    restore_stubs()


# ─── Test 4: duffel-only, no honorable, no corroboration → skip ─────────────

def test_unhonorable_solo_blocked():
    section("4. Duffel-only quote (unhonorable link, no corroboration)")
    install_stubs()
    stub_baseline._value = 300.0
    quotes = [
        {'source': 'duffel', 'price_usd': 200.0,
         'deep_link': 'kayak://', 'deep_link_honors_price': False, 'data': {}},
    ]
    d = we._make_alert_decision(WATCH, quotes, 'email')
    assertEq(d, None, "decision is None")
    assertEq(len(_skips), 1, "one skip row")
    assertEq(_skips[0]['reason'], 'no_corroboration', "skip reason")
    restore_stubs()


# ─── Test 5: travelpayouts-only → fires (single source but honorable) ───────

def test_honorable_solo_fires():
    section("5. Travelpayouts-only honorable quote fires alert")
    install_stubs()
    stub_baseline._value = 300.0
    quotes = [
        {'source': 'travelpayouts', 'price_usd': 215.0,
         'deep_link': 'aviasales://', 'deep_link_honors_price': True, 'data': {}},
    ]
    d = we._make_alert_decision(WATCH, quotes, 'email')
    assertTrue(d is not None, "decision returned")
    assertEq(d['alert_usd'], 215.0, "advertised price is tp's")
    assertEq(d['deep_link_source'], 'travelpayouts', "deep_link from tp")
    restore_stubs()


# ─── Test 6: threshold guardrail when honorable price doesn't clear bar ─────

def test_unhonorable_min_with_honorable_above_threshold():
    section("6. Min clears threshold but honorable quote (advertised) does not")
    install_stubs()
    stub_baseline._value = 300.0
    # min=$266 drops 11.3% (clears 10%). honorable=$279, within min*1.05=$279.30
    # so corroboration passes. Advertised=$279 drops 7% — below threshold.
    quotes = [
        {'source': 'duffel', 'price_usd': 266.0,
         'deep_link': 'k', 'deep_link_honors_price': False, 'data': {}},
        {'source': 'travelpayouts', 'price_usd': 279.0,
         'deep_link': 'a', 'deep_link_honors_price': True, 'data': {}},
    ]
    d = we._make_alert_decision(WATCH, quotes, 'email')
    assertEq(d, None, "no alert when honorable advertised price below threshold")
    reasons = [s['reason'] for s in _skips]
    assertTrue('honorable_quote_below_threshold' in reasons,
               f"skip reason in reasons (got {reasons})")
    restore_stubs()


# ─── Test 7: integration — price-FLOOR gate (new-low only) ──────────────────

def test_ledger_dedupe_integration():
    """End-to-end through _send_alert_v2: the first alert fires; the SAME or a
    HIGHER price is suppressed; a non-material drop is suppressed; a materially
    lower new low re-fires. Pins the fix for the SEA→MIA $260-3× incident (the
    old fixed-week+band unique key let same/higher prices re-fire on a Thursday
    rollover or a $25 band straddle)."""
    section("7. Integration — price-floor gate (new-low only)")
    import utilities.notification_utils as _nu
    from utilities import watch_engine as we

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # A watch with NO existing alerts → the gate only sees rows this test writes.
    cur.execute("""
        SELECT pk_id FROM crab.member_watches
        WHERE pk_id NOT IN (SELECT watch_id FROM crab.watch_alerts)
        LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        print("  ⚠️  no alert-free member_watches row; skipping integration test")
        cur.close(); conn.close()
        return
    watch_id = row['pk_id']
    MARK = 'https://test.invalid/floor-gate'  # cleanup marker for our rows only
    watch = {'pk_id': watch_id, 'alert_threshold_pct': 10}

    def _decision(price):
        return {'channel': 'email', 'baseline_usd': 400.0, 'alert_usd': float(price),
                'drop_pct': 25.0, 'sources_corroborating': 2,
                'price_band': (int(price) // 25) * 25, 'deep_link': MARK}

    # Stub the real send so no email goes out; count fires.
    calls = {'n': 0}
    _orig = _nu.notify_price_drop
    _nu.notify_price_drop = lambda *a, **k: calls.__setitem__('n', calls['n'] + 1)

    cur.execute("DELETE FROM crab.watch_alerts WHERE watch_id=%s AND deep_link=%s",
                (watch_id, MARK))
    conn.commit()
    try:
        assertTrue(we._send_alert_v2(watch, _decision(200)) is True, "first alert ($200) fires")
        assertTrue(we._send_alert_v2(watch, _decision(200)) is False, "same price ($200) suppressed")
        assertTrue(we._send_alert_v2(watch, _decision(260)) is False, "higher price ($260) suppressed")
        assertTrue(we._send_alert_v2(watch, _decision(195)) is False, "non-material drop ($195) suppressed")
        assertTrue(we._send_alert_v2(watch, _decision(170)) is True, "material new low ($170, -15%) re-fires")
        assertEq(calls['n'], 2, "exactly 2 emails sent across the 5 candidates")
    finally:
        _nu.notify_price_drop = _orig
        cur.execute("DELETE FROM crab.watch_alerts WHERE watch_id=%s AND deep_link=%s",
                    (watch_id, MARK))
        conn.commit()
        cur.close(); conn.close()


if __name__ == '__main__':
    print("crab.travel — watch alerting smoke tests\n")
    test_flap_blocked()
    test_real_drop_fires()
    test_no_baseline_quiet()
    test_unhonorable_solo_blocked()
    test_honorable_solo_fires()
    test_unhonorable_min_with_honorable_above_threshold()
    test_ledger_dedupe_integration()
    print(f"\nAll tests passed.")
