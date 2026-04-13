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
