"""
Google Cloud Secret Manager Utilities - Simple Version
Just the get_secret function that works. Caches secrets in memory
to avoid repeated API calls (was 780K ops/month = $7.51/mo).

VERSIONS: every read here is `latest`, and nothing in any repo pins a version number
(checked 2026-09-21). So each secret keeps ONLY its latest version: on rotation, add the
new version, confirm it reads, then DESTROY the old ones in the same pass. Andy never goes
back to an old value, and disabled versions are still billed ($0.06/version/month), which
is how Secret Manager out-cost the shared Cloud SQL. Access is per secret, never
project-wide (task #74). Full policy: kumori-infrastructure skill, "GCP Secret Manager".
"""

import logging
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

_cache = {}
_client = None


def get_secret(secret_id, default=None):
    """Fetch secret from Google Secret Manager with in-memory cache."""
    global _client
    if secret_id in _cache:
        return _cache[secret_id]
    try:
        if _client is None:
            _client = secretmanager.SecretManagerServiceClient()
        name = f"projects/kumori-404602/secrets/{secret_id}/versions/latest"
        response = _client.access_secret_version(request={"name": name})
        value = response.payload.data.decode('UTF-8')
        _cache[secret_id] = value
        return value
    except Exception as e:
        logger.warning(f"Could not fetch secret {secret_id}: {e}")
        return default
