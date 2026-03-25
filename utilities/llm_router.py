"""
Free LLM router — round-robins across free-tier backends, falls back on failure.
All secrets are in the shared kumori-404602 GCP Secret Manager.

Strategy: Round-robin across free backends so every backend gets tested and
we have real latency/reliability data. On failure, try the next one in rotation.
Each backend has a daily cap matching its free tier — once hit, skip till midnight.
RPM throttle prevents hammering backends faster than their rate limits allow.
Paid backends (GPT-4o-mini, Haiku) only used if ALL free ones fail.
"""

import logging
import time
import requests
import random

logger = logging.getLogger(__name__)

# Round-robin counter — rotates which free backend goes first
_call_counter = 0

# ── RPM throttle — track last call time per backend to respect rate limits ──
_last_call_time = {}  # backend_name -> timestamp

# Minimum seconds between calls per backend (derived from RPM limits)
# IMPORTANT: All 4 Groq models share ONE API key, so the shared RPM pool is 30.
# Space them out to avoid cross-model 429s when concurrent crawls are running.
RPM_SPACING = {
    'groq': 4.0,              # 30 RPM shared key — leave headroom
    'groq-kimi': 4.0,         # same shared key
    'groq-qwen': 4.0,         # same shared key
    'groq-gptoss': 4.0,       # same shared key
    'cerebras': 3.0,          # 30 RPM = 1 per 2s, pad for concurrency
    'mistral': 60.0,          # 2 RPM — practically useless, keep as deep fallback
    'nvidia': 5.0,            # 40 RPM but LIFETIME credits — conserve aggressively
    'llm7': 4.0,              # undocumented limits, be conservative
    'gemini': 10.0,           # 10 RPM for Flash, but quota errors are sticky
    'openrouter-gemma': 10.0, # 20 RPM but 50/day — space out to not burn cap in 8 min
    'openrouter-llama': 10.0,
    'openrouter-gemma-nano': 10.0,
    'grok': 10.0,             # Cloud Run worker — slow, give it breathing room
    'deepseek': 10.0,         # Cloud Run worker — slow, PoW takes time
}


def _rpm_ok(backend_name):
    """Return True if enough time has passed since last call to this backend."""
    spacing = RPM_SPACING.get(backend_name, 1.0)
    last = _last_call_time.get(backend_name, 0)
    return (time.time() - last) >= spacing


def _rpm_record(backend_name):
    """Record that we just called this backend."""
    _last_call_time[backend_name] = time.time()

# ── Shared cross-app daily caps ──
from utilities.llm_usage_caps import check_cap, record_call as _shared_record_call, init as _init_caps

_caps_initialized = False

def _ensure_caps():
    global _caps_initialized
    if not _caps_initialized:
        _caps_initialized = True
        try:
            from utilities.postgres_utils import get_db_connection
            import psycopg2.extras

            def _db_write(backend, app_name):
                conn = get_db_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO kumori_llm_daily_caps (usage_date, backend, app_name, call_count)
                        VALUES (CURRENT_DATE, %s, %s, 1)
                        ON CONFLICT (usage_date, backend, app_name)
                        DO UPDATE SET call_count = kumori_llm_daily_caps.call_count + 1
                    """, (backend, app_name))
                    conn.commit()
                    cur.close()
                finally:
                    conn.close()

            def _db_read():
                conn = get_db_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT backend, SUM(call_count) as total
                        FROM kumori_llm_daily_caps
                        WHERE usage_date = CURRENT_DATE
                        GROUP BY backend
                    """)
                    result = {row[0]: row[1] for row in cur.fetchall()}
                    cur.close()
                    return result
                finally:
                    conn.close()

            _init_caps('crab_travel', db_write_fn=_db_write, db_read_fn=_db_read)
            logger.info("Shared LLM caps initialized with DB sync")
        except Exception as e:
            logger.warning(f"Shared caps DB init failed (local only): {e}")
            _init_caps('crab_travel')


def _check_daily_cap(backend_name):
    _ensure_caps()
    return check_cap(backend_name)


def _record_call(backend_name):
    _ensure_caps()
    _shared_record_call(backend_name)


# ── Skip tracking — count how often backends are skipped so we can tune ──
_skip_counts = {}  # (backend, reason) -> count
_skip_last_flush = 0
SKIP_FLUSH_INTERVAL = 300  # flush to DB every 5 min


