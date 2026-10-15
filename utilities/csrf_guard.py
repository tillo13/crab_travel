"""csrf_guard: refuse state-changing requests that a browser sent from another site.

Why it exists: a 2026-10-14 audit of 29 Flask apps found CSRF tokens in one (ooqio) and an explicit
SameSite on the login cookie in nine. Without SameSite, Safari and Firefox send the session cookie on a
cross-site form POST, so any page a signed-in user visits could submit inroads' /profile/save or kumori's
admin forms as them. Two layers, the OWASP CSRF cheat sheet's defense in depth for apps without tokens:

  1. the session cookie is SameSite=Lax (and Secure when served over HTTPS in production);
  2. POST/PUT/PATCH/DELETE is refused when the browser says it came from another site: the
     Sec-Fetch-Site header (sent by every current browser) is 'cross-site', or, from an older
     browser that sends only Origin, the Origin's host is not this host.

A request with neither header is not from a browser (webhooks, Cloud Scheduler, smoke tests, curl) and
passes: those carry no victim's cookie, which is the whole attack. 'same-site' passes, so a sibling
subdomain (www vs apex) keeps working; *.appspot.com and *.run.app are public suffixes, so another app
there counts as cross-site. A route that must take browser POSTs from elsewhere (an OAuth form_post,
an embedded widget) goes in `exempt`, or its origin in `trusted_origins`.

Wire a site in via deploy.json shared_files (spam_control/csrf_guard.py -> utilities/csrf_guard.py),
then after the app's own config is set:

    from utilities.csrf_guard import install as install_csrf_guard
    install_csrf_guard(app)
"""
import logging
import os
from urllib.parse import urlsplit

from flask import jsonify, request

logger = logging.getLogger(__name__)

UNSAFE = frozenset(('POST', 'PUT', 'PATCH', 'DELETE'))


def _host(value):
    return (urlsplit(value).netloc if '//' in value else value).lower()


def verdict(method, path, sec_fetch_site, origin, hosts, exempt=(), trusted_origins=()):
    """None to let the request through, or a short reason it was refused."""
    if method not in UNSAFE or path.startswith(tuple(exempt)):
        return None
    if origin and origin.rstrip('/') in trusted_origins:
        return None
    if sec_fetch_site:
        return 'sec-fetch-site cross-site' if sec_fetch_site.lower() == 'cross-site' else None
    if not origin:
        return None
    if origin == 'null' or _host(origin) not in hosts:
        return 'origin ' + origin[:80]
    return None


def _in_production():
    return bool(os.environ.get('GAE_ENV', '').startswith('standard') or os.environ.get('K_SERVICE'))


def install(app, exempt=(), trusted_origins=()):
    """Set the cookie defaults the app left unset and register the cross-site check."""
    if not app.config.get('SESSION_COOKIE_SAMESITE'):
        app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    if _in_production():
        app.config['SESSION_COOKIE_SECURE'] = True
    trusted = frozenset(o.rstrip('/') for o in trusted_origins)

    def _csrf_guard():
        hosts = {h.lower() for h in (request.host, request.headers.get('X-Forwarded-Host', '').split(',')[0].strip()) if h}
        reason = verdict(request.method, request.path, request.headers.get('Sec-Fetch-Site'),
                         request.headers.get('Origin'), hosts, exempt, trusted)
        if not reason:
            return None
        logger.warning('csrf_guard: refused %s %s (%s)', request.method, request.path[:120], reason)
        if request.is_json or request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
            return jsonify(error='This request came from another site, so it was refused. Reload the page and try again.'), 403
        return 'This request came from another site, so it was refused. Reload the page and try again.\n', 403, \
            {'Content-Type': 'text/plain; charset=utf-8'}

    app.before_request(_csrf_guard)
    return _csrf_guard
