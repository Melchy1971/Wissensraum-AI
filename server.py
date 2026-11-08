"""Web server for multi-user operation (Release 3.0, docs/sicherheit-3.0.md).

Step 10.3: application core, sign-in over OpenID Connect with PKCE, server-side sessions, CSRF
protection, security headers and a limit on failed sign-ins. Documents, search and questions follow
in 10.4 and 10.7. The server works only with PostgreSQL; ``create_app`` takes any store so that the
contract tests can also run on SQLite.

Start: ``python server.py`` with the environment described in ``settings_from_env``.
"""
import collections
import html
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

import accounts
import sessions
from oidc import Provider, SignInError

SESSION_COOKIE = '__Host-wr_session'
LOGIN_COOKIE = '__Host-wr_login'
CSRF_HEADER = 'x-csrf-token'
CSRF_FIELD = 'csrf_token'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
MAX_FORM = 64 * 1024
FAILED_SIGN_INS = (10, 15 * 60)  # per client address and window in seconds
SIGN_IN_STARTS = (30, 15 * 60)
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class RateLimit:
    """Sliding window per key.

    ponytail: kept in process memory, so a restart resets it and several worker processes count
    separately; the supported setup is one worker. Upgrade path: a counter table in PostgreSQL.
    """

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def _current(self, key):
        hits, edge = self._hits[key], self.clock() - self.window
        while hits and hits[0] <= edge:
            hits.popleft()
        return hits

    def blocked(self, key):
        with self._lock:
            return len(self._current(key)) >= self.limit

    def record(self, key):
        with self._lock:
            self._current(key).append(self.clock())


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


def check_base_url(base_url, development=False):
    """HTTPS is required; plain HTTP only on localhost in development mode."""
    parts = urllib.parse.urlsplit(base_url)
    if not parts.hostname or parts.path not in ('', '/') or parts.query or parts.fragment:
        raise ValueError('WR_BASE_URL muss eine Adresse ohne Pfad sein, z. B. https://wissen.heimnetz.')
    if parts.scheme == 'https' or (development and parts.scheme == 'http'
                                   and parts.hostname in ('localhost', '127.0.0.1')):
        return f'{parts.scheme}://{parts.netloc}'
    raise ValueError('Der Server braucht HTTPS; HTTP nur auf localhost im Entwicklungsmodus.')


def _page(title, body, status=200):
    return HTMLResponse(f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{html.escape(title)}'
                        f' – Wissensraum</title></head><body><h1>{html.escape(title)}</h1>{body}</body></html>',
                        status_code=status)