def _log_skip(backend, model, prompt_len, reason, caller):
    """Track skips in memory, flush to DB periodically to avoid log spam."""
    global _skip_last_flush
    key = (backend, reason)
    _skip_counts[key] = _skip_counts.get(key, 0) + 1

    now = time.time()
    if now - _skip_last_flush > SKIP_FLUSH_INTERVAL and _skip_counts:
        _skip_last_flush = now
        # Flush accumulated skips as batch log entries
        for (b, r), count in _skip_counts.items():
            try:
                from utilities.postgres_utils import log_llm_call
                log_llm_call(b, '', 0, 0, 0, False,
                             f'skipped {count}x: {r}', caller,
                             error_type=r, status_code=None)
            except Exception:
                pass
        _skip_counts.clear()


def _classify_error(error_str):
    """Categorize error for dashboarding. Returns (error_type, status_code)."""
    if not error_str:
        return None, None
    e = str(error_str)
    if '429' in e:
        return 'rate_limit', 429
    if '401' in e or 'Unauthorized' in e:
        return 'auth', 401
    if '402' in e or 'Payment Required' in e:
        return 'payment', 402
    if '403' in e or 'Forbidden' in e:
        return 'forbidden', 403
    if '503' in e or 'Service Unavailable' in e:
        return 'unavailable', 503
    if '500' in e and 'Server Error' in e:
        return 'server_error', 500
    if 'timed out' in e or 'timeout' in e.lower():
        return 'timeout', None
    if 'ConnectionError' in e or 'ConnectionPool' in e:
        return 'connection', None
    if 'No module' in e:
        return 'import_error', None
    return 'other', None


def _log(backend, model, prompt_len, response_len, duration_ms, success, error=None, caller=None):
    """Log to DB — fire and forget, never break the actual call."""
    try:
        from utilities.postgres_utils import log_llm_call
        error_type, status_code = _classify_error(error) if error else (None, None)
        log_llm_call(backend, model, prompt_len, response_len, duration_ms, success,
                     error, caller, error_type=error_type, status_code=status_code)
    except Exception:
        pass

# Backend order matters: round-robin starts from a rotating index, so backends
# near each other share load. Group by reliability and capacity.
#
# Tier 1 — High capacity, fast, reliable (handle bulk of crawl traffic)
# Tier 2 — Moderate capacity or slower
# Tier 3 — Low daily caps or slow/unreliable (deep fallbacks)
#
# together removed — $100 signup credits exhausted, 401 Unauthorized
BACKENDS = [
    # ── Tier 1: Groq (4 models × 1K RPD = 4K/day) + Cerebras (9.5K/day) ──
    # These 5 backends carry ~80% of traffic.
    {
        'name': 'cerebras',
        'url': 'https://api.cerebras.ai/v1/chat/completions',
        'model': 'llama3.1-8b',
        'secret': 'KINDNESS_CEREBRAS_API_KEY',
    },
    {
        'name': 'groq-kimi',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'moonshotai/kimi-k2-instruct',
        'secret': 'KINDNESS_GROQ_API_KEY',
    },
    {
        'name': 'groq-qwen',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'qwen/qwen3-32b',
        'secret': 'KINDNESS_GROQ_API_KEY',
    },
    {
        'name': 'groq',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'llama-3.3-70b-versatile',
        'secret': 'KINDNESS_GROQ_API_KEY',
    },
    {
        'name': 'groq-gptoss',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'openai/gpt-oss-120b',
        'secret': 'KINDNESS_GROQ_API_KEY',
    },
    # ── Tier 2: Moderate capacity ──
    # Gemini (250/day), LLM7 (no hard cap but undocumented)
    {
        'name': 'gemini',
        'type': 'gemini',
        'secret': 'KINDNESS_GEMINI_API_KEY',
    },
    {
        'name': 'llm7',
        'url': 'https://api.llm7.io/v1/chat/completions',
        'model': 'deepseek-r1',
        'secret': None,
    },
    # ── Tier 3: Low caps or slow — deep fallbacks only ──
    # OpenRouter (50/day per model), NVIDIA (LIFETIME credits — protect!),
    # Grok/DeepSeek (Cloud Run worker, 60s timeouts common), Mistral (2 RPM)
    {
        'name': 'openrouter-gemma',
        'url': 'https://openrouter.ai/api/v1/chat/completions',
        'model': 'google/gemma-3-4b-it:free',
        'secret': 'KINDNESS_OPENROUTER_API_KEY',
    },
    {
        'name': 'openrouter-llama',
        'url': 'https://openrouter.ai/api/v1/chat/completions',
        'model': 'meta-llama/llama-3.2-3b-instruct:free',
        'secret': 'KINDNESS_OPENROUTER_API_KEY',
    },
    {
        'name': 'openrouter-gemma-nano',
        'url': 'https://openrouter.ai/api/v1/chat/completions',
        'model': 'google/gemma-3n-e2b-it:free',
        'secret': 'KINDNESS_OPENROUTER_API_KEY',
    },
    # NVIDIA NIM — 1K LIFETIME credits (not daily!). Only use as fallback.
    {
        'name': 'nvidia',
        'url': 'https://integrate.api.nvidia.com/v1/chat/completions',
        'model': 'meta/llama-3.3-70b-instruct',
        'secret': 'KINDNESS_NVIDIA_API_KEY',
    },
    # Grok/DeepSeek — Cloud Run worker, free but slow (60s timeouts common)
    {
        'name': 'grok',
        'type': 'grok',
    },
    {
        'name': 'deepseek',
        'type': 'deepseek',
    },
    # Mistral — 2 RPM is practically useless for bulk, keep as absolute last free option
    {
        'name': 'mistral',
        'url': 'https://api.mistral.ai/v1/chat/completions',
        'model': 'mistral-small-latest',
        'secret': 'KINDNESS_MISTRAL_API_KEY',
    },
]

