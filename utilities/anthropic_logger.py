"""
Canonical Anthropic API wrapper for Andy's fleet.

ONE file. EVERY project that calls Anthropic imports from here.
Any file that imports `anthropic` directly or calls `client.messages.create(...)`
outside this module is a CI-check violation.

Purpose: guarantee every Anthropic call lands a row in kumori_api_usage
so the admin-API cost_report reconciles against the DB within pennies.
Closes the $131/35d leak investigated repeatedly through 2026-04.

Public API:
    logged_create(app_name, feature, **create_kwargs) -> Message
    logged_stream(app_name, feature, **stream_kwargs) -> context manager yielding SDK stream
    get_client() -> Anthropic  (raw client for tool-loop edge cases)
    log_usage_async(app_name, model, usage, feature, ...) -> None  (fire-and-forget)

Key source: kumori-404602/KUMORI_ANTHROPIC_API_KEY (cross-project read).
DB target:  kumori-404602 Postgres, table kumori_api_usage.

Both reads require the consuming project's service account to have
roles/secretmanager.secretAccessor on kumori-404602 — every App Engine SA
already has this grant per the kumori-infrastructure skill.

All logging is fire-and-forget in a daemon thread. Never blocks the Anthropic
call path. Never raises. Any DB write failure is swallowed with a log warning.
"""
from __future__ import annotations

import os
import re
import time
import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from anthropic import Anthropic, APIError, APIStatusError, APIConnectionError, APITimeoutError, RateLimitError

logger = logging.getLogger("anthropic_logger")

# ─── Model pricing (per token) ────────────────────────────────────────────────
# Ported from scatterbrain/utilities/claude_utils.py MODEL_PRICING on 2026-04-23.
# Update here when rates change or new models ship. Single source.

MODEL_PRICING = {
    # Claude 4.7 / 4.6 flagship Opus — $5/$25 per 1M (verified 2026-05-07
    # against LiteLLM upstream; was incorrectly $15/$75 prior to that, which
    # is what caused the kumori_api_usage drift on opus-4-7 traffic).
    'claude-opus-4-7':         {'input': 0.000005,    'output': 0.000025},
    'claude-opus-4-6':         {'input': 0.000005,    'output': 0.000025},
    'claude-sonnet-4-6':       {'input': 0.000003,    'output': 0.000015},    # $3  / $15

    # Claude 4.5
    'claude-sonnet-4-5':       {'input': 0.000003,    'output': 0.000015},
    'claude-haiku-4-5':        {'input': 0.0000010,   'output': 0.000005},    # $1  / $5  per 1M

    # Claude 4.1 / 4
    'claude-opus-4-1':         {'input': 0.000020,    'output': 0.000080},
    'claude-opus-4':           {'input': 0.000015,    'output': 0.000075},
    'claude-sonnet-4':         {'input': 0.000003,    'output': 0.000015},

    # Claude 3.x
    'claude-3-7-sonnet':       {'input': 0.000003,    'output': 0.000015},
    'claude-3-5-sonnet':       {'input': 0.000003,    'output': 0.000015},
    'claude-3-5-haiku':        {'input': 0.00000025,  'output': 0.00000125},
    'claude-3-haiku':          {'input': 0.00000025,  'output': 0.00000125},

    # Fallback (Sonnet-tier pricing)
    'default':                 {'input': 0.000003,    'output': 0.000015},
}

CACHE_WRITE_MULT = 1.25  # cache creation costs 1.25x input rate
CACHE_READ_MULT  = 0.10  # cache read costs 0.10x input rate
WEB_SEARCH_COST  = 0.01  # flat per web search request


# ─── DB-backed pricing (auto-refreshed daily from LiteLLM upstream JSON) ──────
# kumori_model_pricing table is populated by kumori's /cron/anthropic-pricing-refresh.
# We read with a 5-min in-process TTL cache. Any DB miss/error → static dict above.

_DB_PRICING_CACHE: dict | None = None
_DB_PRICING_FETCHED_AT: float = 0.0
_DB_PRICING_TTL_SEC = 300


