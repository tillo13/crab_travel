"""Unit tests for the heartbeat suppress-on-unchanged state hash.

The whole point of suppression is: the hash must be INVARIANT to the numbers that
drift every run (counts, ages, percentages) but MUST change on a real status flip
or a lag escalation. If it changed on count drift, suppression would never fire;
if it didn't change on a status flip, a real problem would be silenced.

Run: python tests/test_heartbeat_suppress.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_heartbeat import _state_hash, _norm_waiting

PASS, FAIL = "✅", "❌"


def ok(cond, label):
    print(f"  {PASS if cond else FAIL} {label}")
    if not cond:
        sys.exit(1)


def test_invariant_to_count_drift():
    print("test_invariant_to_count_drift")
    # Same statuses, different drifting numbers in the summary text.
    h1 = [('crons', '✅', 'last run 5min ago'), ('db_pool', '✅', '3/100 active (3%)')]
    h2 = [('crons', '✅', 'last run 9min ago'), ('db_pool', '✅', '7/100 active (7%)')]
    ok(_state_hash(h1, []) == _state_hash(h2, []), "health count/age drift → same hash")

    # Waiting lines differing only in counts.
    w1 = ['<b>1</b> stale watch']
    w2 = ['<b>5</b> stale watch']
    ok(_state_hash([], w1) == _state_hash([], w2), "waiting count drift → same hash")


def test_status_flip_changes_hash():
    print("test_status_flip_changes_hash")
    h_ok = [('opencrab', '✅', 'all good')]
    h_bad = [('opencrab', '🟠', 'silent')]
    ok(_state_hash(h_ok, []) != _state_hash(h_bad, []), "✅→🟠 flip → different hash")

    # A category appearing (waiting line present vs absent) flips the hash.
    ok(_state_hash([], []) != _state_hash([], ['<b>2</b> stale watch']),
       "waiting category appearing → different hash")


def test_lag_escalation_changes_hash():
    print("test_lag_escalation_changes_hash")
    brief = ['stale watch — oldest lag 30h']
    dead = ['stale watch — oldest lag 200h']
    ok(_state_hash([], brief) != _state_hash([], dead),
       "lag 30h→200h (band 24h+ → 7d+) → different hash")
    # Same band, different exact number → same hash.
    a = ['stale watch — oldest lag 25h']
    b = ['stale watch — oldest lag 70h']  # both in 24h+ band
    ok(_state_hash([], a) == _state_hash([], b), "same lag band, different number → same hash")


def test_norm_waiting_bands():
    print("test_norm_waiting_bands")
    ok('LAGlow' in _norm_waiting('lag 5h'), "5h → LAGlow")
    ok('LAGmed' in _norm_waiting('lag 30h'), "30h → LAGmed")
    ok('LAGhigh' in _norm_waiting('lag 4 days'), "4 days → LAGhigh")
    ok('LAGdead' in _norm_waiting('lag 10 days'), "10 days → LAGdead")
    # Adjacent bands must NOT collide after the digit collapse.
    ok(_norm_waiting('lag 30h') != _norm_waiting('lag 100h'), "30h (med) vs 100h (high) differ")
    ok(_norm_waiting('<b>3</b> active') == _norm_waiting('<b>9</b> active'),
       "html stripped + counts collapsed")


def test_broken_check_bucketed():
    print("test_broken_check_bucketed")
    healthy = [('opencrab', '✅', 'ok')]
    broken = [{'_error': 'column foo does not exist'}]
    ok(_state_hash(healthy, []) != _state_hash(broken, []), "healthy→broken-check → different hash")


def main():
    test_invariant_to_count_drift()
    test_status_flip_changes_hash()
    test_lag_escalation_changes_hash()
    test_norm_waiting_bands()
    test_broken_check_bucketed()
    print(f"\n{PASS} all heartbeat suppress hash tests passed")


if __name__ == "__main__":
    main()
