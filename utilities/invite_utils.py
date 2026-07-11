import secrets
import logging

logger = logging.getLogger(__name__)


def generate_token(length=32):
    return secrets.token_urlsafe(length)
