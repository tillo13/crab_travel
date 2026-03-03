import os
import logging
import time
from anthropic import Anthropic
from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

_client = None


def _get_client():
    global _client
    if _client:
        return _client
    api_key = get_secret('ANTHROPIC_API_KEY') or get_secret('KUMORI_ANTHROPIC_API_KEY')
    if not api_key:
        api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise ValueError("No ANTHROPIC_API_KEY found")
    _client = Anthropic(api_key=api_key, timeout=60.0, max_retries=2)
    return _client


def generate_text(prompt, system=None, max_tokens=4096, temperature=0.7):
    client = _get_client()
    start = time.time()
    params = {
        'model': MODEL,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    if system:
        params['system'] = system
    response = client.messages.create(**params)
    text = response.content[0].text
    elapsed = time.time() - start
    tokens_in = response.usage.input_tokens
    tokens_out = response.usage.output_tokens
    logger.info(f"🤖 Claude: {tokens_in}→{tokens_out} tokens, {elapsed:.1f}s")
    return text, tokens_in, tokens_out
