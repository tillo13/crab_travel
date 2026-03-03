import secrets
import logging
from functools import wraps
from flask import request, jsonify, make_response

logger = logging.getLogger(__name__)


def generate_token(length=32):
    return secrets.token_urlsafe(length)


def get_member_token_from_cookie():
    return request.cookies.get('member_token')


def set_member_cookie(response, member_token, max_age=30 * 24 * 60 * 60):
    response.set_cookie('member_token', member_token, max_age=max_age, httponly=True, samesite='Lax')
    return response


def member_or_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import session
        from utilities.postgres_utils import get_member_by_token
        # Check session auth first
        if 'user' in session:
            return f(*args, **kwargs)
        # Check member_token cookie
        token = get_member_token_from_cookie()
        if token:
            member = get_member_by_token(token)
            if member:
                return f(*args, **kwargs)
        return jsonify({'error': 'Access denied'}), 403
    return decorated
