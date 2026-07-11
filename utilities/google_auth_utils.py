import os
from google.cloud import secretmanager


_secrets_cache = {}
_sm_client = None

def get_secret(secret_name, project_id="kumori-404602"):
    env_value = os.getenv(secret_name)
    if env_value:
        return env_value
    cache_key = f"{project_id}:{secret_name}"
    if cache_key in _secrets_cache:
        return _secrets_cache[cache_key]
    try:
        global _sm_client
        if _sm_client is None:
            _sm_client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = _sm_client.access_secret_version(request={"name": name})
        val = response.payload.data.decode("UTF-8")
        _secrets_cache[cache_key] = val
        return val
    except Exception as e:
        raise ValueError(f"Could not retrieve secret {secret_name}: {e}")