def _load_db_pricing() -> dict:
    """Return {model_id: {input, output, cache_write_abs, cache_read_abs}}.
    Empty dict on any error — caller falls back to MODEL_PRICING."""
    global _DB_PRICING_CACHE, _DB_PRICING_FETCHED_AT
    now = time.time()
    if _DB_PRICING_CACHE is not None and (now - _DB_PRICING_FETCHED_AT) < _DB_PRICING_TTL_SEC:
        return _DB_PRICING_CACHE
    try:
        import psycopg2
        creds = _get_db_creds()
        is_gcp = os.environ.get('GAE_ENV', '').startswith('standard') or os.path.exists('/cloudsql')
        if is_gcp:
            socket_dir = os.environ.get('DB_SOCKET_DIR', '/cloudsql')
            host = f"{socket_dir}/{creds['connection_name']}"
        else:
            host = creds['host']
        conn = psycopg2.connect(
            host=host, dbname=creds['dbname'], user=creds['user'],
            password=creds['password'], connect_timeout=5,
            options='-c statement_timeout=5000',
        )
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT model_id,
                       input_cost_per_token::float,
                       output_cost_per_token::float,
                       cache_write_cost_per_token::float,
                       cache_read_cost_per_token::float
                FROM kumori_model_pricing
            """)
            out = {}
            for mid, inp, outp, cw, cr in cur.fetchall():
                d = {'input': float(inp or 0), 'output': float(outp or 0)}
                if cw is not None:
                    d['cache_write_abs'] = float(cw)
                if cr is not None:
                    d['cache_read_abs'] = float(cr)
                out[mid.lower()] = d
            _DB_PRICING_CACHE = out
            _DB_PRICING_FETCHED_AT = now
            return out
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"anthropic_logger: DB pricing fetch failed, using static fallback: {e}")
        # Cache the empty dict for the TTL so we don't hammer the DB on every call.
        _DB_PRICING_CACHE = {}
        _DB_PRICING_FETCHED_AT = now
        return {}


def _pricing_for(model: str) -> dict:
    m = (model or '').lower()

    # 1. Try DB first — exact match wins, then substring containment.
    db = _load_db_pricing()
    if m in db:
        return db[m]
    for mid, p in db.items():
        if mid in m or m in mid:
            return p

    # 2. Static dict fallback (substring match preserves legacy behavior).
    for key, p in MODEL_PRICING.items():
        if key in m:
            return p
    return MODEL_PRICING['default']


# ─── Secret Manager + Anthropic client (cached module-level) ──────────────────

_KEY_CACHE = None
_CLIENT = None
# Env-overridable so a fully-decoupled app (one fully-decoupled app) points at its OWN
# project + key WITHOUT forking this canonical logger. Defaults preserve kumori
# for every other app (env unset -> identical behavior).
_KUMORI_PROJECT = os.environ.get('ANTHROPIC_SECRET_PROJECT', 'kumori-404602')
_ANTHROPIC_SECRET_NAME = os.environ.get('ANTHROPIC_SECRET_NAME', 'KUMORI_ANTHROPIC_API_KEY')


def _get_api_key() -> str:
    """Fetch KUMORI_ANTHROPIC_API_KEY from kumori-404602 Secret Manager.
    Cached after first call. Falls back to ANTHROPIC_API_KEY env var for local dev."""
    global _KEY_CACHE
    if _KEY_CACHE:
        return _KEY_CACHE
    env_key = os.environ.get('ANTHROPIC_API_KEY')
    if env_key and env_key.startswith('sk-ant-api'):
        _KEY_CACHE = env_key
        return _KEY_CACHE
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{_KUMORI_PROJECT}/secrets/{_ANTHROPIC_SECRET_NAME}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        _KEY_CACHE = resp.payload.data.decode("UTF-8")
        return _KEY_CACHE
    except Exception as e:
        raise RuntimeError(
            f"anthropic_logger: could not fetch KUMORI_ANTHROPIC_API_KEY from "
            f"{_KUMORI_PROJECT} Secret Manager: {e}. Ensure this process's "
            f"service account has roles/secretmanager.secretAccessor on {_KUMORI_PROJECT}."
        ) from e


def get_client() -> Anthropic:
    """Returns the shared Anthropic client (cached). Use only when you need raw
    access for tool loops or other patterns that logged_create / logged_stream
    don't cover. If you call client.messages.create() yourself, you MUST also
    call log_usage_async(...) afterwards."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _wrap_for_trace(Anthropic(api_key=_get_api_key(), timeout=60.0, max_retries=1))
    return _CLIENT


def new_client(**kwargs) -> Anthropic:
    """Return a FRESH Anthropic client with the canonical API key + any extra
    kwargs (timeout, max_retries, etc.). Use when existing wrapper classes
    need per-instance config that get_client()'s cached defaults don't fit.
    Caller still MUST pair messages.create() with log_usage_async() to land
    a row in kumori_api_usage."""
    return _wrap_for_trace(Anthropic(api_key=_get_api_key(), **kwargs))


