"""
URL shortener routes — ported from kumori/blueprints/shorturl_bp.py (Apr 21 2026).

Two endpoints:
- GET  /s/<short_code>    public redirect (302 to the stored long URL)
- POST /api/shorten       auth-gated shortening (only crab.travel URLs)

Table: crab.short_urls (see utilities/shorturl_utils.py).

Domain guard: only URLs containing 'crab.travel' (or localhost for dev) can
be shortened — we're not in the general-purpose shortener business and
arbitrary user-submitted links open phishing vectors.
"""
import logging

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from route_helpers import api_auth_required
from utilities.shorturl_utils import create_short_url, get_long_url

logger = logging.getLogger('crab_travel.shorturl_routes')

bp = Blueprint('shorturl', __name__)


@bp.route('/s/<short_code>')
def redirect_short_url(short_code):
    """Public redirect. Unknown codes → 404."""
    if not short_code or len(short_code) > 10 or not short_code.isalnum():
        return render_template('404.html', active_page=None), 404

    long_url = get_long_url(short_code.lower())
    if long_url:
        if 'crab.travel' in long_url and long_url.startswith('http://'):
            long_url = long_url.replace('http://', 'https://', 1)
        return redirect(long_url, code=302)
    return render_template('404.html', active_page=None), 404


@bp.route('/api/shorten', methods=['POST'])
@api_auth_required
def api_shorten_url():
    """Create a short URL. Only shortens our own URLs."""
    try:
        data = request.get_json(silent=True) or {}
        long_url = (data.get('url') or '').strip()

        if not long_url:
            return jsonify({'error': 'URL is required'}), 400

        if 'crab.travel' not in long_url and 'localhost' not in long_url:
            return jsonify({'error': 'Only crab.travel URLs can be shortened'}), 400

        short_code = create_short_url(long_url)
        if short_code:
            short_url = url_for('shorturl.redirect_short_url', short_code=short_code, _external=True)
            return jsonify({'short_url': short_url, 'short_code': short_code})
        return jsonify({'error': 'Failed to create short URL'}), 500

    except Exception as e:
        logger.error(f"Error creating short URL: {e}")
        return jsonify({'error': 'Internal server error'}), 500
