"""
Free LLM router — round-robins across free-tier backends, falls back on failure.
All secrets are in the shared kumori-404602 GCP Secret Manager.

Strategy: Round-robin across free backends so every backend gets tested and
we have real latency/reliability data. On failure, try the next one in rotation.
Each backend has a daily cap matching its free tier — once hit, skip till midnight.
Paid backends (GPT-4o-mini, Haiku) only used if ALL free ones fail.
"""

import logging
import time
import requests
import random

logger = logging.getLogger(__name__)

# Round-robin counter — rotates which free backend goes first
_call_counter = 0

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


def _log(backend, model, prompt_len, response_len, duration_ms, success, error=None, caller=None):
    """Log to DB — fire and forget, never break the actual call."""
    try:
        from utilities.postgres_utils import log_llm_call
        log_llm_call(backend, model, prompt_len, response_len, duration_ms, success, error, caller)
    except Exception:
        pass

# All OpenAI-compatible backends, cheapest/free first.
# Gemini and Haiku handled separately (different API formats).
BACKENDS = [
    # ── Groq free tier — per-model limits, 1K RPD each ──
    {
        'name': 'groq',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'llama-3.3-70b-versatile',
        'secret': 'KINDNESS_GROQ_API_KEY',
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
        'name': 'groq-gptoss',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'openai/gpt-oss-120b',
        'secret': 'KINDNESS_GROQ_API_KEY',
    },
    # ── Other free tiers ──
    {
        'name': 'cerebras',
        'url': 'https://api.cerebras.ai/v1/chat/completions',
        'model': 'llama3.1-8b',
        'secret': 'KINDNESS_CEREBRAS_API_KEY',
    },
    {
        'name': 'mistral',
        'url': 'https://api.mistral.ai/v1/chat/completions',
        'model': 'mistral-small-latest',
        'secret': 'KINDNESS_MISTRAL_API_KEY',
    },
    {
        'name': 'together',
        'url': 'https://api.together.ai/v1/chat/completions',
        'model': 'meta-llama/Llama-3.3-70B-Instruct-Turbo-Free',
        'secret': 'KINDNESS_TOGETHER_API_KEY',
    },
    # LLM7.io — no API key needed, 30 RPM, OpenAI-compatible
    {
        'name': 'llm7',
        'url': 'https://api.llm7.io/v1/chat/completions',
        'model': 'deepseek-r1',
        'secret': None,
    },
    # NVIDIA NIM — 5K lifetime credits, 40 RPM. Conserve, use as mid-priority.
    {
        'name': 'nvidia',
        'url': 'https://integrate.api.nvidia.com/v1/chat/completions',
        'model': 'meta/llama-3.3-70b-instruct',
        'secret': 'KINDNESS_NVIDIA_API_KEY',
    },
    # Gemini — 250 req/day free for Flash, 10 RPM
    {
        'name': 'gemini',
        'type': 'gemini',
        'secret': 'KINDNESS_GEMINI_API_KEY',
    },
    # Grok — free, no API key, proxied through kindness Cloud Run worker
    {
        'name': 'grok',
        'type': 'grok',
    },
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
    # DeepSeek — free via Cloud Run worker PoW bypass (not the paid API)
    {
        'name': 'deepseek',
        'type': 'deepseek',
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

    resp = requests.post(
        backend['url'],
        headers=headers,
        json={
            'model': backend['model'],
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature,
        },
        timeout=30,
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
    """Grok via kindness Cloud Run worker — free, no API key."""
    WORKER_URL = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    resp = requests.post(
        WORKER_URL,
        json={
            'backend': 'grok',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature,
        },
        timeout=60,
    )
    if resp.ok:
        text = resp.json().get('text', '')
        if text:
            return text
    return None


def _try_deepseek(prompt, max_tokens=500, temperature=1.0):
    """DeepSeek via kindness Cloud Run worker — free, uses PoW bypass."""
    WORKER_URL = 'https://kindness-worker-243380010344.us-central1.run.app/chat'
    resp = requests.post(
        WORKER_URL,
        json={
            'backend': 'deepseek',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature,
        },
        timeout=60,
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

    # Skip if daily free cap is reached — no wasted 429s
    if not _check_daily_cap(name):
        logger.debug(f"Skipping {name} — daily cap reached")
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
        if text:
            _record_call(name)
            _log(backend['name'], backend.get('model', ''), prompt_len, len(text), ms, True, caller=caller)
            logger.info(f"🆓 {name} responded ({len(text)} chars, {ms}ms)")
            return text
        return None
    except Exception as e:
        ms = int((time.time() - start) * 1000)
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