# ─── Forensic call-tracing wrapper (2026-05-17, $0.38/hr sonnet-4-5 leak hunt)
#
# Wraps every Anthropic client returned by get_client()/new_client() so that
# any messages.create() / messages.stream() call which is NOT bracketed by
# logged_create/logged_stream gets a row in kumori_anthropic_call_trace with
# the caller's file:line. logged_create/logged_stream set a thread-local to
# skip tracing — only UNLOGGED calls land in trace. So:
#     SELECT * FROM kumori_anthropic_call_trace WHERE model = '...' AND created_at >= '<leak hour>'
# returns the exact set of calls that bypass kumori_api_usage. Independent
# table — never affects reconciliation, only forensics.
import inspect

_skip_trace_local = threading.local()
_THIS_FILE = os.path.abspath(__file__)
_TRACE_TABLE_READY = False


def _caller_site() -> tuple[str, int, str]:
    """First stack frame outside this module."""
    for frame in inspect.stack()[1:]:
        try:
            fn = os.path.abspath(frame.filename)
        except Exception:
            continue
        if fn == _THIS_FILE:
            continue
        if fn.endswith('anthropic_logger.py'):
            continue
        return fn, frame.lineno, frame.function
    return ('unknown', 0, 'unknown')


def _infer_app_from_file(p: str) -> str:
    parts = os.path.abspath(p).split(os.sep)
    try:
        i = parts.index('code')
        return parts[i + 1]
    except (ValueError, IndexError):
        return os.environ.get('GAE_SERVICE') or os.environ.get('K_SERVICE') or 'unknown'


def _ensure_trace_table():
    global _TRACE_TABLE_READY
    if _TRACE_TABLE_READY:
        return
    import psycopg2
    creds = _get_db_creds()
    is_gcp = os.environ.get('GAE_ENV', '').startswith('standard') or os.path.exists('/cloudsql')
    if is_gcp:
        host = f"{os.environ.get('DB_SOCKET_DIR', '/cloudsql')}/{creds['connection_name']}"
    else:
        host = creds['host']
    conn = psycopg2.connect(
        host=host, dbname=creds['dbname'], user=creds['user'],
        password=creds['password'], connect_timeout=5,
    )
    try:
        import psycopg2.errors
        cur = conn.cursor()
        # Skip the bootstrap DDL when the table already exists. telemetry_writer is
        # INSERT-only (no CREATE on the schema), and Postgres runs the schema
        # ACL_CREATE check BEFORE the IF-NOT-EXISTS short-circuit — so even a no-op
        # CREATE ... IF NOT EXISTS makes the engine LOG 'permission denied for schema'
        # (which the cross-project error digest reports). to_regclass is a silent
        # existence check needing only schema USAGE (which the role has); use the
        # SAME unqualified name as the CREATE so search_path resolution matches.
        cur.execute("SELECT to_regclass('kumori_anthropic_call_trace')")
        if cur.fetchone()[0] is None:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS kumori_anthropic_call_trace (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        host_app TEXT,
                        caller_file TEXT,
                        caller_line INT,
                        caller_func TEXT,
                        model TEXT,
                        streaming BOOLEAN DEFAULT FALSE,
                        input_tokens BIGINT,
                        output_tokens BIGINT,
                        cache_creation_tokens BIGINT,
                        cache_read_tokens BIGINT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trace_created ON kumori_anthropic_call_trace(created_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trace_model_created ON kumori_anthropic_call_trace(model, created_at DESC)")
                conn.commit()
            except psycopg2.errors.InsufficientPrivilege:
                # Genuinely-fresh DB and this role can't create it — the superuser
                # provisions it elsewhere. One engine-log line until then, not per-call.
                conn.rollback()
        _TRACE_TABLE_READY = True
    finally:
        conn.close()


def _record_trace_async(model, usage, caller_file, caller_line, caller_func, streaming):
    """Fire-and-forget insert into kumori_anthropic_call_trace. Never raises."""
    def _go():
        try:
            _ensure_trace_table()
            import psycopg2
            creds = _get_db_creds()
            is_gcp = os.environ.get('GAE_ENV', '').startswith('standard') or os.path.exists('/cloudsql')
            if is_gcp:
                host = f"{os.environ.get('DB_SOCKET_DIR', '/cloudsql')}/{creds['connection_name']}"
            else:
                host = creds['host']
            conn = psycopg2.connect(
                host=host, dbname=creds['dbname'], user=creds['user'],
                password=creds['password'], connect_timeout=5,
                options='-c statement_timeout=10000',
            )
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO kumori_anthropic_call_trace
                    (host_app, caller_file, caller_line, caller_func, model, streaming,
                     input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    _infer_app_from_file(caller_file),
                    caller_file, caller_line, caller_func,
                    _canonical_model_id(model or 'unknown'), streaming,
                    _usage_field(usage, 'input_tokens'),
                    _usage_field(usage, 'output_tokens'),
                    _usage_field(usage, 'cache_creation_input_tokens'),
                    _usage_field(usage, 'cache_read_input_tokens'),
                ))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"anthropic_logger: trace insert failed: {e}")
    sync = bool(os.environ.get('K_SERVICE'))
    if sync:
        _go()
    else:
        threading.Thread(target=_go, daemon=True).start()


