"""Web server for multi-user operation (Release 3.0, docs/sicherheit-3.0.md).

Step 10.3: application core, sign-in over OpenID Connect with PKCE, server-side sessions, CSRF
protection, security headers and a limit on failed sign-ins. Step 10.4: JSON endpoints under
``/api`` that read only through ``store.as_viewer(account)``, so every list, search, vector search,
graph, original, export and AI context is filtered in the database. Objects that are missing and
objects the account may not see give the same 404. Step 10.5: security events go to the hash-chained
audit trail (``audit.py``) before the response leaves the server. Step 10.6: every change names the
revision it read; a stale one answers 409 with the current state and changes nothing. The web pages
follow in 10.7. The server works only with PostgreSQL; ``create_app`` takes any store so that the
contract tests can also run on SQLite.

Start: ``python server.py`` with the environment described in ``settings_from_env``.
"""
import collections
import html
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import accounts
import audit
from core import Conflict
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


NOT_FOUND = 'Nicht gefunden.'


class NewDocument(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=2_000_000)


class DocumentChange(BaseModel):
    revision: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, min_length=1, max_length=100)


class CollectionChange(BaseModel):
    revision: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    add_documents: list[int] = Field(default=[], max_length=1000)
    remove_documents: list[int] = Field(default=[], max_length=1000)


