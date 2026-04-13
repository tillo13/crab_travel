"""
LLM Router — thin wrapper around kumori_free_llms.
Delegates all routing to the shared infrastructure router while preserving
the same public API: generate(prompt, ...) -> (text, backend_name).
"""

import logging

logger = logging.getLogger(__name__)

_initialized = False


def _ensure_init():
    global _initialized
    if _initialized:
        return
    _initialized = True
    try:
        from utilities.google_auth_utils import get_secret
        from utilities.postgres_utils import db_cursor
        from utilities.claude_utils import _get_api_key, log_api_usage
        from utilities import kumori_free_llms

        kumori_free_llms.init(
            app_name='crab_travel',
            get_secret_fn=get_secret,
            db_cursor_fn=db_cursor,
            anthropic_key_fn=_get_api_key,
            log_api_usage_fn=log_api_usage,
        )
    except Exception as e:
        logger.error(f"kumori_free_llms init failed: {e}")


def generate(prompt, max_tokens=500, temperature=1.0, caller='crawl'):
    """Round-robin across free backends, paid fallbacks, haiku last resort.
    Returns: (text, backend_name) or (None, None)."""
    _ensure_init()
    from utilities.kumori_free_llms import generate as _generate
    return _generate(prompt, max_tokens=max_tokens, temperature=temperature, caller=caller)
