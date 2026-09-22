"""DB connection pool + canonical cursor pattern, for EVERY app on the shared Cloud SQL.

This file is the one implementation (task #170, 2026-09-22). Other apps do not fork it:
their deploy.json vendors it on every deploy
    {"src": "kumori/utilities/postgres_utils/connection.py", "dst": "utilities/kumori_db.py"}
    {"src": "kumori/utilities/google_secrets_utils.py", "dst": "utilities/google_secrets_utils.py"}
and their own utilities/postgres_utils.py is a thin shim onto it. Per-app settings come from
app.yaml env_variables; the defaults are kumori's:
    DB_ROLE           the app's Postgres role                 (kumori_app / kumori_batch)
    DB_SECRET_PREFIX  <PREFIX>_POSTGRES_USERNAME/_PASSWORD    (KUMORI_APP / KUMORI_BATCH)
    DB_POOL_MAX       connections per process                 (10 / KUMORI_BATCH_POOL_MAX)
    KUMORI_DB_AUTH    'iam' = log in as the app's service account, no password read

Exposes get_db_connection (returns a PooledConnection that returns to the pool
on close) and the @contextmanager db_cursor for the new code path. Per
db-speed-first: existing 30+ call sites stay as-is until touched naturally;
new code MUST use db_cursor."""
import os
import threading
import logging
from contextlib import contextmanager
from typing import Dict

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

_credentials_cache = {}
_connection_pools = {}
_pool_semaphores = {}   # project -> BoundedSemaphore(maxconn): checkout gate (see get_db_connection)
_pool_lock = threading.Lock()
# gthread workers (app.yaml: 1x16) mean up to 16 threads per process contest the
# 10-conn pool, and psycopg2's ThreadedConnectionPool raises PoolError instantly
# instead of waiting. Gate checkout with a semaphore so threads queue briefly
# for a slot; only raise after POOL_WAIT_SECS of real starvation.
POOL_WAIT_SECS = 10
POOL_WAIT_WARN_MS = 500
# Ceiling on the liveness-probe/retry loop in get_db_connection, which holds a
# semaphore slot the whole time it runs. Keep the product under POOL_WAIT_SECS so
# a caller stuck probing cannot outlast the callers queued behind it.
CHECKOUT_PROBE_TRIES = 3
CHECKOUT_DEADLINE_SECS = 8
DB_TIER = os.environ.get('KUMORI_DB_TIER', 'serving')
DB_SECRET_PREFIX = os.environ.get('DB_SECRET_PREFIX') or ('KUMORI_BATCH' if DB_TIER == 'batch' else 'KUMORI_APP')
POOL_MAX = int(os.environ.get('DB_POOL_MAX')
               or (os.environ.get('KUMORI_BATCH_POOL_MAX', '4') if DB_TIER == 'batch' else 10))
# Cloud SQL IAM database authentication (2026-09-21): on GCP with KUMORI_DB_AUTH=iam the app
# logs in as its own service account (DB user '<sa email minus .gserviceaccount.com>') with a
# one-hour OAuth token as the password, then runs as DB_ROLE. No DB password is stored or read.
# The IAM user must be a member of DB_ROLE with `ALTER ROLE ... SET role = DB_ROLE`, so tables,
# grants and the connection cap are the password role's. Falls back to the password path (same
# role, same privilege) if the IAM login fails, until every app has proven it on a cold start.
DB_AUTH = os.environ.get('KUMORI_DB_AUTH', 'password')
DB_ROLE = os.environ.get('DB_ROLE') or ('kumori_batch' if DB_TIER == 'batch' else 'kumori_app')
# Per-connection, because Postgres applies ALTER ROLE ... SET only for the LOGIN role: under IAM
# the login is the service account, so the app role's own search_path would never take effect.
DB_SEARCH_PATH = os.environ.get('DB_SEARCH_PATH', '')
_SESSION_OPTS = ('-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000'
                 + (f" -c search_path={DB_SEARCH_PATH.replace(' ', '')}" if DB_SEARCH_PATH else ''))
