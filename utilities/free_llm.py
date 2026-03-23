"""
Free LLM router — tries every free-tier backend before paid fallbacks.
All secrets are in the shared kumori-404602 GCP Secret Manager.

Order: Groq → Cerebras → Mistral → Together → Gemini → Grok → OpenRouter (x3) →
       DeepSeek → GPT-4o-mini → Haiku (absolute last resort)

12 backends deep. Haiku should basically never get hit.
"""

import logging
import time
import requests

logger = logging.getLogger(__name__)


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
    # ── Completely free tiers ──
    {
        'name': 'groq',
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'model': 'llama-3.3-70b-versatile',
        'secret': 'KINDNESS_GROQ_API_KEY',
    },
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
    # Gemini — uses its own SDK, 1500 req/day free
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
    # ── Cheap paid (pennies) ──
    {
        'name': 'deepseek',
        'url': 'https://api.deepseek.com/v1/chat/completions',
        'model': 'deepseek-chat',
        'secret': 'KINDNESS_DEEPSEEK_API_KEY',
    },
    {
        'name': 'gpt4o-mini',
        'url': 'https://api.openai.com/v1/chat/completions',
        'model': 'gpt-4o-mini',
        'secret': 'KINDNESS_OPENAI_API_KEY',
    },
    # Haiku is absolute last resort — handled by _try_haiku()
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
    key = _get_key(backend['secret'])
    if not key:
        return None

    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
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


def _try_haiku(prompt, max_tokens=500, temperature=1.0):
    """Absolute last resort — Anthropic Haiku (paid)."""
    from utilities.claude_utils import _get_api_key, API_URL

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
    return resp.json()['content'][0]['text']


def generate(prompt, max_tokens=500, temperature=1.0, caller='crawl'):
    """
    Try ALL free backends, then cheap paid, then Haiku last resort.
    12 backends deep. Every attempt logged to crab.llm_calls.
    Returns: (text, backend_name)
    """
    prompt_len = len(prompt)

    for backend in BACKENDS:
        start = time.time()
        try:
            if backend.get('type') == 'gemini':
                text = _try_gemini(prompt, max_tokens, temperature)
            elif backend.get('type') == 'grok':
                text = _try_grok(prompt, max_tokens, temperature)
            else:
                text = _try_openai_compatible(backend, prompt, max_tokens, temperature)

            ms = int((time.time() - start) * 1000)
            if text:
                _log(backend['name'], backend.get('model', ''), prompt_len, len(text), ms, True, caller=caller)
                logger.info(f"🆓 {backend['name']} responded ({len(text)} chars, {ms}ms)")
                return text, backend['name']
        except Exception as e:
            ms = int((time.time() - start) * 1000)
            _log(backend['name'], backend.get('model', ''), prompt_len, 0, ms, False, str(e)[:200], caller=caller)
            logger.warning(f"LLM {backend['name']} failed ({ms}ms): {e}")
            continue

    # Absolute last resort — paid Haiku
    start = time.time()
    try:
        text = _try_haiku(prompt, max_tokens, temperature)
        ms = int((time.time() - start) * 1000)
        _log('haiku', 'claude-haiku-4-5-20251001', prompt_len, len(text), ms, True, caller=caller)
        logger.info(f"💰 Haiku last-resort fallback ({len(text)} chars, {ms}ms)")
        return text, 'haiku'
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        _log('haiku', 'claude-haiku-4-5-20251001', prompt_len, 0, ms, False, str(e)[:200], caller=caller)
        logger.error(f"ALL {len(BACKENDS) + 1} backends failed: {e}")
        return None, None
