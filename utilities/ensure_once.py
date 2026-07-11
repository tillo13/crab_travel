"""Process-level run-once guard for idempotent schema-ensure DDL.

The shared Cloud SQL db-f1-micro is hit by every kumori app; a `CREATE TABLE
IF NOT EXISTS` that re-runs on every request slows the whole fleet (see
~/.claude/skills/db-speed-first — "SPEED IS THE PRODUCT"). Decorate any DDL
ensure-fn with @ensure_once so its DDL runs at most once per process.

    @ensure_once
    def _ensure_widgets(cur):
        cur.execute("CREATE TABLE IF NOT EXISTS ...")

Semantics:
  - First call runs the body and caches its return value.
  - Later calls return the cached value WITHOUT touching the DB (args ignored —
    DDL is global, not per-arg).
  - If the first call raises (e.g. DB not yet reachable at cold start), the
    guard stays open so the next call retries. The app.py startup block already
    wraps these in try/except and defers on failure.
"""
import functools


def ensure_once(fn):
    state = {"done": False, "result": None}

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if state["done"]:
            return state["result"]
        result = fn(*args, **kwargs)
        state["done"] = True
        state["result"] = result
        return result

    wrapper._ensure_once = True
    return wrapper
