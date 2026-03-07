import os
import logging
import time
import requests
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
API_URL = "https://api.anthropic.com/v1/messages"

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
    return text, tokens_in, tokens_out