class _TracingMessages:
    def __init__(self, real):
        self._real = real

    def create(self, **kwargs):
        if getattr(_skip_trace_local, 'skip', False):
            return self._real.create(**kwargs)
        cf, cl, fn = _caller_site()
        resp = self._real.create(**kwargs)
        try:
            _record_trace_async(kwargs.get('model'), getattr(resp, 'usage', None), cf, cl, fn, False)
        except Exception:
            pass
        return resp

    def stream(self, **kwargs):
        if getattr(_skip_trace_local, 'skip', False):
            return self._real.stream(**kwargs)
        cf, cl, fn = _caller_site()
        real_cm = self._real.stream(**kwargs)
        model = kwargs.get('model')

        class _TracingStreamCM:
            def __enter__(_self):
                _self._stream = real_cm.__enter__()
                return _self._stream

            def __exit__(_self, *args):
                try:
                    final = _self._stream.get_final_message()
                    _record_trace_async(model, final.usage, cf, cl, fn, True)
                except Exception:
                    pass
                return real_cm.__exit__(*args)
        return _TracingStreamCM()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TracingAnthropic:
    """Duck-typed proxy that intercepts .messages.create / .messages.stream."""
    def __init__(self, real):
        self._real = real
        self.messages = _TracingMessages(real.messages)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _wrap_for_trace(real_client):
    if os.environ.get('ANTHROPIC_TRACE_DISABLE', '').strip() in ('1', 'true', 'yes'):
        return real_client
    return _TracingAnthropic(real_client)


# ─── DB logging (fire-and-forget) ─────────────────────────────────────────────

_DB_CREDS_CACHE = None


def _get_db_creds() -> dict:
    global _DB_CREDS_CACHE
    if _DB_CREDS_CACHE:
        return _DB_CREDS_CACHE
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()

    def fetch(name: str) -> str:
        path = f"projects/{_KUMORI_PROJECT}/secrets/{name}/versions/latest"
        return client.access_secret_version(request={"name": path}).payload.data.decode("UTF-8")

    def fetch_or(primary: str, fallback: str) -> str:
        # Connect as the dedicated least-privilege telemetry_writer role (INSERT-
        # only on the telemetry tables) rather than the shared postgres role, so
        # the logger can never reach another tenant's data. Falls back to KUMORI_*
        # until TELEMETRY_* is provisioned, so no flag day.
        try:
            return fetch(primary)
        except Exception:
            return fetch(fallback)

    _DB_CREDS_CACHE = {
        'host': fetch('KUMORI_POSTGRES_IP'),
        'dbname': fetch('KUMORI_POSTGRES_DB_NAME'),
        'user': fetch_or('TELEMETRY_POSTGRES_USERNAME', 'KUMORI_POSTGRES_USERNAME'),
        'password': fetch_or('TELEMETRY_POSTGRES_PASSWORD', 'KUMORI_POSTGRES_PASSWORD'),
        'connection_name': fetch('KUMORI_POSTGRES_CONNECTION_NAME'),
    }
    return _DB_CREDS_CACHE


def _usage_field(usage: Any, key: str, default: int = 0) -> int:
    """Read a usage field from either an Anthropic SDK usage object or a dict."""
    if usage is None:
        return default
    val = getattr(usage, key, None)
    if val is not None:
        return val or default
    if isinstance(usage, dict):
        return usage.get(key, default) or default
    return default


