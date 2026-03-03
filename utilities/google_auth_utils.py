import os
from google.cloud import secretmanager


def get_secret(secret_name, project_id="kumori-404602"):
    env_value = os.getenv(secret_name)
    if env_value:
        return env_value
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        raise ValueError(f"Could not retrieve secret {secret_name}: {e}")