# Paid backends — ONLY if ALL free ones fail. GPT 2nd-to-last, Haiku dead last.
PAID_BACKENDS = [
    {
        'name': 'gpt4o-mini',
        'url': 'https://api.openai.com/v1/chat/completions',
        'model': 'gpt-4o-mini',
        'secret': 'KINDNESS_OPENAI_API_KEY',
    },
    # Haiku handled separately by _try_haiku() — absolute last resort
]

_key_cache = {}


def _get_key(secret_name):
    if secret_name not in _key_cache:
        try:
            from utilities.google_auth_utils import get_secret
            _key_cache[secret_name] = get_secret(secret_name)
        except Exception:
            _key_cache[secret_name] = None
    return _key_cache[secret_name]


def _try_openai_compatible(backend, prompt, max_tokens=500, temperature=1.0):
    """Call an OpenAI-compatible chat API."""
    headers = {'Content-Type': 'application/json'}

    if backend.get('secret'):
        key = _get_key(backend['secret'])
        if not key:
            return None
        headers['Authorization'] = f'Bearer {key}'

    if 'openrouter' in backend['name']:
        headers['HTTP-Referer'] = 'https://crab.travel'

    # NVIDIA NIM needs longer timeout — cold starts can take 45s+
    timeout = 60 if backend['name'] == 'nvidia' else 30

    resp = requests.post(
        backend['url'],
        headers=headers,
        json={
            'model': backend['model'],
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content'].strip()


def _try_gemini(prompt, max_tokens=500, temperature=1.0):
    """Google Gemini via generativeai SDK — 1,500 req/day free."""
    key = _get_key('KINDNESS_GEMINI_API_KEY')
    if not key:
        return None

    import google.generativeai as genai
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        'gemini-2.5-flash',
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    response = model.generate_content(prompt)
    return response.text.strip()


def _try_grok(prompt, max_tokens=500, temperature=1.0):
    """Grok via kindness Cloud Run worker — free, no API key.
    Uses 120s timeout (matching kindness_social) because the ECDSA handshake
    + PoW challenge can take 30-60s before the LLM call even starts.
    """
    WORKER_URL = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    resp = requests.post(
        WORKER_URL,
        json={
            'backend': 'grok',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature,
        },
        timeout=120,
    )
    if resp.ok:
        text = resp.json().get('text', '')
        if text:
            return text
    return None


def _try_deepseek(prompt, max_tokens=500, temperature=1.0):
    """DeepSeek via kindness Cloud Run worker — free, uses PoW bypass.
    Uses 120s timeout (matching kindness_social) because PoW solving
    + session creation + streaming adds up significantly.
    """
    WORKER_URL = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    resp = requests.post(
        WORKER_URL,
        json={
            'backend': 'deepseek',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature,
        },
        timeout=120,
    )
    if resp.ok:
        text = resp.json().get('text', '')
        if text:
            return text
    return None


def _try_haiku(prompt, max_tokens=500, temperature=1.0):
    """Absolute last resort — Anthropic Haiku (paid)."""
    from utilities.claude_utils import _get_api_key, API_URL, log_api_usage

    resp = requests.post(
        API_URL,
        headers={
            'x-api-key': _get_api_key(),
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': max_tokens,
            'temperature': temperature,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    log_api_usage('claude-haiku-4-5-20251001', data.get('usage', {}),
                  feature='haiku_last_resort')
    return data['content'][0]['text']


def _try_backend(backend, prompt, max_tokens, temperature, caller, prompt_len):
    """Try a single backend, log result. Returns text or raises."""
    name = backend['name']

    # Skip if calling too fast — respect RPM limits
    if not _rpm_ok(name):
        _log_skip(name, backend.get('model', ''), prompt_len, 'skip_rpm', caller)
        return None

    # Skip if daily free cap is reached — no wasted 429s
    if not _check_daily_cap(name):
        _log_skip(name, backend.get('model', ''), prompt_len, 'skip_cap', caller)
        return None

    start = time.time()
    try:
        if backend.get('type') == 'gemini':
            text = _try_gemini(prompt, max_tokens, temperature)
        elif backend.get('type') == 'grok':
            text = _try_grok(prompt, max_tokens, temperature)
        elif backend.get('type') == 'deepseek':
            text = _try_deepseek(prompt, max_tokens, temperature)
        else:
            text = _try_openai_compatible(backend, prompt, max_tokens, temperature)

        ms = int((time.time() - start) * 1000)
        _rpm_record(name)  # Record call time even on empty response
        if text:
            _record_call(name)
            _log(backend['name'], backend.get('model', ''), prompt_len, len(text), ms, True, caller=caller)
            logger.info(f"🆓 {name} responded ({len(text)} chars, {ms}ms)")
            return text
        return None
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        _rpm_record(name)  # Record call time on failure too — don't hammer a failing backend
        _log(backend['name'], backend.get('model', ''), prompt_len, 0, ms, False, str(e)[:200], caller=caller)
        logger.warning(f"LLM {backend['name']} failed ({ms}ms): {e}")
        return None


def generate(prompt, max_tokens=500, temperature=1.0, caller='crawl'):
    """
    Round-robin across free backends so every one gets tested, then paid last resort.
    On failure, try next in rotation. GPT-4o-mini 2nd to last, Haiku dead last.
    Every attempt logged to crab.llm_calls.
    Returns: (text, backend_name)
    """
    global _call_counter
    prompt_len = len(prompt)

    # Round-robin: rotate starting position across free backends
    n = len(BACKENDS)
    start_idx = _call_counter % n
    _call_counter += 1

    # Try all free backends starting from rotated position
    for i in range(n):
        backend = BACKENDS[(start_idx + i) % n]
        text = _try_backend(backend, prompt, max_tokens, temperature, caller, prompt_len)
        if text:
            return text, backend['name']

    # Paid fallbacks — only if ALL free ones failed
    for backend in PAID_BACKENDS:
        text = _try_backend(backend, prompt, max_tokens, temperature, caller, prompt_len)
        if text:
            return text, backend['name']

    # Absolute last resort — Anthropic Haiku (still capped)
    if not _check_daily_cap('haiku'):
        logger.warning("All backends at daily cap — cannot serve request")
        return None, None
    start = time.time()
    try:
        text = _try_haiku(prompt, max_tokens, temperature)
        ms = int((time.time() - start) * 1000)
        _record_call('haiku')
        _log('haiku', 'claude-haiku-4-5-20251001', prompt_len, len(text), ms, True, caller=caller)
        logger.info(f"💰 Haiku LAST RESORT ({len(text)} chars, {ms}ms)")
        return text, 'haiku'
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        _log('haiku', 'claude-haiku-4-5-20251001', prompt_len, 0, ms, False, str(e)[:200], caller=caller)
        logger.error(f"ALL {len(BACKENDS) + len(PAID_BACKENDS) + 1} backends failed: {e}")
        return None, None