def _compute_cost(model: str, usage: Any) -> float:
    p = _pricing_for(model)
    i = _usage_field(usage, 'input_tokens')
    o = _usage_field(usage, 'output_tokens')
    cc = _usage_field(usage, 'cache_creation_input_tokens')
    cr = _usage_field(usage, 'cache_read_input_tokens')
    th = _usage_field(usage, 'thinking_tokens')

    # server-side tool usage lives in usage.server_tool_use
    server = getattr(usage, 'server_tool_use', None) or (
        usage.get('server_tool_use', {}) if isinstance(usage, dict) else {}
    ) or {}
    ws = _usage_field(server, 'web_search_requests')

    cache_write_rate = p.get('cache_write_abs', p['input'] * CACHE_WRITE_MULT)
    cache_read_rate  = p.get('cache_read_abs',  p['input'] * CACHE_READ_MULT)

    return (
        i * p['input']
        + o * p['output']
        + cc * cache_write_rate
        + cr * cache_read_rate
        + th * p['output']
        + ws * WEB_SEARCH_COST
    )


_DATED_MODEL_RE = re.compile(r'-(\d{8})$')


def _canonical_model_id(model: str) -> str:
    """Strip Anthropic dated suffix (-YYYYMMDD) so e.g.
    claude-haiku-4-5-20251001 → claude-haiku-4-5. Anthropic's admin usage_report
    always returns the dated form; the API accepts both. If we let the dated
    form into kumori_api_usage, the hourly reconciler sees the same hour's
    traffic split across two model rows and fires a false-positive leak alert.
    Apply this at the write boundary so the column is always canonical."""
    if not model:
        return model
    return _DATED_MODEL_RE.sub('', model)


def _insert_usage_row(*, app_name: str, model: str, usage: Any,
                      feature: Optional[str], user_id: Optional[str],
                      duration_ms: Optional[int], streaming: bool,
                      image_count: int):
    """Blocking INSERT into kumori_api_usage. Called from daemon thread."""
    import psycopg2
    creds = _get_db_creds()

    is_gcp = os.environ.get('GAE_ENV', '').startswith('standard') or os.path.exists('/cloudsql')
    if is_gcp:
        socket_dir = os.environ.get('DB_SOCKET_DIR', '/cloudsql')
        host = f"{socket_dir}/{creds['connection_name']}"
    else:
        host = creds['host']

    i = _usage_field(usage, 'input_tokens')
    o = _usage_field(usage, 'output_tokens')
    cc = _usage_field(usage, 'cache_creation_input_tokens')
    cr = _usage_field(usage, 'cache_read_input_tokens')
    th = _usage_field(usage, 'thinking_tokens')

    server = getattr(usage, 'server_tool_use', None) or (
        usage.get('server_tool_use', {}) if isinstance(usage, dict) else {}
    ) or {}
    ws = _usage_field(server, 'web_search_requests')
    wf = _usage_field(server, 'web_fetch_requests')
    ce = _usage_field(server, 'code_execution_requests')

    model = _canonical_model_id(model)
    cost = _compute_cost(model, usage)

    conn = psycopg2.connect(
        host=host, dbname=creds['dbname'], user=creds['user'],
        password=creds['password'], connect_timeout=5,
        options='-c statement_timeout=10000',
    )
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kumori_api_usage
            (app_name, feature, model, input_tokens, output_tokens,
             cache_creation_tokens, cache_read_tokens, thinking_tokens,
             web_search_requests, web_fetch_requests, code_execution_requests,
             image_count, estimated_cost_usd, streaming, user_id, duration_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (app_name, feature, model, i, o, cc, cr, th,
              ws, wf, ce, image_count, cost, streaming, user_id, duration_ms))
        conn.commit()
    finally:
        conn.close()


