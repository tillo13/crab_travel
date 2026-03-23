import os
import logging
import time
import requests
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
API_URL = "https://api.anthropic.com/v1/messages"

APP_NAME = 'crab_travel'

_PRICING = {
    'haiku-4-5': {'input': 0.000001, 'output': 0.000005},
    'sonnet-4': {'input': 0.000003, 'output': 0.000015},
    'sonnet-4-5': {'input': 0.000003, 'output': 0.000015},
    'opus-4-5': {'input': 0.000005, 'output': 0.000025},
    'opus-4-6': {'input': 0.000005, 'output': 0.000025},
}

def _get_pricing(model):
    m = model.lower()
    for k, v in _PRICING.items():
        if k in m:
            return v
    return {'input': 0.000003, 'output': 0.000015}


def log_api_usage(model, usage, feature=None, streaming=False,
                  image_count=0, user_id=None, duration_ms=None):
    """Log an API call to kumori_api_usage. Never raises."""
    try:
        from utilities.postgres_utils import get_db_connection
        pricing = _get_pricing(model)

        input_tokens = usage.get('input_tokens', 0) if isinstance(usage, dict) else 0
        output_tokens = usage.get('output_tokens', 0) if isinstance(usage, dict) else 0
        cache_creation = usage.get('cache_creation_input_tokens', 0) if isinstance(usage, dict) else 0
        cache_read = usage.get('cache_read_input_tokens', 0) if isinstance(usage, dict) else 0

        cost = (
            input_tokens * pricing['input']
            + output_tokens * pricing['output']
            + cache_creation * pricing['input'] * 1.25
            + cache_read * pricing['input'] * 0.1
        )

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO kumori_api_usage
                (app_name, feature, model, input_tokens, output_tokens,
                 cache_creation_tokens, cache_read_tokens, thinking_tokens,
                 web_search_requests, web_fetch_requests, code_execution_requests,
                 image_count, estimated_cost_usd, streaming, user_id, duration_ms)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (APP_NAME, feature, model, input_tokens, output_tokens,
                  cache_creation, cache_read, 0,
                  0, 0, 0,
                  image_count, cost, streaming, user_id, duration_ms))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to log API usage: {e}")

_api_key = None


def _get_api_key():
    global _api_key
    if _api_key:
        return _api_key
    _api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not _api_key:
        try:
            _api_key = get_secret("KUMORI_ANTHROPIC_API_KEY", project_id="kumori-404602")
        except Exception as e:
            logger.error(f"Secret Manager fallback failed: {e}")
    if not _api_key:
        raise ValueError("No ANTHROPIC_API_KEY found in env or Secret Manager")
    return _api_key


def generate_text(prompt, system=None, max_tokens=4096, temperature=0.7):
    api_key = _get_api_key()
    start = time.time()
    body = {
        'model': MODEL,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    if system:
        body['system'] = system
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }
    r = requests.post(API_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    text = data['content'][0]['text']
    elapsed = time.time() - start
    tokens_in = data['usage']['input_tokens']
    tokens_out = data['usage']['output_tokens']
    logger.info(f"Claude: {tokens_in}->{tokens_out} tokens, {elapsed:.1f}s")
    log_api_usage(MODEL, data['usage'], feature='generate_text',
                  duration_ms=int(elapsed * 1000))
    return text, tokens_in, tokens_out
