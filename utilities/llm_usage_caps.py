"""
Shared LLM daily usage caps — canonical source of truth for all kumori apps.
Copy this file into your project's utilities/ directory.

In-memory counters for fast path (no DB hit per call).
DB sync on cold start + every 5 min to share totals across apps.
Async DB writes so LLM calls never block on logging.

Usage:
    from utilities.llm_usage_caps import check_cap, record_call, sync_from_db, DAILY_CAPS

    if check_cap('groq'):
        # make the call
        record_call('groq')
    else:
        # skip, daily cap reached
"""

import logging
import threading
import time
from datetime import date

logger = logging.getLogger(__name__)

# ── Actual free tier daily caps (recalibrated 2026-03-24) ──
# These are the REAL limits. When hit, skip the backend — no retries, no 429 waste.
#
# With crawlers running every 5 min = ~288 calls/day, we need ~300/day total.
# Spread across Tier 1 backends (cerebras + 4× groq) = ~60 each, well under limits.
# Keep caps conservative to avoid 429 storms that waste time and log noise.
#
# KEY INSIGHT: All 4 Groq models share ONE API key with a shared 30 RPM pool.
# The per-model 1K RPD is real, but the RPM is the binding constraint with concurrency.
DAILY_CAPS = {
    # ── Tier 1: Workhorses (carry bulk of traffic) ──
    'cerebras': 1000,            # 1M tokens/day free, 30 RPM — our #1 backend
    'groq': 500,                 # 1K RPD per model, but shared key RPM limits real throughput
    'groq-kimi': 500,            # same — cap at 500 to leave room for user-triggered calls
    'groq-qwen': 500,            # same
    'groq-gptoss': 500,          # same

    # ── Tier 2: Moderate (fill gaps when Tier 1 throttled) ──
    'gemini': 200,               # 250 req/day for Flash, 10 RPM — leave 50 for user calls
    'llm7': 300,                 # No documented cap, no key — conservative

    # ── Tier 3: Low caps or slow (deep fallbacks) ──
    'openrouter-gemma': 40,      # 50/day per :free model — leave 10 buffer
    'openrouter-llama': 40,
    'openrouter-gemma-nano': 40,
    'nvidia': 50,                # 1K LIFETIME credits (not daily!) — PROTECT. ~20 days at 50/day.
    'grok': 100,                 # PoW via Cloud Run — free but slow, 60s timeouts
    'deepseek': 100,             # PoW via Cloud Run — free but slow, 60s timeouts
    'mistral': 100,              # 2 RPM = max ~2,880/day but practically useless at that speed

    # ── Dead backends (keep in caps table for tracking, 0 = disabled) ──
    'together': 0,               # Credits exhausted, 401 Unauthorized — dead
    'grok_fast': 0,              # Not currently wired up
    'grok4': 0,                  # Not currently wired up

    # ── Paid backends (absolute last resort) ──
    'gpt4o-mini': 50,            # Paid — keep low, only for when all free fail
    'gpt4o': 5,                  # Expensive
    'haiku': 10,                 # Anthropic paid — absolute last resort
    'sonnet': 5,
    'opus': 2,
    'local': 999999,             # No limit
}

# ── In-memory state ──
_daily_counts = {}     # backend -> count (fast path)
_count_date = None     # date of current counts
_last_db_sync = 0      # timestamp of last DB sync
_app_name = None       # set by init()
_db_module = None      # set by init() — the project's postgres_utils or similar

DB_SYNC_INTERVAL = 300  # Sync from DB every 5 minutes


def init(app_name, db_write_fn=None, db_read_fn=None):
    """Initialize with app name and optional DB functions.

    Args:
        app_name: 'kindness_social', 'crab_travel', etc.
        db_write_fn: fn(backend, app_name) to increment DB counter. Fire-and-forget.
        db_read_fn: fn() -> {backend: total_across_all_apps}. Called every 5 min.
    """
    global _app_name, _db_write_fn, _db_read_fn
    _app_name = app_name
    _db_write_fn = db_write_fn
    _db_read_fn = db_read_fn
    # Seed from DB on startup
    if db_read_fn:
        sync_from_db()


_db_write_fn = None
_db_read_fn = None


def _reset_if_new_day():
    global _daily_counts, _count_date
    today = date.today()
    if _count_date != today:
        _daily_counts = {}
        _count_date = today


def sync_from_db():
    """Pull cross-app totals from DB into local counters."""
    global _daily_counts, _last_db_sync, _count_date
    if not _db_read_fn:
        return
    try:
        totals = _db_read_fn()
        if totals:
            _count_date = date.today()
            _daily_counts = dict(totals)
            _last_db_sync = time.time()
            logger.debug(f"Synced LLM caps from DB: {sum(totals.values())} total calls today")
    except Exception as e:
        logger.debug(f"DB sync failed (non-fatal): {e}")


def _maybe_sync():
    """Periodic DB sync — every 5 min."""
    if time.time() - _last_db_sync > DB_SYNC_INTERVAL:
        sync_from_db()


def check_cap(backend):
    """Return True if this backend is under its daily free cap. Fast path (in-memory)."""
    _reset_if_new_day()
    _maybe_sync()
    cap = DAILY_CAPS.get(backend, 50)
    used = _daily_counts.get(backend, 0)
    return used < cap


def remaining(backend):
    """How many calls left today for this backend."""
    _reset_if_new_day()
    cap = DAILY_CAPS.get(backend, 50)
    used = _daily_counts.get(backend, 0)
    return max(0, cap - used)


def record_call(backend):
    """Record a successful call. Updates in-memory counter + async DB write."""
    _reset_if_new_day()
    _daily_counts[backend] = _daily_counts.get(backend, 0) + 1

    # Async DB write — never block the LLM response
    if _db_write_fn and _app_name:
        t = threading.Thread(target=_safe_db_write, args=(backend,), daemon=True)
        t.start()


def _safe_db_write(backend):
    try:
        _db_write_fn(backend, _app_name)
    except Exception:
        pass  # Fire and forget


def get_usage_summary():
    """Current usage vs caps for all backends."""
    _reset_if_new_day()
    result = {}
    for backend, cap in DAILY_CAPS.items():
        used = _daily_counts.get(backend, 0)
        result[backend] = {
            'used': used,
            'cap': cap,
            'remaining': max(0, cap - used),
            'pct': round(used / cap * 100, 1) if cap > 0 else 0,
        }
    return {
        'date': date.today().isoformat(),
        'backends': result,
        'total_used': sum(_daily_counts.values()),
        'total_cap': sum(DAILY_CAPS.values()),
    }
