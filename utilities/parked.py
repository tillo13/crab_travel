"""Parked mode for a fleet Flask app: the site stays up and clickable, nothing new happens.

CANONICAL LOCATION: _local_infrastructure/deploy/parked.py, vendored by `deploy` to
utilities/parked.py (deploy.json shared_files). `deploy --park "why"` sets APP_MODE=parked
on the upload and `deploy --revive` clears it; this module is a no-op unless it is set.

While parked:
  - reads work: every GET/HEAD/OPTIONS is served as usual from the existing data;
  - writes do not: any other method gets a 503 "paused" page (JSON for API callers), so no
    sign-ups, form mail, saves or uploads;
  - every HTML page carries a one-line "paused" banner.
Scheduled work is stopped by the deploy tool itself (it uploads a cron list without the
jobs), not here. Anything an app does live on a GET, such as calling a paid API while
rendering a page, has to check is_parked() itself.

    from utilities.parked import install_parked
    install_parked(app)
"""
import os

BANNER = ('<div style="background:#fff3cd;color:#222;padding:8px 12px;text-align:center;'
          'font:14px/1.4 system-ui,sans-serif;border-bottom:1px solid #d6c27a">'
          'This site is paused. You can look around, but nothing new is being added right now.'
          '</div>')
PAUSED_HTML = ('<!doctype html><meta charset="utf-8"><title>Paused</title>'
               '<body style="font:16px/1.5 system-ui,sans-serif;max-width:36em;margin:4em auto;padding:0 1em">'
               '<h1>This site is paused</h1><p>You can still look around, but it is not taking '
               'new sign-ups, messages or changes right now.</p><p><a href="/">Back to the home page</a></p>')


def is_parked():
    return os.environ.get('APP_MODE') == 'parked'


def install_parked(app):
    """Register the parked-mode hooks. Safe to call twice; does nothing unless parked."""
    if not is_parked() or getattr(app, '_parked_installed', False):
        return
    app._parked_installed = True
    from flask import request, jsonify

    @app.before_request
    def _parked_block_writes():
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return None
        if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
            return jsonify({'error': 'paused', 'message': 'This site is paused.'}), 503
        return PAUSED_HTML, 503

    @app.after_request
    def _parked_banner(resp):
        if resp.mimetype != 'text/html' or resp.direct_passthrough or resp.status_code >= 300:
            return resp
        html = resp.get_data(as_text=True)
        i = html.find('<body')
        j = html.find('>', i) if i != -1 else -1
        if j != -1:
            resp.set_data(html[:j + 1] + BANNER + html[j + 1:])
        return resp

    @app.context_processor
    def _parked_flag():
        return {'site_parked': True}