# Apps written against psycopg 3's dict_row read rows as row['col']; DB_DICT_ROWS=1 keeps that.
_CURSOR_KW = ({'cursor_factory': psycopg2.extras.RealDictCursor}
              if os.environ.get('DB_DICT_ROWS') == '1' else {})
_iam_creds = None


def _iam_token():
    """A Cloud SQL login token for this service account, refreshed 5 minutes before expiry."""
    global _iam_creds
    import datetime
    import google.auth
    from google.auth.transport.requests import Request
    if _iam_creds is None:
        _iam_creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/sqlservice.login'])
    expiry = getattr(_iam_creds, 'expiry', None)
    if not _iam_creds.valid or (expiry and expiry - datetime.datetime.utcnow() < datetime.timedelta(minutes=5)):
        _iam_creds.refresh(Request())
    return _iam_creds.token


def _iam_db_user():
    _iam_token()  # resolves service_account_email on metadata-server credentials
    return _iam_creds.service_account_email.removesuffix('.gserviceaccount.com')


class _IAMConnectionPool(psycopg2.pool.ThreadedConnectionPool):
    """Every new physical connection gets a fresh token; pooled connections outlive their token."""
    def _connect(self, key=None):
        self._kwargs['password'] = _iam_token()
        return super()._connect(key)