def create_app(store, provider, base_url, development=False, logger=None, clock=time.time):
    base = check_base_url(base_url, development)
    host = urllib.parse.urlsplit(base).hostname
    failed = RateLimit(*FAILED_SIGN_INS)
    starts = RateLimit(*SIGN_IN_STARTS)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.failed_sign_ins = failed

    def log(event, severity='INFO', **metadata):
        if logger is not None:
            try:
                logger.event(event, severity=severity, metadata=metadata or None)
            except Exception:  # logging never breaks a request
                pass

    def client(request):
        return request.client.host if request.client else 'unbekannt'

    def refuse(request, reason, status=401):
        failed.record(client(request))
        log('sign_in_refused', 'WARNING', reason=reason)
        message = ('Für dieses Konto gibt es keinen Zugang. Bitte die Administration ansprechen.' if status == 403
                   else 'Die Anmeldung ist fehlgeschlagen. Bitte erneut anmelden.')
        response = _page('Anmeldung fehlgeschlagen', f'<p>{message}</p><p><a href="/">Zur Startseite</a></p>', status)
        response.delete_cookie(LOGIN_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    async def csrf_token_of(request):
        token = request.headers.get(CSRF_HEADER)
        if token is None and request.headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
            body = await request.body()
            if len(body) > MAX_FORM:
                return None
            values = urllib.parse.parse_qs(body.decode('latin-1'), max_num_fields=100).get(CSRF_FIELD, [])
            token = values[0] if len(values) == 1 else None
        return token

    def same_origin(request):
        origin = request.headers.get('origin')
        if origin is not None:
            return origin == base
        referer = request.headers.get('referer', '')
        return referer.startswith(base + '/')

    @app.middleware('http')
    async def protect(request: Request, call_next):
        request.state.session = request.state.account = None
        session_id = request.cookies.get(SESSION_COOKIE)
        session = sessions.lookup(store, session_id, clock()) if session_id else None
        if session is not None:
            account = accounts.account(store, session['account_id'])
            if account['disabled']:
                sessions.end(store, session_id)
            else:
                request.state.session, request.state.account = session, account
        if request.method not in SAFE_METHODS:
            token = await csrf_token_of(request)
            if (not same_origin(request) or request.state.session is None or not isinstance(token, str)
                    or not secrets.compare_digest(token, request.state.session['csrf_token'])):
                log('csrf_refused', 'WARNING')
                response = _page('Anfrage abgelehnt', '<p>Die Anfrage ist ungültig. Bitte die Seite neu laden.</p>', 403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        headers = response.headers
        headers['Content-Security-Policy'] = CSP
        headers['X-Content-Type-Options'] = 'nosniff'
        headers['Referrer-Policy'] = 'same-origin'
        headers['X-Frame-Options'] = 'DENY'
        headers['Cache-Control'] = 'no-store'
        if base.startswith('https://'):
            headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    # Added last, so it runs first: requests for another host name never reach the application.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host])

    @app.get('/')
    def start(request: Request):
        account = request.state.account
        if account is None:
            return _page('Wissensraum', '<p><a href="/login">Anmelden</a></p>')
        csrf = html.escape(request.state.session['csrf_token'])
        name = html.escape(account['display_name'] or account['object_id'])
        return _page('Wissensraum', f'<p>Angemeldet als {name} ({html.escape(account["role"])}).</p>'
                     f'<form method="post" action="/logout"><input type="hidden" name="{CSRF_FIELD}" value="{csrf}">'
                     '<button type="submit">Abmelden</button></form>')

    @app.get('/login')
    def login(request: Request):
        if starts.blocked(client(request)) or failed.blocked(client(request)):
            return _page('Zu viele Versuche', '<p>Bitte später erneut versuchen.</p>', 429)
        starts.record(client(request))
        state, nonce, verifier = sessions.start_login(store, clock())
        try:
            target = provider.authorize_url(state, nonce, verifier)
        except (SignInError, OSError, ValueError):
            log('identity_provider_unavailable', 'ERROR')
            return _page('Anmeldung nicht möglich', '<p>Der Anmeldedienst ist nicht erreichbar.</p>', 503)
        response = RedirectResponse(target, status_code=303)
        # Binds the sign-in to this browser, so a foreign callback link cannot sign someone in.
        response.set_cookie(LOGIN_COOKIE, state, max_age=sessions.LOGIN_SECONDS, path='/', secure=True,
                            httponly=True, samesite='lax')
        return response

    @app.get('/auth/callback')
    def callback(request: Request):
        if failed.blocked(client(request)):
            log('sign_in_rate_limited', 'WARNING')
            return _page('Zu viele Versuche', '<p>Bitte später erneut versuchen.</p>', 429)
        params = request.query_params
        if 'error' in params:
            return refuse(request, 'provider_error')
        state, bound = params.get('state', ''), request.cookies.get(LOGIN_COOKIE, '')
        pending = sessions.finish_login(store, state, clock())
        if pending is None:
            return refuse(request, 'state')
        if not bound or not secrets.compare_digest(bound, state):
            return refuse(request, 'browser')
        nonce_digest, verifier = pending
        try:
            claims = provider.verify(provider.redeem(params.get('code', ''), verifier), nonce_digest)
        except SignInError as error:
            return refuse(request, str(error))
        account = accounts.by_identity(store, claims['tid'], claims['oid'])
        if account is None or account['disabled']:
            return refuse(request, 'no_account', 403)
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            sessions.end(store, previous)
        session_id, _ = sessions.create(store, account['id'], clock())
        log('sign_in', account_id=account['id'])
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(SESSION_COOKIE, session_id, path='/', secure=True, httponly=True, samesite='lax')
        response.delete_cookie(LOGIN_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    @app.post('/logout')
    def logout(request: Request):
        account = request.state.account
        with store.connect() as db:
            sessions.end_all(db, account['id'])
        log('sign_out', account_id=account['id'])
        response = RedirectResponse('/', status_code=303)
        response.delete_cookie(SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    return app


def _secret(environ, name):
    """A secret from ``<NAME>_FILE`` (Docker secret) or ``<NAME>``; empty if neither is set."""
    path = environ.get(f'{name}_FILE')
    if path:
        return Path(path).read_text(encoding='utf-8').strip()
    return environ.get(name, '')


def settings_from_env(environ=None):
    """Server settings from the environment.

    ``WR_BASE_URL`` public address, ``WR_OIDC_TENANT_ID``, ``WR_OIDC_CLIENT_ID``,
    ``WR_OIDC_CLIENT_SECRET`` or ``WR_OIDC_CLIENT_SECRET_FILE``, optional ``WR_OIDC_AUTHORITY``
    (default Microsoft Entra ID for the tenant), ``WR_DEVELOPMENT=1`` allows HTTP on localhost.
    Database: ``DB_HOST``, ``DB_PORT``, ``DB_DATABASE``, ``DB_USERNAME``, ``DB_SSLMODE`` and
    ``DB_PASSWORD`` or ``DB_PASSWORD_FILE``; the backend is always PostgreSQL.
    """
    environ = os.environ if environ is None else environ
    development = environ.get('WR_DEVELOPMENT') == '1'
    base = check_base_url(environ.get('WR_BASE_URL', ''), development)
    tenant, client_id = environ.get('WR_OIDC_TENANT_ID', '').strip(), environ.get('WR_OIDC_CLIENT_ID', '').strip()
    secret = _secret(environ, 'WR_OIDC_CLIENT_SECRET')
    if not (tenant and client_id and secret):
        raise ValueError('WR_OIDC_TENANT_ID, WR_OIDC_CLIENT_ID und das Client-Geheimnis sind Pflicht.')
    authority = environ.get('WR_OIDC_AUTHORITY') or f'https://login.microsoftonline.com/{tenant}/v2.0'
    from database import DatabaseSettings
    database, password = DatabaseSettings.validate(
        {'host': environ.get('DB_HOST', ''), 'port': environ.get('DB_PORT', '5432'),
         'database': environ.get('DB_DATABASE', ''), 'username': environ.get('DB_USERNAME', ''),
         'sslmode': environ.get('DB_SSLMODE', 'require')}, _secret(environ, 'DB_PASSWORD'))
    return {'base_url': base, 'development': development, 'database': database, 'db_password': password,
            'provider': Provider(authority, tenant, client_id, secret, base + '/auth/callback'),
            'secrets': (secret, password)}


def main():  # pragma: no cover - starts the real server
    import uvicorn
    from core import PostgresStore
    from local_logging import create_operational_logger
    settings = settings_from_env()
    store = PostgresStore(settings['database'], settings['db_password'])
    logger = create_operational_logger(Path(__file__).resolve().parent, 'server', known_secrets=settings['secrets'])
    app = create_app(store, settings['provider'], settings['base_url'], settings['development'], logger)
    uvicorn.run(app, host=os.environ.get('WR_HOST', '127.0.0.1'), port=int(os.environ.get('WR_PORT', '8000')),
                ssl_certfile=os.environ.get('WR_TLS_CERT_FILE') or None,
                ssl_keyfile=os.environ.get('WR_TLS_KEY_FILE') or None)


if __name__ == '__main__':
    main()
