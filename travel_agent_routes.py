"""
travel_agent routes — secret-link trip boards.

One endpoint:
- GET /trip-board/<token>   serve a stored board (self-contained HTML) by
  unguessable token. 404 on miss.

Boards are authored locally by the travel_agent tooling and stored in
crab.ta_boards (see travel_agent/schema.py::publish_board). There is
deliberately no write endpoint here — content only enters via the local tool,
so this route never serves third-party HTML.
"""
import logging

from flask import Blueprint, Response, render_template

from travel_agent.schema import get_board

logger = logging.getLogger('crab_travel.travel_agent_routes')

bp = Blueprint('travel_agent', __name__)


@bp.route('/trip-board/<token>')
def trip_board(token):
    if not token or len(token) > 32 or not all(c.isalnum() or c in '-_' for c in token):
        return render_template('404.html', active_page=None), 404
    board = get_board(token)
    if not board:
        return render_template('404.html', active_page=None), 404
    return Response(board["html"], mimetype="text/html",
                    headers={"X-Robots-Tag": "noindex, nofollow"})