class EntityChange(BaseModel):
    revision: int
    name: str = Field(min_length=1, max_length=200)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class NewAccount(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    object_id: str = Field(min_length=1, max_length=200)
    role: str
    display_name: str = Field(default='', max_length=200)


class NewGrant(BaseModel):
    account_id: int
    access: str
    document_id: int | None = None
    collection_id: int | None = None
    revision: int | None = None  # of the grant read; 0 when none was there


def create_app(store, provider, base_url, development=False, logger=None, clock=time.time, ai=None):
    """``ai`` builds the answering component for a store (``core.AI``); None: questions answer 503."""
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

    def audited(request, event, account_id=None, target='', **details):
        """Audit entry first (a failure stops the request), then the operational log."""
        audit.record(store, event, account_id, target, client(request), details, clock())
        log(event, 'WARNING' if event == 'sign_in_refused' else 'INFO', account_id=account_id, target=target, **details)

    def refuse(request, reason, status=401):
        failed.record(client(request))
        audited(request, 'sign_in_refused', reason=reason)
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
        audited(request, 'sign_in', account['id'])
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(SESSION_COOKIE, session_id, path='/', secure=True, httponly=True, samesite='lax')
        response.delete_cookie(LOGIN_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    @app.post('/logout')
    def logout(request: Request):
        account = request.state.account
        with store.connect() as db:
            sessions.end_all(db, account['id'])
        audited(request, 'sign_out', account['id'])
        response = RedirectResponse('/', status_code=303)
        response.delete_cookie(SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    # --- API (step 10.4) ------------------------------------------------------------------
    def signed_in(request):
        if request.state.account is None:
            raise HTTPException(401, 'Nicht angemeldet.')
        return request.state.account

    def viewer(request):
        return store.as_viewer(signed_in(request))

    def require_role(request, *roles):
        account = signed_in(request)
        if account['role'] not in roles:
            raise HTTPException(403, 'Keine Berechtigung.')
        return account

    def document(scoped, doc_id):
        try:
            return scoped.get(doc_id)
        except ValueError:
            raise HTTPException(404, NOT_FOUND) from None

    @app.get('/api/documents')
    def list_documents(request: Request, offset: int = 0, limit: int = 50):
        rows = viewer(request).list()
        offset, limit = max(0, offset), max(1, min(limit, 200))
        return {'total': len(rows), 'documents': rows[offset:offset + limit]}

    @app.get('/api/documents/{doc_id}')
    def show_document(request: Request, doc_id: int):
        scoped = viewer(request)
        row = document(scoped, doc_id)
        return {'id': row['id'], 'revision': row['revision'], 'title': row['title'], 'category': row['category'],
                'status': row['status'],
                'created_at': str(row['created_at']), 'content': row['content'],
                'original': row['original'] is not None, 'versions': scoped.versions(doc_id),
                'relations': scoped.relations(doc_id), 'collections': scoped.document_collections(doc_id),
                'graph': scoped.document_graph(doc_id),
                'writable': accounts.can_write(store, request.state.account, doc_id)}

    @app.get('/api/documents/{doc_id}/original')
    def original(request: Request, doc_id: int):
        row = document(viewer(request), doc_id)
        if row['original'] is None:
            raise HTTPException(404, NOT_FOUND)
        name = (row['filename'] or 'original').replace('\\', '/').rsplit('/', 1)[-1]
        name = ''.join(c if c.isalnum() or c in '._- ' else '_' for c in name).strip(' .')[:100] or 'original'
        audited(request, 'original_download', request.state.account['id'], f'document:{doc_id}')
        return Response(bytes(row['original']), media_type='application/octet-stream',
                        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})

    @app.get('/api/search')
    def search(request: Request, q: str = '', limit: int = 20):
        from retrieval import Retriever, snippet
        if not 0 < len(q) <= 500:
            raise HTTPException(422, 'Suchbegriff mit 1 bis 500 Zeichen angeben.')
        result = Retriever(viewer(request)).search(q, limit=max(1, min(limit, 50)))
        return {'mode': result.mode, 'results': [
            {'document_id': r['document_id'], 'title': r['title'], 'ordinal': r['ordinal'],
             'snippet': snippet(r['content'], q)} for r in result.results]}

    @app.get('/api/graph')
    def graph(request: Request, focus: int | None = None):
        try:
            return viewer(request).graph_data(focus=focus)
        except ValueError:
            raise HTTPException(404, NOT_FOUND) from None

    @app.get('/api/entities/{entity_id}')
    def entity(request: Request, entity_id: int):
        try:
            return viewer(request).entity(entity_id)
        except ValueError:
            raise HTTPException(404, NOT_FOUND) from None

    @app.get('/api/collections')
    def collections(request: Request):
        return viewer(request).collections()

    @app.get('/api/collections/{collection_id}/export')
    def export(request: Request, collection_id: int):
        scoped = viewer(request)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'sammlung.zip'
            try:
                report = scoped.export_collection(collection_id, target)
            except ValueError:
                raise HTTPException(404, NOT_FOUND) from None
            data = target.read_bytes()
        audited(request, 'collection_export', request.state.account['id'], f'collection:{collection_id}',
                documents=[d['id'] for d in report['documents']])
        return Response(data, media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="sammlung.zip"'})

    @app.post('/api/questions')
    def question(request: Request, body: Question):
        scoped = viewer(request)
        if ai is None:
            raise HTTPException(503, 'Für den Server ist kein KI-Anbieter eingerichtet.')
        result = ai(scoped).ask(body.question)
        audited(request, 'ai_question', request.state.account['id'], status=result.status,
                sources=sorted({s['document_id'] for s in result.sources if s.get('document_id') is not None}))
        return {'answer': result.text, 'status': result.status, 'sources': [
            {key: source.get(key) for key in ('document_id', 'title', 'ordinal')} for source in result.sources]}

    @app.post('/api/documents', status_code=201)
    def create_document(request: Request, body: NewDocument):
        account = require_role(request, 'admin', 'editor')
        try:
            doc_id, created = store.add(body.title.strip(), body.content)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if not created:
            # ponytail: answers whether this exact text already exists somewhere, without its id or
            # title; guessing that needs the whole text. A per-account copy would remove even that.
            raise HTTPException(409, 'Dieser Inhalt ist bereits vorhanden.')
        accounts.grant(store, account['id'], 'owner', document_id=doc_id, end_sessions=False)
        audited(request, 'document_created', account['id'], f'document:{doc_id}')
        return {'id': doc_id}

    @app.patch('/api/documents/{doc_id}')
    def change_document(request: Request, doc_id: int, body: DocumentChange):
        account = require_role(request, 'admin', 'editor', 'reader')
        document(viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_write(store, account, doc_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        changes = {key: value for key, value in (('title', body.title), ('category', body.category)) if value}
        try:
            store.update_metadata(doc_id, revision=body.revision, **changes)
        except Conflict as error:
            row = store.get(doc_id)
            return conflict(error, {key: row[key] for key in ('id', 'title', 'category', 'revision')})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        audited(request, 'document_changed', account['id'], f'document:{doc_id}', fields=','.join(sorted(changes)))
        return {'id': doc_id}

    @app.post('/api/accounts', status_code=201)
    def create_account(request: Request, body: NewAccount):
        admin = require_role(request, 'admin')
        try:
            created = accounts.create_account(store, body.tenant_id, body.object_id, body.role, body.display_name)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        audited(request, 'account_created', admin['id'], f'account:{created}', role=body.role)
        return {'id': created}

    @app.post('/api/grants', status_code=201)
    def create_grant(request: Request, body: NewGrant):
        admin = require_role(request, 'admin')
        try:
            created = accounts.grant(store, body.account_id, body.access, body.document_id, body.collection_id,
                                     revision=body.revision)
        except Conflict as error:
            return conflict(error, [g for g in accounts.grants(store, body.account_id)
                                    if (g['document_id'], g['collection_id']) == (body.document_id, body.collection_id)])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        target = f'document:{body.document_id}' if body.document_id is not None else f'collection:{body.collection_id}'
        audited(request, 'grant_created', admin['id'], target, account=body.account_id, access=body.access)
        return {'id': created}

    def conflict(error, current):
        """409: nothing was changed; the caller gets the current state to decide again (T2)."""
        return JSONResponse({'detail': str(error), 'current': current}, status_code=409)

    def visible_collection(scoped, collection_id):
        found = [c for c in scoped.collections() if c['id'] == collection_id]
        if not found:
            raise HTTPException(404, NOT_FOUND)
        return found[0]

    @app.patch('/api/collections/{collection_id}')
    def change_collection(request: Request, collection_id: int, body: CollectionChange):
        account = signed_in(request)
        scoped = viewer(request)
        visible_collection(scoped, collection_id)
        if not accounts.can_write_collection(store, account, collection_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        if [bool(body.name), bool(body.add_documents), bool(body.remove_documents)].count(True) != 1:
            # one change per request, so it is applied whole or not at all
            raise HTTPException(422, 'Genau eine Änderung angeben: name, add_documents oder remove_documents.')
        for doc_id in body.add_documents + body.remove_documents:
            document(scoped, doc_id)  # only documents the account sees
        try:
            if body.name:
                store.rename_collection(collection_id, body.name, body.revision)
            elif body.add_documents:
                store.add_to_collection(collection_id, body.add_documents, body.revision)
            else:
                store.remove_from_collection(collection_id, body.remove_documents, body.revision)
        except Conflict as error:
            return conflict(error, visible_collection(scoped, collection_id))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        audited(request, 'collection_changed', account['id'], f'collection:{collection_id}',
                added=body.add_documents, removed=body.remove_documents, renamed=bool(body.name))
        return visible_collection(scoped, collection_id)

    @app.patch('/api/entities/{entity_id}')
    def change_entity(request: Request, entity_id: int, body: EntityChange):
        scoped = viewer(request)
        try:
            scoped.entity(entity_id)
        except ValueError:
            raise HTTPException(404, NOT_FOUND) from None
        admin = require_role(request, 'admin')
        try:
            store.rename_entity(entity_id, body.name, body.revision)
        except Conflict as error:
            current = scoped.entity(entity_id)
            return conflict(error, {key: current[key] for key in ('id', 'name', 'revision')})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        audited(request, 'graph_changed', admin['id'], f'entity:{entity_id}')
        return {'id': entity_id}

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
    ``DB_PASSWORD`` or ``DB_PASSWORD_FILE``; the backend is always PostgreSQL. Questions to the AI:
    ``WR_AI_PROVIDER`` (``openai``, ``anthropic``, ``gemini`` or ``ollama``), ``WR_AI_MODEL`` and
    ``WR_AI_KEY`` or ``WR_AI_KEY_FILE``; without a provider questions answer 503.
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
    ai, ai_key = None, _secret(environ, 'WR_AI_KEY')
    ai_provider = environ.get('WR_AI_PROVIDER', '').strip()
    if ai_provider:
        from core import AI
        from settings import PROVIDERS
        if ai_provider not in PROVIDERS:
            raise ValueError('WR_AI_PROVIDER ist unbekannt.')
        model = environ.get('WR_AI_MODEL') or PROVIDERS[ai_provider]['models'][0]
        AI(None, ai_key, model, provider=ai_provider)  # checks key and model before the server starts
        ai = lambda scoped: AI(scoped, ai_key, model, provider=ai_provider)  # noqa: E731
    return {'base_url': base, 'development': development, 'database': database, 'db_password': password,
            'provider': Provider(authority, tenant, client_id, secret, base + '/auth/callback'),
            'ai': ai, 'secrets': tuple(value for value in (secret, password, ai_key) if value)}


def main():  # pragma: no cover - starts the real server
    import uvicorn
    from core import PostgresStore
    from local_logging import create_operational_logger
    settings = settings_from_env()
    store = PostgresStore(settings['database'], settings['db_password'])
    logger = create_operational_logger(Path(__file__).resolve().parent, 'server', known_secrets=settings['secrets'])
    app = create_app(store, settings['provider'], settings['base_url'], settings['development'], logger,
                     ai=settings['ai'])
    uvicorn.run(app, host=os.environ.get('WR_HOST', '127.0.0.1'), port=int(os.environ.get('WR_PORT', '8000')),
                ssl_certfile=os.environ.get('WR_TLS_CERT_FILE') or None,
                ssl_keyfile=os.environ.get('WR_TLS_KEY_FILE') or None)


if __name__ == '__main__':
    main()