def log_usage_async(*, app_name: str, model: str, usage: Any,
                    feature: Optional[str] = None, user_id: Optional[str] = None,
                    duration_ms: Optional[int] = None, streaming: bool = False,
                    image_count: int = 0, sync: Optional[bool] = None) -> None:
    """Log to kumori_api_usage.

    In long-running environments (App Engine, local) this spawns a daemon thread
    and returns immediately — fire-and-forget, never blocks.

    In Cloud Run (detected via K_SERVICE env var) the log is done SYNCHRONOUSLY
    before returning, because Cloud Run aggressively scales containers down
    seconds after a request finishes and will kill daemon threads mid-INSERT.
    Waiting for the INSERT adds ~50-200ms to the request — acceptable for the
    guarantee that every call lands a row.

    Pass sync=True explicitly to force a blocking write regardless of platform —
    REQUIRED for non-streaming / batch / cron / artifact calls on App Engine,
    whose instances can be reaped before a daemon thread flushes (this dropped
    rows while Anthropic still billed — a recurring leak-detector source).
    sync=None (default) keeps the auto behavior; ANTHROPIC_LOGGER_SYNC=1/0 still
    overrides when no explicit sync is passed.

    Never raises. DB failures are swallowed with a logger.warning.
    """
    if sync is None:
        sync_override = os.environ.get('ANTHROPIC_LOGGER_SYNC', '').strip()
        if sync_override in ('1', 'true', 'True', 'yes'):
            sync = True
        elif sync_override in ('0', 'false', 'False', 'no'):
            sync = False
        else:
            # Default: sync on Cloud Run, async elsewhere
            sync = bool(os.environ.get('K_SERVICE'))

    def _do():
        try:
            _insert_usage_row(
                app_name=app_name, model=model, usage=usage,
                feature=feature, user_id=user_id, duration_ms=duration_ms,
                streaming=streaming, image_count=image_count,
            )
            logger.info(f"anthropic_logger: logged {app_name}/{feature or '?'} to kumori_api_usage")
        except Exception as e:
            # Never raise. Warn and drop.
            logger.warning(f"anthropic_logger: kumori_api_usage INSERT failed: {e}")

    if sync:
        _do()
    else:
        threading.Thread(target=_do, daemon=True).start()


# ─── Primary public API: wrapped create + stream ──────────────────────────────

def logged_create(*, app_name: str, feature: Optional[str] = None,
                  user_id: Optional[str] = None, image_count: int = 0,
                  **create_kwargs) -> Any:
    """Drop-in replacement for client.messages.create(...). Auto-logs usage.
    Usage:
        msg = logged_create(app_name='kumori', feature='chat', user_id=uid,
                            model='claude-sonnet-4-6', max_tokens=4096,
                            messages=[...])
    """
    # Kumori central killswitch — raises KillswitchTripped if MTD Anthropic
    # spend across all kumori-family apps has crossed the cap. Apps without
    # utilities/killswitch.py installed are unaffected (fail-open).
    try:
        from utilities.killswitch import check_killswitch
        check_killswitch('anthropic')
    except ImportError:
        pass
    t0 = time.time()
    model = create_kwargs.get('model', 'unknown')
    client = get_client()
    _skip_trace_local.skip = True
    try:
        response = client.messages.create(**create_kwargs)
    finally:
        _skip_trace_local.skip = False
    dur = int((time.time() - t0) * 1000)
    log_usage_async(
        app_name=app_name, model=model, usage=response.usage,
        feature=feature, user_id=user_id, duration_ms=dur,
        streaming=False, image_count=image_count,
    )
    return response


@contextmanager
def logged_stream(*, app_name: str, feature: Optional[str] = None,
                  user_id: Optional[str] = None, image_count: int = 0,
                  **stream_kwargs) -> Iterator[Any]:
    """Context manager wrapping client.messages.stream(...). Logs final usage on exit.
    Usage:
        with logged_stream(app_name='kumori', feature='chat_stream', user_id=uid,
                           model='...', messages=[...]) as stream:
            for text in stream.text_stream:
                yield text
    """
    try:
        from utilities.killswitch import check_killswitch
        check_killswitch('anthropic')
    except ImportError:
        pass
    t0 = time.time()
    model = stream_kwargs.get('model', 'unknown')
    client = get_client()
    _skip_trace_local.skip = True
    try:
        _stream_cm = client.messages.stream(**stream_kwargs)
    finally:
        _skip_trace_local.skip = False
    with _stream_cm as stream:
        try:
            yield stream
        finally:
            try:
                final = stream.get_final_message()
                usage = final.usage
                dur = int((time.time() - t0) * 1000)
                log_usage_async(
                    app_name=app_name, model=model, usage=usage,
                    feature=feature, user_id=user_id, duration_ms=dur,
                    streaming=True, image_count=image_count,
                )
            except Exception as e:
                logger.warning(f"anthropic_logger: logged_stream exit-log failed: {e}")


# Public API
__all__ = [
    'logged_create',
    'logged_stream',
    'get_client',
    'new_client',
    'log_usage_async',
    'MODEL_PRICING',
    # Re-exported SDK exception types so consumers never need to `import anthropic`
    'APIError',
    'APIStatusError',
    'APIConnectionError',
    'APITimeoutError',
    'RateLimitError',
]