# Background work (a cron and the threads it fans out) may hold at most this many of the pool's
# connections at once, so a user request always finds a slot. Measured 2026-09-18: the 21:00 PT
# LLM canary fanned 50 probe threads into this 10-connection pool every night, each probe writing
# its call record and breaker state, and users waited a median 3.3s for a connection while it ran
# (1,061 contention warnings in four days, 33 requests gave up at 10s). Opt in with background_db().
BACKGROUND_DB_SHARE = max(1, POOL_MAX // 3)
BACKGROUND_WAIT_SECS = 60
_bg_state = threading.local()
_bg_semaphores = {}
_bg_lock = threading.Lock()


@contextmanager
def background_db():
    """Mark this thread's database use as background work for the duration: it queues for one of
    BACKGROUND_DB_SHARE slots before it may take a pool connection. Nesting is fine."""
    prev = getattr(_bg_state, 'on', False)
    _bg_state.on = True
    try:
        yield
    finally:
        _bg_state.on = prev


def _background_semaphore(gcp_project_id):
    with _bg_lock:
        sem = _bg_semaphores.get(gcp_project_id)
        if sem is None:
            sem = _bg_semaphores[gcp_project_id] = threading.BoundedSemaphore(BACKGROUND_DB_SHARE)
        return sem
if DB_TIER == 'batch' and not 1 <= POOL_MAX <= 4:
    raise ValueError('KUMORI_BATCH_POOL_MAX must be between 1 and 4')


def get_secret(secret_id: str, gcp_project_id: str = None) -> str:
    """Fetch secret from Google Secret Manager with in-memory cache."""
    from utilities.google_secrets_utils import get_secret as _get_secret
    return _get_secret(secret_id)

def get_postgres_credentials(gcp_project_id: str) -> Dict[str, str]:
    """Get database credentials - cached to avoid repeated Secret Manager calls"""
    global _credentials_cache

    if gcp_project_id in _credentials_cache:
        return _credentials_cache[gcp_project_id]

    # The host app connects as the least-priv kumori_app role (owns its own
    # tables; no superuser reach into other tenants' data). There is deliberately
    # NO fallback to KUMORI_POSTGRES_*: that pair is the `postgres` SUPERUSER, so
    # the old "fall back on a falsy value or an exception" made an absent or
    # denied app secret escalate instead of fail. A fallback must never be more
    # privileged than the primary (CWE-636). KUMORI_APP_POSTGRES_* has been
    # provisioned since 2026-08; if it is unreadable, fail loudly here.
    creds = {
        'host': get_secret('KUMORI_POSTGRES_IP', gcp_project_id),
        'dbname': get_secret('KUMORI_POSTGRES_DB_NAME', gcp_project_id),
        'user': get_secret(f'{DB_SECRET_PREFIX}_POSTGRES_USERNAME'),
        'password': get_secret(f'{DB_SECRET_PREFIX}_POSTGRES_PASSWORD'),
        'connection_name': get_secret('KUMORI_POSTGRES_CONNECTION_NAME', gcp_project_id),
    }
    _credentials_cache[gcp_project_id] = creds
    return creds

def _app_identity() -> str:
    """Per-app label for pg_stat_activity.application_name so the shared db's
    by_app attribution (/api/admin/postgres, db-health-check app_hog) can tell
    the apps on this instance apart instead of bucketing them as '(unknown)'.
    Prefers the GCP project (the unit that distinguishes apps on the shared
    instance), then the GAE/Cloud Run service. Falls back to 'kumori'.
    Truncated to Postgres's 63-char application_name limit.

    NOTE: this only labels connections opened through THIS canonical pool. Apps
    that run their own connection pool (e.g. galactica's utilities/postgres/core)
    must adopt the same pattern for full fleet-wide by_app attribution."""
    project = (os.environ.get('GOOGLE_CLOUD_PROJECT')
               or os.environ.get('GAE_APPLICATION', '').replace('s~', '')
               or 'kumori')
    service = os.environ.get('GAE_SERVICE') or os.environ.get('K_SERVICE')
    name = f"{project}/{service}" if service and service != 'default' else project
    return name[:63]


def socket_dir():
    return os.environ.get("DB_SOCKET_DIR", "/cloudsql")


def on_gcp():
    """True when the Cloud SQL connector socket is the right way to reach the instance.

    App Engine standard sets GAE_ENV; Cloud Run SERVICES set K_SERVICE; Cloud Run JOBS set
    CLOUD_RUN_JOB and never K_SERVICE. Checking only GAE_ENV meant anything on Cloud Run fell
    through to the instance's PUBLIC IP and sat there until the connect timed out
    (kumori-digests, 2026-09-08) — and public IP is the access pattern we are explicitly not
    supposed to use. The isdir() check is the honest one: if the socket is mounted, use it
    whatever the environment claims.

    Lives here as the single copy on purpose. db_connection_monitor had its own GAE_ENV-only
    duplicate, so fixing this in one place left the other silently on public IP.
    """
    return (os.environ.get('GAE_ENV', '').startswith('standard')
            or bool(os.environ.get('K_SERVICE'))
            or bool(os.environ.get('CLOUD_RUN_JOB'))
            or os.path.isdir(socket_dir()))


def _get_connection_pool(gcp_project_id: str) -> psycopg2.pool.ThreadedConnectionPool:
    """Get or create a connection pool for the given project"""
    global _connection_pools

    with _pool_lock:
        if gcp_project_id in _connection_pools:
            return _connection_pools[gcp_project_id]

        # A vendored copy (utilities/kumori_db.py in another app) must name its own role and
        # secrets. The defaults are kumori's, and a fleet app whose service account can read
        # every secret would otherwise log in as kumori_app, e.g. a Cloud Run service that
        # never sees app.yaml env (pilgrims-burn/ward, 2026-09-22, #170).
        if __name__ != 'utilities.postgres_utils.connection' and not (
                os.environ.get('DB_SECRET_PREFIX') and os.environ.get('DB_ROLE')):
            raise RuntimeError('kumori_db: set DB_SECRET_PREFIX and DB_ROLE for this app '
                               '(app.yaml, or the Cloud Run env in deploy.json)')

        is_gcp = on_gcp()
        if is_gcp and DB_AUTH == 'iam':
            try:
                pool = _IAMConnectionPool(
                    minconn=1, maxconn=POOL_MAX,
                    dbname=get_secret('KUMORI_POSTGRES_DB_NAME', gcp_project_id),
                    user=_iam_db_user(), password='',
                    host=f"{socket_dir()}/{get_secret('KUMORI_POSTGRES_CONNECTION_NAME', gcp_project_id)}",
                    connect_timeout=10, keepalives=1, keepalives_idle=30, keepalives_interval=10,
                    keepalives_count=3, application_name=_app_identity(),
                    options=f'{_SESSION_OPTS} -c role={DB_ROLE}', **_CURSOR_KW)
                _connection_pools[gcp_project_id] = pool
                _pool_semaphores[gcp_project_id] = threading.BoundedSemaphore(pool.maxconn)
                logger.info(f"Created IAM-auth connection pool for {gcp_project_id} as {DB_ROLE}")
                return pool
            except Exception as e:
                logger.error(f"IAM DB auth failed, falling back to password login: {e}")

        db_credentials = get_postgres_credentials(gcp_project_id)
        _socket_dir = socket_dir()

        if is_gcp:
            db_socket_dir = _socket_dir
            cloud_sql_connection_name = db_credentials['connection_name']
            host = f"{db_socket_dir}/{cloud_sql_connection_name}"
        else:
            host = db_credentials['host']

        # Budget: 50 max_connections shared across 8+ apps on db-f1-micro
        # See kumori/docs/postgres_connections.md for allocation
        # kumori app's documented allotment is min=1 max=10. History: 3 -> 5 after
        # the 2026-05-09 pool-exhaustion incident (Sarah's streaming SSE chat held
        # all 3 slots, blocking concurrent /api/chat from Andy); 5 -> 10 after the
        # 2026-07-10 gunicorn 2x8 -> 1x16 change put all 16 threads on ONE pool
        # (was 8 threads per 5-conn pool across two processes). Per-instance DB
        # footprint is unchanged: 2 processes x 5 conns before, 1 x 10 now.
        pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=POOL_MAX,
            dbname=db_credentials['dbname'],
            user=db_credentials['user'],
            password=db_credentials['password'],
            host=host,
            connect_timeout=10,
            # TCP keepalives so the OS keeps idle pooled conns alive / detects
            # drops at the socket layer, cutting how often Cloud SQL silently
            # reaps an idle pooled connection (the 'server closed the connection
            # unexpectedly' in the error digest).
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            application_name=_app_identity(),
            # statement_timeout=30s caps any single query
            # idle_in_transaction_session_timeout=60s reaps stuck txns server-side
            options=(_SESSION_OPTS + (' -c role=kumori_app' if DB_TIER == 'batch' else '')),
            **_CURSOR_KW,
        )
        _connection_pools[gcp_project_id] = pool
        _pool_semaphores[gcp_project_id] = threading.BoundedSemaphore(pool.maxconn)
        logger.info(f"Created connection pool for {gcp_project_id}")
        return pool

