"""
litellm_plus_router.py — Combined super router for all kumori apps.

All of scatterbrain's routing powers (round-robin, RPM throttle, cross-app
daily caps, grok/deepseek via kindness-worker, paid fallbacks) PLUS the
LiteLLM gateway as an additional fallback tier with spend tracking.

Lives in _infrastructure/litellm/. Any kumori app can import and use it.
Self-contained — NO imports from utilities.* — all deps injected via init().

Usage:
    from litellm_plus_router import init, generate

    init(
        app_name='scatterbrain',
        get_secret_fn=get_secret,           # (secret_name) -> str
        db_cursor_fn=db_cursor,             # context manager yielding cursor (optional)
        anthropic_key_fn=get_anthropic_key,  # () -> str (optional, for haiku)
        log_api_usage_fn=log_api_usage,     # (model, usage, feature) (optional)
    )

    text, backend = generate("hello world")
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error
from datetime import date

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Injected dependencies — set by init()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_app_name = None
_get_secret_fn = None       # (secret_name) -> str
_db_cursor_fn = None        # context manager yielding DB cursor
_anthropic_key_fn = None    # () -> str
_log_api_usage_fn = None    # (model, usage_dict, feature=) -> None
_litellm_url = None         # LiteLLM gateway URL
_litellm_key = None         # LiteLLM virtual key for this app
_initialized = False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backend definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BACKENDS = [
    # Tier 1: Groq (fast, generous free tier — all share one API key / 30 RPM pool)
    # gateway_model: if direct call fails (Cloudflare, etc.), retry through LiteLLM gateway
    {'name': 'groq', 'url': 'https://api.groq.com/openai/v1/chat/completions', 'model': 'llama-3.3-70b-versatile', 'secret': 'KINDNESS_GROQ_API_KEY', 'gateway_model': 'groq-llama-70b'},
    {'name': 'groq-kimi', 'url': 'https://api.groq.com/openai/v1/chat/completions', 'model': 'moonshotai/kimi-k2-instruct', 'secret': 'KINDNESS_GROQ_API_KEY', 'gateway_model': 'groq-kimi'},
    {'name': 'groq-qwen', 'url': 'https://api.groq.com/openai/v1/chat/completions', 'model': 'qwen/qwen3-32b', 'secret': 'KINDNESS_GROQ_API_KEY', 'gateway_model': 'groq-qwen'},
    {'name': 'groq-gptoss', 'url': 'https://api.groq.com/openai/v1/chat/completions', 'model': 'openai/gpt-oss-120b', 'secret': 'KINDNESS_GROQ_API_KEY', 'gateway_model': 'groq-gptoss'},
    # Tier 1: Cerebras
    {'name': 'cerebras', 'url': 'https://api.cerebras.ai/v1/chat/completions', 'model': 'llama3.1-8b', 'secret': 'KINDNESS_CEREBRAS_API_KEY', 'gateway_model': 'cerebras-llama'},
    # Tier 2: Gemini (multiple models for separate quota pools) + llm7
    {'name': 'gemini', 'type': 'gemini', 'secret': 'KINDNESS_GEMINI_API_KEY', 'gemini_model': 'gemini-2.5-flash', 'gateway_model': 'gemini-flash'},
    {'name': 'gemini-lite', 'type': 'gemini', 'secret': 'KINDNESS_GEMINI_API_KEY', 'gemini_model': 'gemini-2.5-flash-lite'},
    {'name': 'gemma', 'type': 'gemini', 'secret': 'KINDNESS_GEMINI_API_KEY', 'gemini_model': 'gemma-3-4b-it'},
    {'name': 'llm7', 'url': 'https://api.llm7.io/v1/chat/completions', 'model': 'deepseek-r1', 'secret': None, 'gateway_model': 'llm7-deepseek'},
    # Tier 3: OpenRouter free slots (share one key / ~50/day pool)
    {'name': 'openrouter-gemma', 'url': 'https://openrouter.ai/api/v1/chat/completions', 'model': 'google/gemma-3-4b-it:free', 'secret': 'KINDNESS_OPENROUTER_API_KEY', 'gateway_model': 'openrouter-gemma'},
    {'name': 'openrouter-llama', 'url': 'https://openrouter.ai/api/v1/chat/completions', 'model': 'meta-llama/llama-3.2-3b-instruct:free', 'secret': 'KINDNESS_OPENROUTER_API_KEY', 'gateway_model': 'openrouter-llama'},
    {'name': 'openrouter-gemma-nano', 'url': 'https://openrouter.ai/api/v1/chat/completions', 'model': 'google/gemma-3n-e2b-it:free', 'secret': 'KINDNESS_OPENROUTER_API_KEY', 'gateway_model': 'openrouter-gemma-nano'},
    # Tier 3: NVIDIA NIM (5K LIFETIME credits — precious)
    {'name': 'nvidia', 'url': 'https://integrate.api.nvidia.com/v1/chat/completions', 'model': 'meta/llama-3.3-70b-instruct', 'secret': 'KINDNESS_NVIDIA_API_KEY', 'gateway_model': 'nvidia-llama'},
    # Tier 3: Grok + DeepSeek via kindness-worker (free, zero-auth, slow)
    {'name': 'grok', 'type': 'grok'},              # grok-3-auto (default)
    {'name': 'grok_fast', 'type': 'grok_fast'},     # grok-3-fast — faster, less capable
    {'name': 'grok4', 'type': 'grok4'},             # grok-4 — most capable
    {'name': 'deepseek', 'type': 'deepseek'},
    # Tier 3: Mistral (2 RPM — slow but free)
    {'name': 'mistral', 'url': 'https://api.mistral.ai/v1/chat/completions', 'model': 'mistral-small-latest', 'secret': 'KINDNESS_MISTRAL_API_KEY', 'gateway_model': 'mistral-small'},
]

# LiteLLM gateway backends — tried after all direct+gateway-fallback attempts fail.
# One call triggers the gateway's own fallback chain (config.yaml).
LITELLM_BACKENDS = [
    {'name': 'litellm-gateway', 'type': 'litellm', 'litellm_model': 'groq-llama-70b'},
]

PAID_BACKENDS = [
    {'name': 'gpt4o-mini', 'url': 'https://api.openai.com/v1/chat/completions', 'model': 'gpt-4o-mini', 'secret': 'KINDNESS_OPENAI_API_KEY'},
    {'name': 'gpt4o', 'url': 'https://api.openai.com/v1/chat/completions', 'model': 'gpt-4o', 'secret': 'KINDNESS_OPENAI_API_KEY'},
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Provider limits — loaded from kumori_llm_provider_limits table at init.
# Fallback defaults used if DB is unavailable.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_provider_limits = {}   # backend -> {daily_limit, rpm_spacing_sec, lifetime_limit, lifetime_used, backoff_sec, enabled, conservation}
_daily_counts = {}
_count_date = None
_last_db_sync = 0
_caps_db_write_fn = None
_caps_db_read_fn = None
_last_call_time = {}    # backend_name -> timestamp
_call_counter = 0       # round-robin rotation
_backoff_until = {}     # backend_name -> timestamp when it becomes available again
DB_SYNC_INTERVAL = 300

# Hardcoded fallbacks — used ONLY when DB is unavailable
_FALLBACK_LIMITS = {
    'groq': {'daily_limit': 1000, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120, 'enabled': True},
    'groq-kimi': {'daily_limit': 1000, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120, 'enabled': True},
    'groq-qwen': {'daily_limit': 1000, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120, 'enabled': True},
    'groq-gptoss': {'daily_limit': 200, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120, 'enabled': True},
    'cerebras': {'daily_limit': 10, 'rpm_spacing_sec': 3.0, 'backoff_sec': 120, 'enabled': True, 'conservation': True},
    'gemini': {'daily_limit': 20, 'rpm_spacing_sec': 10.0, 'backoff_sec': 300, 'enabled': True},
    'gemini-lite': {'daily_limit': 500, 'rpm_spacing_sec': 5.0, 'backoff_sec': 300, 'enabled': True},
    'gemma': {'daily_limit': 500, 'rpm_spacing_sec': 5.0, 'backoff_sec': 300, 'enabled': True},
    'llm7': {'daily_limit': 300, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120, 'enabled': True},
    'openrouter-gemma': {'daily_limit': 50, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'openrouter-llama': {'daily_limit': 50, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'openrouter-gemma-nano': {'daily_limit': 50, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'nvidia': {'daily_limit': 50, 'rpm_spacing_sec': 5.0, 'backoff_sec': 120, 'enabled': True, 'lifetime_limit': 5000, 'conservation': True},
    'grok': {'daily_limit': 100, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'grok_fast': {'daily_limit': 100, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'grok4': {'daily_limit': 100, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'deepseek': {'daily_limit': 100, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120, 'enabled': True},
    'mistral': {'daily_limit': 100, 'rpm_spacing_sec': 60.0, 'backoff_sec': 120, 'enabled': True},
    'litellm-gateway': {'daily_limit': 500, 'rpm_spacing_sec': 2.0, 'backoff_sec': 120, 'enabled': True},
    'gpt4o-mini': {'daily_limit': 50, 'rpm_spacing_sec': 2.0, 'backoff_sec': 120, 'enabled': True},
    'gpt4o': {'daily_limit': 20, 'rpm_spacing_sec': 2.0, 'backoff_sec': 120, 'enabled': True},
    'haiku': {'daily_limit': 10, 'rpm_spacing_sec': 2.0, 'backoff_sec': 120, 'enabled': True},
}


def _get_limit(backend_name, field, default=None):
    """Get a limit value for a backend, from DB cache or fallback."""
    limits = _provider_limits.get(backend_name) or _FALLBACK_LIMITS.get(backend_name) or {}
    return limits.get(field, default)


def _load_provider_limits():
    """Load all provider limits from kumori_llm_provider_limits table."""
    global _provider_limits
    if not _db_cursor_fn:
        return
    try:
        with _db_cursor_fn(dict_cursor=False) as cur:
            cur.execute("""
                SELECT backend, daily_limit, rpm_spacing_sec, lifetime_limit, lifetime_used,
                       backoff_sec, enabled, conservation
                FROM kumori_llm_provider_limits
            """)
            for row in cur.fetchall():
                _provider_limits[row[0]] = {
                    'daily_limit': row[1],
                    'rpm_spacing_sec': float(row[2]) if row[2] else 1.0,
                    'lifetime_limit': row[3],
                    'lifetime_used': row[4] or 0,
                    'backoff_sec': row[5] or 120,
                    'enabled': row[6] if row[6] is not None else True,
                    'conservation': row[7] if row[7] is not None else False,
                }
        logger.info(f"Loaded {len(_provider_limits)} provider limits from DB")
    except Exception as e:
        logger.warning(f"Could not load provider limits from DB (using fallbacks): {e}")


def _rpm_ok(backend_name):
    spacing = _get_limit(backend_name, 'rpm_spacing_sec', 1.0)
    last = _last_call_time.get(backend_name, 0)
    return (time.time() - last) >= spacing


def _rpm_record(backend_name):
    _last_call_time[backend_name] = time.time()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Daily caps + lifetime tracking
# In-memory counters + periodic DB sync for cross-app coordination.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _reset_if_new_day():
    global _daily_counts, _count_date
    today = date.today()
    if _count_date != today:
        _daily_counts = {}
        _count_date = today


def _sync_from_db():
    global _daily_counts, _last_db_sync, _count_date
    if not _caps_db_read_fn:
        return
    try:
        totals = _caps_db_read_fn()
        if totals:
            _count_date = date.today()
            _daily_counts = dict(totals)
            _last_db_sync = time.time()
            logger.debug(f"Synced LLM caps from DB: {sum(totals.values())} total calls today")
    except Exception as e:
        logger.debug(f"DB sync failed (non-fatal): {e}")
    # Also refresh provider limits on sync
    _load_provider_limits()


def _maybe_sync():
    if time.time() - _last_db_sync > DB_SYNC_INTERVAL:
        _sync_from_db()


def _is_enabled(backend_name):
    """Check if a backend is enabled in provider limits."""
    return _get_limit(backend_name, 'enabled', True)


def _check_daily_cap(backend_name):
    _reset_if_new_day()
    _maybe_sync()
    cap = _get_limit(backend_name, 'daily_limit', 50)
    if cap is None:
        return True  # No daily limit
    used = _daily_counts.get(backend_name, 0)
    return used < cap


def _check_lifetime(backend_name):
    """Check if a backend has lifetime budget remaining."""
    limit = _get_limit(backend_name, 'lifetime_limit')
    if limit is None:
        return True  # No lifetime limit
    used = _get_limit(backend_name, 'lifetime_used', 0)
    return used < limit


def _record_call(backend_name):
    _reset_if_new_day()
    _daily_counts[backend_name] = _daily_counts.get(backend_name, 0) + 1
    # Async DB writes — daily caps + lifetime
    if _caps_db_write_fn and _app_name:
        threading.Thread(target=_safe_caps_write, args=(backend_name,), daemon=True).start()
    if _get_limit(backend_name, 'lifetime_limit') is not None and _db_cursor_fn:
        threading.Thread(target=_safe_lifetime_increment, args=(backend_name,), daemon=True).start()


def _safe_caps_write(backend_name):
    try:
        _caps_db_write_fn(backend_name, _app_name)
    except Exception:
        pass


def _safe_lifetime_increment(backend_name):
    """Increment lifetime_used in kumori_llm_provider_limits. Fire-and-forget."""
    try:
        with _db_cursor_fn(dict_cursor=False, commit=True) as cur:
            cur.execute("""
                UPDATE kumori_llm_provider_limits
                SET lifetime_used = lifetime_used + 1, updated_at = NOW()
                WHERE backend = %s
            """, (backend_name,))
        # Update local cache too
        if backend_name in _provider_limits:
            _provider_limits[backend_name]['lifetime_used'] = _provider_limits[backend_name].get('lifetime_used', 0) + 1
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sticky backoff — mark backends unavailable after failures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mark_backoff(backend_name):
    duration = _get_limit(backend_name, 'backoff_sec', 120)
    _backoff_until[backend_name] = time.time() + duration
    logger.info(f"Backing off {backend_name} for {duration}s")


def _is_backed_off(backend_name):
    until = _backoff_until.get(backend_name, 0)
    if time.time() >= until:
        return False
    remaining = int(until - time.time())
    logger.debug(f"{backend_name} backed off for {remaining}s more")
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Secret cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_key_cache = {}


def _get_key(secret_name):
    if not secret_name:
        return None
    if secret_name not in _key_cache:
        if not _get_secret_fn:
            logger.warning(f"No get_secret_fn — cannot fetch {secret_name}")
            return None
        try:
            _key_cache[secret_name] = _get_secret_fn(secret_name)
        except Exception:
            _key_cache[secret_name] = None
    return _key_cache[secret_name]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backend implementations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _try_openai_compatible(backend, prompt, max_tokens, temperature):
    """Standard OpenAI-compatible API call (Groq, Cerebras, NVIDIA, Mistral, etc.)."""
    headers = {'Content-Type': 'application/json'}
    if backend.get('secret'):
        key = _get_key(backend['secret'])
        if not key:
            return None
        headers['Authorization'] = f'Bearer {key}'
    if 'openrouter' in backend['name']:
        headers['HTTP-Referer'] = f'https://{_app_name or "kumori"}.app'

    payload = json.dumps({
        'model': backend['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'temperature': temperature,
    }).encode()

    timeout = 60 if backend['name'] == 'nvidia' else 30
    req = urllib.request.Request(backend['url'], data=payload, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
        msg = data['choices'][0]['message']
        content = msg.get('content') or msg.get('reasoning') or ''
        return content.strip() if content.strip() else None


def _try_gemini(prompt, max_tokens, temperature, model_name='gemini-2.5-flash'):
    """Google Gemini via the google-generativeai SDK."""
    key = _get_key('KINDNESS_GEMINI_API_KEY')
    if not key:
        return None
    import google.generativeai as genai
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        model_name,
        generation_config=genai.types.GenerationConfig(max_output_tokens=max_tokens, temperature=temperature),
    )
    try:
        response = model.generate_content(prompt)
    except Exception as e:
        if '429' in str(e):
            raise  # Let caller handle 429 for backoff
        return None
    if not response.candidates or not response.candidates[0].content.parts:
        return None  # Safety filter or empty response
    return response.text.strip()


def _try_grok(prompt, max_tokens, temperature):
    """Grok via kindness-worker (ECDSA zero-auth, free)."""
    url = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    payload = json.dumps({
        'backend': 'grok',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens, 'temperature': temperature,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data.get('text', '')
        return text if text else None


def _try_grok_fast(prompt, max_tokens, temperature):
    """Grok-3-fast via kindness-worker (ECDSA zero-auth, free, faster variant)."""
    url = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    payload = json.dumps({
        'backend': 'grok_fast',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens, 'temperature': temperature,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data.get('text', '')
        return text if text else None


def _try_grok4(prompt, max_tokens, temperature):
    """Grok-4 via kindness-worker (ECDSA zero-auth, free, most capable)."""
    url = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    payload = json.dumps({
        'backend': 'grok4',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens, 'temperature': temperature,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data.get('text', '')
        return text if text else None


def _try_deepseek(prompt, max_tokens, temperature):
    """DeepSeek via kindness-worker (PoW bypass, free)."""
    url = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    payload = json.dumps({
        'backend': 'deepseek',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens, 'temperature': temperature,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data.get('text', '')
        return text if text else None


def _try_litellm_gateway(backend, prompt, max_tokens, temperature):
    """Route through the LiteLLM Cloud Run gateway. Gateway has its own fallback chain."""
    if not _litellm_url or not _litellm_key:
        logger.debug("LiteLLM gateway not configured — skipping")
        return None

    payload = json.dumps({
        'model': backend.get('litellm_model', 'groq-llama-70b'),
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'temperature': temperature,
    }).encode()

    req = urllib.request.Request(
        f'{_litellm_url}/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {_litellm_key}'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
        msg = data['choices'][0]['message']
        # Some reasoning models (gptoss, qwen3) put output in 'reasoning' not 'content'
        content = msg.get('content') or msg.get('reasoning') or ''
        return content.strip() if content.strip() else None


def _try_haiku(prompt, max_tokens, temperature):
    """Anthropic Haiku — absolute last resort (paid)."""
    if _anthropic_key_fn:
        api_key = _anthropic_key_fn()
    else:
        api_key = _get_key('KUMORI_ANTHROPIC_API_KEY')
    if not api_key:
        raise ValueError("No Anthropic API key available")

    payload = json.dumps({
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        if _log_api_usage_fn:
            _log_api_usage_fn('claude-haiku-4-5-20251001', data.get('usage', {}), feature='haiku_last_resort')
        return data['content'][0]['text']


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _try_backend(backend, prompt, max_tokens, temperature, caller):
    """Try a single backend. Returns text on success, None on failure.

    If the direct call fails and the backend has a gateway_model configured,
    automatically retries through the LiteLLM Cloud Run gateway (bypasses
    Cloudflare blocks, gives spend tracking).
    """
    name = backend['name']

    if not _is_enabled(name):
        return None
    if _is_backed_off(name):
        return None
    if not _rpm_ok(name):
        return None
    if not _check_daily_cap(name):
        return None
    if not _check_lifetime(name):
        return None

    start = time.time()
    direct_failed = False
    try:
        btype = backend.get('type')
        if btype == 'gemini':
            text = _try_gemini(prompt, max_tokens, temperature, backend.get('gemini_model', 'gemini-2.5-flash'))
        elif btype == 'grok':
            text = _try_grok(prompt, max_tokens, temperature)
        elif btype == 'grok_fast':
            text = _try_grok_fast(prompt, max_tokens, temperature)
        elif btype == 'grok4':
            text = _try_grok4(prompt, max_tokens, temperature)
        elif btype == 'deepseek':
            text = _try_deepseek(prompt, max_tokens, temperature)
        elif btype == 'litellm':
            text = _try_litellm_gateway(backend, prompt, max_tokens, temperature)
        else:
            text = _try_openai_compatible(backend, prompt, max_tokens, temperature)

        ms = int((time.time() - start) * 1000)
        _rpm_record(name)

        if text:
            _record_call(name)
            logger.info(f"{'🌐' if btype == 'litellm' else '🆓'} {name} responded ({len(text)} chars, {ms}ms) caller={caller}")
            return text
        direct_failed = True

    except urllib.error.HTTPError as e:
        ms = int((time.time() - start) * 1000)
        _rpm_record(name)
        code = e.code
        if code == 429:
            _mark_backoff(name)
            # 429 means the upstream quota is exhausted — gateway uses the same key,
            # so don't bother retrying through gateway for rate limits.
            logger.warning(f"LLM {name} HTTP {code} ({ms}ms)")
            return None
        logger.warning(f"LLM {name} HTTP {code} ({ms}ms)")
        direct_failed = True

    except Exception as e:
        ms = int((time.time() - start) * 1000)
        _rpm_record(name)
        # Detect 429 in non-HTTP exceptions (e.g. Gemini SDK)
        if '429' in str(e):
            _mark_backoff(name)
            logger.warning(f"LLM {name} rate limited ({ms}ms)")
            return None  # Skip gateway fallback — same API key, same quota
        logger.warning(f"LLM {name} failed ({ms}ms): {e}")
        direct_failed = True

    # Gateway fallback: if direct call failed (not 429) and a gateway model exists,
    # retry through the LiteLLM Cloud Run gateway. This bypasses Cloudflare blocks
    # and other local network issues since the gateway runs in GCP.
    if direct_failed and backend.get('gateway_model') and _litellm_url and _litellm_key:
        gw_model = backend['gateway_model']
        logger.info(f"Retrying {name} via gateway ({gw_model})...")
        gw_start = time.time()
        try:
            text = _try_litellm_gateway({'litellm_model': gw_model}, prompt, max_tokens, temperature)
            gw_ms = int((time.time() - gw_start) * 1000)
            if text:
                _record_call(name)
                logger.info(f"🌐 {name} responded VIA GATEWAY ({len(text)} chars, {gw_ms}ms) caller={caller}")
                return text
        except urllib.error.HTTPError as e:
            gw_ms = int((time.time() - gw_start) * 1000)
            code = e.code
            if code == 429:
                _mark_backoff(name)
            logger.warning(f"LLM {name} gateway fallback HTTP {code} ({gw_ms}ms)")
        except Exception as e:
            gw_ms = int((time.time() - gw_start) * 1000)
            logger.warning(f"LLM {name} gateway fallback failed ({gw_ms}ms): {e}")

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def init(app_name, get_secret_fn=None, db_cursor_fn=None,
         anthropic_key_fn=None, log_api_usage_fn=None,
         litellm_url=None, litellm_key=None,
         policy='always_answer'):
    """
    Initialize the router. Call once at app startup.

    Args:
        app_name: 'scatterbrain', 'kindness_social', 'crab_travel', etc.
        get_secret_fn: (secret_name) -> str. Fetches API keys from Secret Manager.
        db_cursor_fn: Optional context manager yielding a DB cursor. Enables cross-app
                      daily cap coordination via kumori_llm_daily_caps table.
        anthropic_key_fn: Optional () -> str. For Haiku last resort.
        log_api_usage_fn: Optional (model, usage_dict, feature=) -> None. For paid usage logging.
        litellm_url: LiteLLM gateway URL. Auto-fetched from LITELLM_GATEWAY_URL secret if omitted.
        litellm_key: LiteLLM virtual key. Auto-fetched from SCATTERBRAIN_LITELLM_KEY secret if omitted.
        policy: 'always_answer' (fall through to paid) or 'silent' (return None if free fails).
    """
    global _app_name, _get_secret_fn, _db_cursor_fn, _anthropic_key_fn
    global _log_api_usage_fn, _litellm_url, _litellm_key
    global _caps_db_write_fn, _caps_db_read_fn, _initialized, _policy

    _app_name = app_name
    _get_secret_fn = get_secret_fn
    _db_cursor_fn = db_cursor_fn
    _anthropic_key_fn = anthropic_key_fn
    _log_api_usage_fn = log_api_usage_fn
    _policy = policy

    # Load provider limits from DB (caps, RPM spacing, lifetime, backoff, enabled)
    if db_cursor_fn:
        _load_provider_limits()

    # Wire up cross-app daily caps if DB cursor is available
    if db_cursor_fn:
        def _db_write(backend, app):
            with db_cursor_fn(dict_cursor=False, commit=True) as cur:
                cur.execute("""
                    INSERT INTO kumori_llm_daily_caps (usage_date, backend, app_name, call_count)
                    VALUES (CURRENT_DATE, %s, %s, 1)
                    ON CONFLICT (usage_date, backend, app_name)
                    DO UPDATE SET call_count = kumori_llm_daily_caps.call_count + 1
                """, (backend, app))

        def _db_read():
            with db_cursor_fn(dict_cursor=False) as cur:
                cur.execute("""
                    SELECT backend, SUM(call_count) as total
                    FROM kumori_llm_daily_caps
                    WHERE usage_date = CURRENT_DATE
                    GROUP BY backend
                """)
                return {row[0]: row[1] for row in cur.fetchall()}

        _caps_db_write_fn = _db_write
        _caps_db_read_fn = _db_read
        _sync_from_db()
        logger.info(f"litellm_plus_router: caps DB sync enabled for {app_name}")

    # Resolve LiteLLM gateway URL + key
    _litellm_url = litellm_url
    _litellm_key = litellm_key
    if get_secret_fn:
        if not _litellm_url:
            try:
                _litellm_url = get_secret_fn('LITELLM_GATEWAY_URL')
            except Exception:
                logger.info("LITELLM_GATEWAY_URL not found — gateway backend disabled")
        if not _litellm_key:
            # Master key first (virtual keys need gateway DB to be healthy)
            for key_name in ['LITELLM_MASTER_KEY', 'SCATTERBRAIN_LITELLM_KEY']:
                try:
                    _litellm_key = get_secret_fn(key_name)
                    if _litellm_key:
                        logger.info(f"litellm_plus_router: using {key_name} for gateway auth")
                        break
                except Exception:
                    continue
            if not _litellm_key:
                logger.info("No LiteLLM key found — gateway backend disabled")

    if _litellm_url:
        _litellm_url = _litellm_url.rstrip('/')
        logger.info(f"litellm_plus_router: gateway at {_litellm_url}")
    else:
        logger.info("litellm_plus_router: no gateway URL — direct backends only")

    _initialized = True
    logger.info(f"litellm_plus_router initialized: app={app_name}, policy={policy}, "
                f"gateway={'yes' if _litellm_url else 'no'}, db_caps={'yes' if db_cursor_fn else 'no'}")


def generate(prompt, max_tokens=500, temperature=1.0, caller=None):
    """
    Route a prompt through all available backends.

    Returns: (text, backend_name) or (None, None) on total failure.

    Order:
      1. Direct free backends (round-robin rotation)
      2. LiteLLM gateway (independent retry state, spend tracking)
      3. Paid backends (gpt4o-mini) — only if policy='always_answer'
      4. Haiku last resort — only if policy='always_answer'
    """
    global _call_counter

    if not _initialized:
        logger.error("litellm_plus_router.generate() called before init()")
        return None, None

    caller = caller or _app_name or 'unknown'
    policy = getattr(generate, '_policy_override', None) or _policy

    # ── Tier: Direct free backends (round-robin) ──
    n = len(BACKENDS)
    start_idx = _call_counter % n
    _call_counter += 1

    for i in range(n):
        backend = BACKENDS[(start_idx + i) % n]
        text = _try_backend(backend, prompt, max_tokens, temperature, caller)
        if text:
            return text, backend['name']

    # ── Tier: LiteLLM gateway ──
    for backend in LITELLM_BACKENDS:
        text = _try_backend(backend, prompt, max_tokens, temperature, caller)
        if text:
            return text, backend['name']

    # ── Silent policy stops here ──
    if policy == 'silent':
        logger.warning(f"All free backends failed, policy=silent — returning None (caller={caller})")
        return None, None

    # ── Tier: Paid backends ──
    for backend in PAID_BACKENDS:
        text = _try_backend(backend, prompt, max_tokens, temperature, caller)
        if text:
            return text, backend['name']

    # ── Tier: Haiku last resort ──
    if not _check_daily_cap('haiku'):
        logger.warning("All backends at daily cap — cannot serve request")
        return None, None

    start = time.time()
    try:
        text = _try_haiku(prompt, max_tokens, temperature)
        ms = int((time.time() - start) * 1000)
        _record_call('haiku')
        logger.info(f"💰 Haiku LAST RESORT ({len(text)} chars, {ms}ms) caller={caller}")
        return text, 'haiku'
    except Exception as e:
        total = len(BACKENDS) + len(LITELLM_BACKENDS) + len(PAID_BACKENDS) + 1
        logger.error(f"ALL {total} backends failed: {e}")
        return None, None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# chat() — message-based interface (used by kindness_social agents)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Map backend names to the corresponding entry in BACKENDS / PAID_BACKENDS
_BACKEND_BY_NAME = {}


def _ensure_backend_index():
    if not _BACKEND_BY_NAME:
        for b in BACKENDS + LITELLM_BACKENDS + PAID_BACKENDS:
            _BACKEND_BY_NAME[b['name']] = b
        # Also add haiku as a special entry
        _BACKEND_BY_NAME['haiku'] = {'name': 'haiku', 'type': 'haiku'}


def _messages_to_prompt(messages, system=None):
    """Convert a list of chat messages to a single prompt string."""
    parts = []
    if system:
        parts.append(system)
    for msg in messages:
        content = msg.get('content', '')
        if msg.get('role') == 'system':
            parts.append(content)
        elif msg.get('role') == 'assistant':
            parts.append(f"Assistant: {content}")
        else:
            parts.append(content)
    return '\n\n'.join(parts)


def chat(backend_name, messages, max_tokens=500, temperature=0.3, system=None, caller=None):
    """
    Route a chat request to a SPECIFIC backend. No round-robin, no fallback.
    If the backend is down, rate-limited, or at cap, returns (None, backend_name).
    This is the interface kindness_social agents use — each agent is assigned one backend.

    Args:
        backend_name: e.g. 'groq', 'grok_fast', 'gemini', 'haiku'
        messages: list of {'role': ..., 'content': ...} dicts
        max_tokens: max output tokens
        temperature: sampling temperature
        system: optional system prompt
        caller: tag for logging

    Returns: (response_text, actual_backend_used)
    """
    if not _initialized:
        logger.error("litellm_plus_router.chat() called before init()")
        return None, backend_name

    _ensure_backend_index()
    caller = caller or _app_name or 'unknown'

    backend = _BACKEND_BY_NAME.get(backend_name)
    if not backend:
        logger.warning(f"Unknown backend: {backend_name}")
        return None, backend_name

    prompt = _messages_to_prompt(messages, system)
    text = _try_backend(backend, prompt, max_tokens, temperature, caller)
    if text:
        return text, backend_name

    return None, backend_name


# Eval pool — every free backend that can reliably return a short answer.
# Order is randomized per call so eval load is spread across providers.
# Paid backends are NEVER used for eval.
EVAL_POOL_FREE = [
    'cerebras', 'mistral', 'groq', 'groq-kimi', 'groq-qwen', 'groq-gptoss',
    'llm7', 'nvidia', 'gemini', 'gemini-lite', 'gemma', 'openrouter-gemma',
    'openrouter-llama', 'openrouter-gemma-nano',
]


def chat_eval(prompt, system="Return ONLY a number 1-10.", caller=None):
    """
    Evaluation call — low tokens, low temperature, randomized free-tier pool.
    Eval is a system function (scoring), not an agent voice, so it uses a
    randomized free pool for consistency. Paid backends are never used.
    Returns: (response_text, backend_name)
    """
    if not _initialized:
        logger.error("litellm_plus_router.chat_eval() called before init()")
        return None, 'eval'

    import random
    _ensure_backend_index()
    caller = caller or _app_name or 'unknown'

    pool = EVAL_POOL_FREE[:]
    random.shuffle(pool)

    eval_prompt = f"{system}\n\n{prompt}" if system else prompt

    for name in pool:
        backend = _BACKEND_BY_NAME.get(name)
        if not backend:
            continue
        text = _try_backend(backend, eval_prompt, max_tokens=10, temperature=0.1, caller=f'eval:{caller}')
        if text and text.strip():
            return text, name

    return None, 'eval'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Utilities
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_policy = 'always_answer'


def get_usage_summary():
    """Current usage vs caps for all backends."""
    _reset_if_new_day()
    # Collect all known backends from both DB limits and backend configs
    all_backends = set()
    for b in BACKENDS + LITELLM_BACKENDS + PAID_BACKENDS:
        all_backends.add(b['name'])
    all_backends.update(_provider_limits.keys())
    all_backends.update(_FALLBACK_LIMITS.keys())

    result = {}
    for backend in sorted(all_backends):
        cap = _get_limit(backend, 'daily_limit', 50) or 0
        used = _daily_counts.get(backend, 0)
        entry = {
            'used': used, 'cap': cap,
            'remaining': max(0, cap - used),
            'pct': round(used / cap * 100, 1) if cap > 0 else 0,
            'enabled': _is_enabled(backend),
        }
        lifetime = _get_limit(backend, 'lifetime_limit')
        if lifetime:
            lifetime_used = _get_limit(backend, 'lifetime_used', 0)
            entry['lifetime_limit'] = lifetime
            entry['lifetime_used'] = lifetime_used
            entry['lifetime_remaining'] = max(0, lifetime - lifetime_used)
        result[backend] = entry

    return {
        'date': date.today().isoformat(),
        'app': _app_name,
        'policy': _policy,
        'gateway': bool(_litellm_url),
        'limits_source': 'db' if _provider_limits else 'fallback',
        'backends': result,
        'total_used': sum(_daily_counts.values()),
        'backoffs': {k: int(v - time.time()) for k, v in _backoff_until.items() if v > time.time()},
    }


def list_backends():
    """List all configured backends with their current status."""
    rows = []
    for backend in BACKENDS + LITELLM_BACKENDS + PAID_BACKENDS:
        name = backend['name']
        cap = _get_limit(name, 'daily_limit', 50) or 0
        used = _daily_counts.get(name, 0)
        entry = {
            'name': name,
            'type': backend.get('type', 'openai'),
            'enabled': _is_enabled(name),
            'rpm_ok': _rpm_ok(name),
            'cap_ok': _check_daily_cap(name),
            'lifetime_ok': _check_lifetime(name),
            'backed_off': _is_backed_off(name),
            'cap_remaining': max(0, cap - used),
        }
        lifetime = _get_limit(name, 'lifetime_limit')
        if lifetime:
            entry['lifetime_remaining'] = max(0, lifetime - _get_limit(name, 'lifetime_used', 0))
        rows.append(entry)
    return rows
