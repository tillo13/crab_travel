"""
Shared auth decorators and helpers for all blueprint files.

app.py sets AUTH_ENABLED after OAuth initializes; blueprints just import
and use login_required / api_auth_required.
"""
import logging
from functools import wraps

from flask import session, redirect, jsonify, request

logger = logging.getLogger(__name__)

# Default True (prod); app.py calls set_auth_enabled(bool) after OAuth init
AUTH_ENABLED = True


def set_auth_enabled(enabled):
    global AUTH_ENABLED
    AUTH_ENABLED = enabled


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and 'user' not in session:
            logger.info(f"🚫 login_required blocked: session keys={list(session.keys())}, path={request.path}")
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def api_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and 'user' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return decorated


def bearer_auth_required(secret_name):
    """Bearer-token auth for server-to-server callers (e.g. OpenCrab VPS).
    Expects `Authorization: Bearer <token>` matching the GCP secret named by secret_name.
    """
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            from utilities.google_auth_utils import get_secret
            auth_header = request.headers.get('Authorization', '')
            if not auth_header.startswith('Bearer '):
                return jsonify({'error': 'Missing bearer token'}), 401
            supplied = auth_header[len('Bearer '):].strip()
            try:
                expected = get_secret(secret_name)
            except Exception as e:
                logger.error(f"bearer_auth: secret lookup failed for {secret_name}: {e}")
                return jsonify({'error': 'Auth misconfigured'}), 500
            if not expected or supplied != expected:
                logger.warning(f"bearer_auth: token mismatch on {request.path}")
                return jsonify({'error': 'Invalid bearer token'}), 401
            return f(*args, **kwargs)
        return decorated
    return wrapper
