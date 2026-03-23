"""
Free LLM router — tries free-tier backends before falling back to Haiku.
Groq (14,400/day) → Cerebras (1M tokens/day) → Mistral (500K tokens/min) → Haiku (paid fallback).
All secrets are in the shared kumori GCP Secret Manager.
"""

import logging
import requests

logger = logging.getLogger(__name__)

# ── Backend configs: (name, url, model, secret_name) ──
BACKENDS = [
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
]

_key_cache = {}


def _get_key(secret_name):
    if secret_name not in _key_cache:
        from utilities.google_auth_utils import get_secret
        _key_cache[secret_name] = get_secret(secret_name)
    return _key_cache[secret_name]


def _try_openai_compatible(backend, prompt, max_tokens=500, temperature=1.0):
    """Call an OpenAI-compatible chat API."""
    key = _get_key(backend['secret'])
    if not key:
        return None

    resp = requests.post(
        backend['url'],
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        },
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


def _try_haiku(prompt, max_tokens=500, temperature=1.0):
    """Paid fallback — Anthropic Haiku."""
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


def generate(prompt, max_tokens=500, temperature=1.0):
    """
    Try free backends first, fall back to Haiku.
    Returns: (text, backend_name)
    """
    for backend in BACKENDS:
        try:
            text = _try_openai_compatible(backend, prompt, max_tokens, temperature)
            if text:
                logger.info(f"🆓 {backend['name']} responded ({len(text)} chars)")
                return text, backend['name']
        except Exception as e:
            logger.warning(f"Free LLM {backend['name']} failed: {e}")
            continue

    # Paid fallback
    try:
        text = _try_haiku(prompt, max_tokens, temperature)
        logger.info(f"💰 Haiku fallback responded ({len(text)} chars)")
        return text, 'haiku'
    except Exception as e:
        logger.error(f"All LLM backends failed including Haiku: {e}")
        return None, None
