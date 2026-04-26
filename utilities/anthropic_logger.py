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
    # Claude 4.6 flagship
    'claude-opus-4-6':         {'input': 0.000015,    'output': 0.000075},    # $15 / $75 per 1M
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


def _pricing_for(model: str) -> dict:
    m = (model or '').lower()
    for key, p in MODEL_PRICING.items():
        if key in m:
            return p
    return MODEL_PRICING['default']


# ─── Secret Manager + Anthropic client (cached module-level) ──────────────────

_KEY_CACHE = None
_CLIENT = None
_KUMORI_PROJECT = 'kumori-404602'


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
        name = f"projects/{_KUMORI_PROJECT}/secrets/KUMORI_ANTHROPIC_API_KEY/versions/latest"
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
        _CLIENT = Anthropic(api_key=_get_api_key(), timeout=60.0, max_retries=1)
    return _CLIENT


def new_client(**kwargs) -> Anthropic:
    """Return a FRESH Anthropic client with the canonical API key + any extra
    kwargs (timeout, max_retries, etc.). Use when existing wrapper classes
    need per-instance config that get_client()'s cached defaults don't fit.
    Caller still MUST pair messages.create() with log_usage_async() to land
    a row in kumori_api_usage."""
    return Anthropic(api_key=_get_api_key(), **kwargs)


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

    _DB_CREDS_CACHE = {
        'host': fetch('KUMORI_POSTGRES_IP'),
        'dbname': fetch('KUMORI_POSTGRES_DB_NAME'),
        'user': fetch('KUMORI_POSTGRES_USERNAME'),
        'password': fetch('KUMORI_POSTGRES_PASSWORD'),
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

    return (
        i * p['input']
        + o * p['output']
        + cc * p['input'] * CACHE_WRITE_MULT
        + cr * p['input'] * CACHE_READ_MULT
        + th * p['output']
        + ws * WEB_SEARCH_COST
    )


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
                    image_count: int = 0) -> None:
    """Log to kumori_api_usage.

    In long-running environments (App Engine, local) this spawns a daemon thread
    and returns immediately — fire-and-forget, never blocks.

    In Cloud Run (detected via K_SERVICE env var) the log is done SYNCHRONOUSLY
    before returning, because Cloud Run aggressively scales containers down
    seconds after a request finishes and will kill daemon threads mid-INSERT.
    Waiting for the INSERT adds ~50-200ms to the request — acceptable for the
    guarantee that every call lands a row.

    Never raises. DB failures are swallowed with a logger.warning.
    Can be forced with ANTHROPIC_LOGGER_SYNC=1 / ANTHROPIC_LOGGER_SYNC=0.
    """
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
    t0 = time.time()
    model = create_kwargs.get('model', 'unknown')
    client = get_client()
    response = client.messages.create(**create_kwargs)
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
    t0 = time.time()
    model = stream_kwargs.get('model', 'unknown')
    client = get_client()
    with client.messages.stream(**stream_kwargs) as stream:
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