class PooledConnection:
    """Wrapper that returns connection to pool on close() instead of closing it"""
    def __init__(self, conn, pool, semaphore=None, background=None):
        self._conn = conn
        self._pool = pool
        self._sem = semaphore
        self._bg = background

    def close(self):
        """Return connection to pool instead of closing"""
        if self._conn:
            try:
                # putconn rolls back open txns but does NOT reset autocommit —
                # without this, a borrower who flipped it would leak an
                # autocommit connection to the next borrower.
                if self._conn.autocommit:
                    self._conn.autocommit = False
                self._pool.putconn(self._conn)
            except Exception:
                pass  # Pool may be closed
            self._conn = None
            if self._sem:
                try:
                    self._sem.release()
                except ValueError:
                    pass  # BoundedSemaphore over-release guard
                self._sem = None
            if self._bg:
                try:
                    self._bg.release()
                except ValueError:
                    pass
                self._bg = None

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        # Delegate writes symmetrically with __getattr__ — without this,
        # `conn.autocommit = True` lands on the wrapper and silently no-ops
        # (three sessions "applied" DDL that rolled back on putconn, 2026-08).
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        self.close()
        return False

def get_admin_connection(gcp_project_id: str = 'kumori-404602', dbname: str = 'postgres'):
    """The ONE way to do role/DDL admin on the shared instance. Local machines only, never app code.

    Logs in as the gcloud-authenticated human through Cloud SQL IAM database authentication
    (a one-hour token, nothing stored), then SET ROLE cloudsqlsuperuser: on Postgres 15 the
    CREATEROLE attribute is not inherited through membership, so role management only works
    after the SET ROLE. As of 2026-09-21 andy.tillo@gmail.com holds that membership (granted via
    the Cloud SQL Admin API, users.update databaseRoles); revoke it the same way.

    This replaced the stored `postgres` password: KUMORI_POSTGRES_SUPERUSER_PASSWORD stopped
    authenticating some time between 2026-08-21 and 2026-09-20, and a vault holding a dead
    superuser password was pushing sessions to improvise privileged logins instead.
    """
    import subprocess
    if on_gcp():
        raise RuntimeError('get_admin_connection is for a human at a local machine, never a deployed app')
    account = subprocess.run(['gcloud', 'config', 'get-value', 'account'],
                             capture_output=True, text=True, timeout=30).stdout.strip().lower()
    token = subprocess.run(['gcloud', 'sql', 'generate-login-token'],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    if not account or not token:
        raise RuntimeError('no gcloud login: run `gcloud auth login` first')
    conn = psycopg2.connect(host=get_secret('KUMORI_POSTGRES_IP', gcp_project_id), dbname=dbname,
                            user=account, password=token, sslmode='require', connect_timeout=15)
    with conn.cursor() as cur:
        cur.execute('SET ROLE cloudsqlsuperuser')
    conn.commit()   # SET ROLE is session-level; end the implicit transaction it opened
    return conn


def get_db_connection(gcp_project_id: str):
    """Get a database connection from the pool

    Returns a PooledConnection that returns to pool on close().
    Works with existing code pattern:
        conn = get_db_connection(...)
        try:
            # work
        finally:
            conn.close()  # Returns to pool, doesn't close
    """
    pool = _get_connection_pool(gcp_project_id)
    # Checkout gate: queue for a slot instead of letting ThreadedConnectionPool
    # raise PoolError the instant all conns are checked out (gthread workers put
    # 8 threads/process on this 5-conn pool). One acquire per caller, released
    # in PooledConnection.close().
    sem = _pool_semaphores.get(gcp_project_id)
    bg = _background_semaphore(gcp_project_id) if getattr(_bg_state, 'on', False) else None
    if bg and not bg.acquire(timeout=BACKGROUND_WAIT_SECS):
        raise psycopg2.pool.PoolError(
            f"background DB share busy (waited {BACKGROUND_WAIT_SECS}s for one of {BACKGROUND_DB_SHARE} slots)")
    if sem:
        _w0 = _perf_counter()
        if not sem.acquire(timeout=POOL_WAIT_SECS):
            if bg:
                bg.release()
            raise psycopg2.pool.PoolError(
                f"connection pool exhausted (waited {POOL_WAIT_SECS}s for a slot)")
        _wait_ms = (_perf_counter() - _w0) * 1000
        if _wait_ms >= POOL_WAIT_WARN_MS:
            logger.warning("DB pool contention: waited %.0fms for a connection slot", _wait_ms)
    try:
        # The probe loop below runs while HOLDING the slot, and every step in it
        # can block for connect_timeout (10s) when Cloud SQL is refusing or slow.
        # Unbounded, that is maxconn x 10s = 100s of one slot held by a caller who
        # has not run a query yet, while everyone else times out at POOL_WAIT_SECS
        # and raises "pool exhausted" — 38 of those on 2026-09-15, interleaved with
        # the `timeout expired` / `Connection refused` socket errors that caused
        # them. Cap the retries and the wall clock so a bad backend degrades one
        # request instead of cascading across the pool.
        _deadline = _perf_counter() + CHECKOUT_DEADLINE_SECS
        for _ in range(CHECKOUT_PROBE_TRIES):
            if _perf_counter() > _deadline:
                raise psycopg2.pool.PoolError(
                    f"no live connection within {CHECKOUT_DEADLINE_SECS}s "
                    f"(pool had only stale/unreachable connections)")
            conn = pool.getconn()
            # Liveness probe: a pooled conn can be silently dropped server-side
            # (Cloud SQL idle reaping / instance recycle) while idle in the pool.
            # psycopg2's pool never validates on getconn(), so probe here and discard
            # dead conns instead of handing a stale one to the caller.
            if getattr(conn, 'closed', 0):
                pool.putconn(conn, close=True)
                continue
            try:
                cur = conn.cursor()
                cur.execute('SELECT 1')
                cur.close()
                # end the probe's implicit txn — hand the conn out clean, not
                # idle-in-transaction (also lets callers set session flags
                # like autocommit, which raise mid-transaction)
                conn.rollback()
            except psycopg2.Error:
                pool.putconn(conn, close=True)  # drop dead conn; pool makes a fresh one
                continue
            return PooledConnection(conn, pool, sem, bg)
        # Every try came back stale. One last unprobed connection is still better
        # than raising — the caller's own query surfaces a real error — but only
        # if there is deadline left to open it in.
        if _perf_counter() > _deadline:
            raise psycopg2.pool.PoolError(
                f"no live connection within {CHECKOUT_DEADLINE_SECS}s "
                f"({CHECKOUT_PROBE_TRIES} stale connections discarded)")
        conn = pool.getconn()
        return PooledConnection(conn, pool, sem, bg)
    except Exception:
        for _s in (sem, bg):
            if _s:
                try:
                    _s.release()
                except ValueError:
                    pass
        raise


# ── Runtime DB-speed instrumentation (tier-1, per db-speed-first) ────────────
# The runtime half of the db-speed gate (the static N+1 linter runs at deploy).
# Mirrors galactica's per-request cursor counter + inroads' slow-query timing:
#   • counter — reset_db_counter() in a before_request hook, get_db_counter() to
#     read (main.py warns when a request exceeds DB_CALL_WARN_THRESHOLD). Catches
#     runtime N+1 the static linter can't: a cursor opened inside a helper that's
#     called in a loop.
#   • slow log — any cursor held >= SLOW_QUERY_MS logs its caller site, surfacing
#     slow single queries + connections held too long on the shared f1-micro pool.
from time import perf_counter as _perf_counter
_db_tls = threading.local()
DB_CALL_WARN_THRESHOLD = 15
SLOW_QUERY_MS = int(os.environ.get('KUMORI_SLOW_QUERY_MS', '500'))


def reset_db_counter():
    _db_tls.count = 0


def get_db_counter() -> int:
    return getattr(_db_tls, 'count', 0)


def _slow_cursor_site():
    import traceback
    here = os.path.basename(__file__)
    for fr in reversed(traceback.extract_stack()[:-2]):
        base = os.path.basename(fr.filename)
        if base != here and 'contextlib' not in fr.filename:
            return f"{base}:{fr.lineno}"
    return 'unknown'


@contextmanager

def db_cursor(gcp_project_id: str = None, commit: bool = False, dict_rows: bool = False):
    """Per `db-speed-first/SKILL.md` — the canonical cursor pattern for kumori.
    Replaces the verbose `conn=get_db_connection(); try/finally: conn.close()`
    pattern. New code MUST use this. Existing 30+ call sites stay as-is until
    touched naturally.

        with db_cursor(commit=True) as cur:
            cur.execute("UPDATE ...")

        with db_cursor(dict_rows=True) as cur:
            cur.execute("SELECT ...")
            row = cur.fetchone()
    """
    project = gcp_project_id or 'kumori-404602'   # the pool key; every app is on this instance
    _db_tls.count = getattr(_db_tls, 'count', 0) + 1
    _t0 = _perf_counter()
    conn = get_db_connection(project)
    cursor_factory = psycopg2.extras.RealDictCursor if dict_rows else None
    cur = conn.cursor(cursor_factory=cursor_factory) if cursor_factory else conn.cursor()
    try:
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
        _ms = (_perf_counter() - _t0) * 1000
        if _ms >= SLOW_QUERY_MS:
            logger.warning("SLOW DB cursor %.0fms (>=%dms) held by %s", _ms, SLOW_QUERY_MS, _slow_cursor_site())


# ============================================================================
# API USAGE TRACKING - Shared across all kumori apps
# ============================================================================
