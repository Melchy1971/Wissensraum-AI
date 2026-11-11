"""Web server for multi-user operation (Release 3.0, docs/sicherheit-3.0.md).

Step 10.3: application core, sign-in over OpenID Connect with PKCE, server-side sessions, CSRF
protection, security headers and a limit on failed sign-ins. Step 10.4: JSON endpoints under
``/api`` that read only through ``store.as_viewer(account)``, so every list, search, vector search,
graph, original, export and AI context is filtered in the database. Objects that are missing and
objects the account may not see give the same 404. Step 10.5: security events go to the hash-chained
audit trail (``audit.py``) before the response leaves the server. Step 10.6: every change names the
revision it read; a stale one answers 409 with the current state and changes nothing. The web pages
are in ``web.py`` (step 10.7). The server works only with PostgreSQL; ``create_app`` takes any store so that the
contract tests can also run on SQLite.

Start: ``python server.py`` with the environment described in ``settings_from_env``.
"""
import collections
import datetime
import html
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import accounts
import audit
from core import Conflict
import services
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
    status: str | None = Field(default=None, max_length=20)
    owner: str | None = Field(default=None, max_length=200)
    review_date: str | None = Field(default=None, max_length=10)  # '' removes the date
    tags: list[str] | None = Field(default=None, max_length=50)  # the complete new list



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


def create_app(store, provider, base_url, development=False, logger=None, clock=time.time, ai=None, limits=None,
               secret_expires=None, export_dir=None, graph_model=None):
    """``ai`` builds the answering component for a store (``core.AI``); None: questions answer 503.

    ``secret_expires`` is the expiry date of the Entra client secret (S-11); administrators see a warning.
    ``export_dir`` is the folder the worker writes the installation export to (``WR_EXPORT_DIR``).
    ``graph_model`` is the Ollama model for AI graph suggestions; None: suggestions are not offered (D-023).

    ``limits`` (``services.Limits``) replaces the default rate limits per account, for tests.
    """
    base = check_base_url(base_url, development)
    host = urllib.parse.urlsplit(base).hostname
    failed = RateLimit(*FAILED_SIGN_INS)
    starts = RateLimit(*SIGN_IN_STARTS)
    limits = limits or services.Limits()
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

    def recorder(request):
        """``audited`` bound to the request, in the form ``services`` expects."""
        return lambda event, account_id=None, target='', **details: audited(request, event, account_id, target,
                                                                            **details)

    def limited(request, kind):
        """429 once the signed-in account used up its requests of ``kind`` (S-06)."""
        if not limits.allow(kind, signed_in(request)['id']):
            log('rate_limited', 'WARNING', operation=kind)
            raise HTTPException(429, 'Zu viele Anfragen. Bitte etwas später erneut versuchen.')

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
        with store.transaction() as db:
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
    def list_documents(request: Request, offset: int = 0, limit: int = 50, tag: str = '', favorites: bool = False):
        rows = viewer(request).list(tag=tag.strip()[:100] or None, favorites=favorites)  # favorites: own account
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
    def search(request: Request, q: str = '', limit: int = 20, tag: str = ''):
        from retrieval import Retriever, snippet
        if not 0 < len(q) <= 500 or len(tag) > 100:
            raise HTTPException(422, 'Suchbegriff mit 1 bis 500 Zeichen angeben.')
        limited(request, 'search')
        result = Retriever(viewer(request)).search(q, limit=max(1, min(limit, 50)), tag=tag.strip() or None)
        return {'mode': result.mode, 'status': result.status, 'results': [
            {'document_id': r['document_id'], 'title': r['title'], 'ordinal': r['ordinal'],
             'method': r.get('retrieval_method'), 'snippet': snippet(r['content'], q)} for r in result.results]}

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
        limited(request, 'question')
        try:
            result = services.ask(scoped, ai, recorder(request), request.state.account, body.question)
        except services.Unavailable as error:
            raise HTTPException(503, str(error)) from None
        return {'answer': result.text, 'status': result.status, 'sources': [
            {key: source.get(key) for key in ('document_id', 'title', 'ordinal')} for source in result.sources]}

    @app.post('/api/documents', status_code=201)
    def create_document(request: Request, body: NewDocument):
        account = require_role(request, 'admin', 'editor')
        limited(request, 'upload')
        try:
            doc_id = services.create_document(store, recorder(request), account, body.title.strip(), body.content)
        except services.Duplicate as error:
            # ponytail: answers whether this exact text already exists somewhere, without its id or
            # title; guessing that needs the whole text. Kept on purpose (decision 10.11.2026, S-05).
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {'id': doc_id}

    @app.patch('/api/documents/{doc_id}')
    def change_document(request: Request, doc_id: int, body: DocumentChange):
        account = require_role(request, 'admin', 'editor', 'reader')
        document(viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_write(store, account, doc_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        fields = body.model_dump(exclude={'revision'}, exclude_none=True)
        try:
            services.change_document(store, recorder(request), account, doc_id, body.revision, **fields)
        except Conflict as error:
            row = store.get(doc_id)
            return conflict(error, {key: row[key] for key in ('id', 'title', 'category', 'status', 'owner',
                                                               'review_date', 'revision')})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {'id': doc_id}

    @app.delete('/api/documents/{doc_id}')
    def delete_document(request: Request, doc_id: int, revision: int):
        account = signed_in(request)
        document(viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_delete(store, account, doc_id):
            raise HTTPException(403, 'Löschen dürfen nur der Besitzer des Dokuments und Administratoren.')
        try:
            services.delete_document(store, recorder(request), account, doc_id, revision)
        except Conflict as error:
            row = store.get(doc_id)
            return conflict(error, {key: row[key] for key in ('id', 'title', 'revision')})
        return {'id': doc_id}

    @app.post('/api/accounts', status_code=201)
    def create_account(request: Request, body: NewAccount):
        admin = require_role(request, 'admin')
        try:
            created = services.create_account(store, recorder(request), admin, body.tenant_id, body.object_id,
                                              body.role, body.display_name)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {'id': created}

    @app.post('/api/grants', status_code=201)
    def create_grant(request: Request, body: NewGrant):
        admin = require_role(request, 'admin')
        try:
            created = services.grant(store, recorder(request), admin, body.account_id, body.access, body.document_id,
                                     body.collection_id, revision=body.revision)
        except Conflict as error:
            return conflict(error, [g for g in accounts.grants(store, body.account_id)
                                    if (g['document_id'], g['collection_id']) == (body.document_id, body.collection_id)])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
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
        for doc_id in body.add_documents + body.remove_documents:
            document(scoped, doc_id)  # only documents the account sees
        if not all(accounts.can_write(store, account, doc_id) for doc_id in body.add_documents):
            # A collection grant shows its documents to others: only who may change a document may put it
            # there, read access does not pass it on (decision 11.11.2026).
            raise HTTPException(403, 'Hinzufügen nur mit Schreibrecht am Dokument.')
        try:
            services.change_collection(store, recorder(request), account, collection_id, body.revision, body.name,
                                       body.add_documents, body.remove_documents)
        except Conflict as error:
            return conflict(error, visible_collection(scoped, collection_id))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
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
            with store.transaction():
                store.rename_entity(entity_id, body.name, body.revision)
                audited(request, 'graph_changed', admin['id'], f'entity:{entity_id}')
        except Conflict as error:
            current = scoped.entity(entity_id)
            return conflict(error, {key: current[key] for key in ('id', 'name', 'revision')})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {'id': entity_id}

    @app.get('/healthz')
    def healthz():
        """For the container health check: database reachable, nothing else revealed."""
        try:
            with store.connect() as db:
                db.execute('SELECT 1').fetchone()
        except Exception:
            return JSONResponse({'status': 'unavailable'}, status_code=503)
        return {'status': 'ok'}

    import web  # the pages of step 10.7, with the same helpers as the API
    warning = secret_warning(secret_expires, datetime.date.fromtimestamp(clock()))
    if warning:
        log('client_secret', 'WARNING', status=warning[0])
    web.register(app, SimpleNamespace(store=store, signed_in=signed_in, viewer=viewer, require_role=require_role,
                                      document=document, audited=audited, ai=ai, recorder=recorder, limited=limited,
                                      secret_expires=secret_expires, clock=clock, export_dir=export_dir,
                                      graph_model=graph_model))
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
    ``WR_AI_KEY`` or ``WR_AI_KEY_FILE``; without a provider questions answer 503. ``WR_OLLAMA_URL``
    (for example ``http://ollama:11434``) points ``ollama`` at another container.
    """
    environ = os.environ if environ is None else environ
    development = environ.get('WR_DEVELOPMENT') == '1'
    base = check_base_url(environ.get('WR_BASE_URL', ''), development)
    tenant, client_id = environ.get('WR_OIDC_TENANT_ID', '').strip(), environ.get('WR_OIDC_CLIENT_ID', '').strip()
    secret = _secret(environ, 'WR_OIDC_CLIENT_SECRET')
    if not (tenant and client_id and secret):
        raise ValueError('WR_OIDC_TENANT_ID, WR_OIDC_CLIENT_ID und das Client-Geheimnis sind Pflicht.')
    authority = environ.get('WR_OIDC_AUTHORITY') or f'https://login.microsoftonline.com/{tenant}/v2.0'
    database, password = database_from_env(environ)
    ai, ai_key = None, _secret(environ, 'WR_AI_KEY')
    ai_provider = environ.get('WR_AI_PROVIDER', '').strip()
    if ai_provider:
        from core import AI
        from settings import PROVIDERS
        if ai_provider not in PROVIDERS:
            raise ValueError('WR_AI_PROVIDER ist unbekannt.')
        model = environ.get('WR_AI_MODEL') or PROVIDERS[ai_provider]['models'][0]
        endpoint = None
        if ai_provider == 'ollama' and environ.get('WR_OLLAMA_URL'):
            parts = urllib.parse.urlsplit(environ['WR_OLLAMA_URL'])
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.path not in ('', '/'):
                raise ValueError('WR_OLLAMA_URL braucht die Form http://host:port.')
            endpoint = f'{parts.scheme}://{parts.netloc}/api/chat'
        AI(None, ai_key, model, provider=ai_provider)  # checks key and model before the server starts
        ai = lambda scoped: AI(scoped, ai_key, model, provider=ai_provider, endpoint=endpoint)  # noqa: E731
    return {'base_url': base, 'development': development, 'database': database, 'db_password': password,
            'provider': Provider(authority, tenant, client_id, secret, base + '/auth/callback'),
            'ai': ai, 'secrets': tuple(value for value in (secret, password, ai_key) if value),
            'secret_expires': secret_expiry(environ), 'export_dir': environ.get('WR_EXPORT_DIR') or None,
            'graph_model': graph_model(environ)}


SECRET_WARNING_DAYS = 30


def graph_model(environ):
    """Model for AI graph suggestions: only with the local Ollama as provider (D-023), else None."""
    if environ.get('WR_AI_PROVIDER', '').strip() != 'ollama':
        return None
    from settings import PROVIDERS
    return environ.get('WR_AI_MODEL') or PROVIDERS['ollama']['models'][0]


def secret_expiry(environ):
    """``WR_OIDC_CLIENT_SECRET_EXPIRES`` (YYYY-MM-DD) as a date, None when not set (S-11)."""
    raw = environ.get('WR_OIDC_CLIENT_SECRET_EXPIRES', '').strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ValueError('WR_OIDC_CLIENT_SECRET_EXPIRES braucht die Form JJJJ-MM-TT.') from None


def secret_warning(expires, today):
    """``(status, text)`` when the client secret is unknown, expires within 30 days or has expired."""
    if expires is None:
        return ('unbekannt', 'Ablaufdatum des Client-Geheimnisses ist nicht hinterlegt (WR_OIDC_CLIENT_SECRET_EXPIRES).')
    days = (expires - today).days
    if days < 0:
        return ('abgelaufen', f'Das Client-Geheimnis ist am {expires.strftime("%d.%m.%Y")} abgelaufen. '
                              'Anmeldungen schlagen fehl, bis ein neues hinterlegt ist.')
    if days <= SECRET_WARNING_DAYS:
        return ('bald', f'Das Client-Geheimnis läuft in {days} Tagen ab ({expires.strftime("%d.%m.%Y")}). '
                        'Neues Geheimnis in Entra ID anlegen und hinterlegen.')
    return None


def configure_ollama(environ):
    """``WR_OLLAMA_URL`` also for embeddings (search, worker); without it the local default stays."""
    import core
    core.use_ollama_at(environ.get('WR_OLLAMA_URL') or None)


def database_from_env(environ, prefix='DB'):
    """PostgreSQL profile and password from ``<prefix>_HOST`` and so on; ``<prefix>_PASSWORD_FILE`` first."""
    from database import DatabaseSettings
    return DatabaseSettings.validate(
        {'host': environ.get('DB_HOST', ''), 'port': environ.get('DB_PORT', '5432'),
         'database': environ.get('DB_DATABASE', ''), 'username': environ.get(f'{prefix}_USERNAME', ''),
         'sslmode': environ.get('DB_SSLMODE', 'require')}, _secret(environ, f'{prefix}_PASSWORD'))


ROLE_NAME = r'[a-z_][a-z0-9_]{0,62}'


def grant_runtime_role(store, role):
    """Rights of the server's runtime role (step 10.8, docs/sicherheit-3.0.md, Audit-Verlauf).

    Read and write on the application tables, no DDL, only SELECT on the migration history and only
    SELECT and INSERT on the audit trail. Run by the owner role after every migration; idempotent.
    """
    import re
    if not re.fullmatch(ROLE_NAME, role or ''):
        raise ValueError('Ungültiger Rollenname für die Laufzeitrolle.')
    with store.connect() as db:
        for statement in (
                'GRANT USAGE ON SCHEMA wissensraum TO {role}',
                'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA wissensraum TO {role}',
                'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wissensraum TO {role}',
                'REVOKE INSERT, UPDATE, DELETE ON wissensraum.schema_migrations FROM {role}',
                'REVOKE UPDATE, DELETE, TRUNCATE ON wissensraum.audit_log FROM {role}'):
            # The role name is checked against ROLE_NAME above; identifiers cannot be bound as parameters.
            db.execute(statement.format(role=role))  # nosec B608


def migrate(environ=None):
    """``python server.py migrate``: migrate with the owner role (``DB_MIGRATION_USERNAME`` and
    ``DB_MIGRATION_PASSWORD_FILE``) and grant the runtime role ``DB_USERNAME`` its rights."""
    from core import PostgresStore
    environ = os.environ if environ is None else environ
    owner, password = database_from_env(environ, 'DB_MIGRATION')
    runtime, _ = database_from_env(environ)
    if owner['username'] == runtime['username']:
        raise ValueError('Migration und Betrieb brauchen getrennte Datenbankrollen.')
    store = PostgresStore(owner, password)
    grant_runtime_role(store, runtime['username'])
    return store


def runtime_store(settings):
    """The server's store: no migration, only the check that the schema is current."""
    from core import PostgresStore
    from migrations import check_postgres_current
    store = PostgresStore(settings['database'], settings['db_password'], initialize=False)
    with store.connect() as db:
        check_postgres_current(db)
    return store


def add_account(store, argv):
    """``python server.py account-add --tenant T --object O --role admin [--name N]`` in the container:
    the first administrator (docs/sicherheit-3.0.md, no self-registration); recorded in the audit trail."""
    import argparse
    import json
    parser = argparse.ArgumentParser(prog='server.py account-add')
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--object', required=True)
    parser.add_argument('--role', required=True, choices=accounts.ROLES)
    parser.add_argument('--name', default='')
    args = parser.parse_args(argv)
    with store.transaction():
        created = accounts.create_account(store, args.tenant, args.object, args.role, args.name)
        audit.record(store, 'account_created', None, f'account:{created}', 'cli', {'role': args.role})
    return json.dumps(accounts.account(store, created), ensure_ascii=False)


def audit_check(store, argv):
    """``audit-verify [--expect ANZAHL:HASH]``: (JSON text, exit code); needs only the database.

    Used in the container and by the restore drill against a restored copy (parity roadmap phase 0, S-03).
    """
    import argparse
    import json
    parser = argparse.ArgumentParser(prog='server.py audit-verify')
    parser.add_argument('--expect', metavar='ANZAHL:HASH')
    args = parser.parse_args(argv)
    expected = None
    if args.expect:
        count, _, head = args.expect.partition(':')
        if not count.isdigit() or len(head) != 64:
            raise ValueError('--expect erwartet ANZAHL:HASH aus einer früheren Prüfung.')
        expected = (int(count), head)
    result = audit.verify(store, expected)
    return json.dumps(result, ensure_ascii=False), 0 if result['ok'] else 1


class PathOnlyAccessLog:
    """Logging filter for ``uvicorn.access``: keeps the path, drops the query string.

    Uvicorn writes the full request target, so ``/auth/callback?code=...&state=...`` would put the
    sign-in code into the container log and ``/suche?q=...`` the search words (parity roadmap phase 0, S-01).
    """

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split('?', 1)[0], *args[3:])
        return True


def quiet_access_log():
    """Install ``PathOnlyAccessLog`` once; call it after uvicorn has configured its logging."""
    import logging
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, PathOnlyAccessLog) for f in access.filters):
        access.addFilter(PathOnlyAccessLog())


def main(argv=None):  # pragma: no cover - starts the real server
    import sys
    import uvicorn
    from local_logging import create_operational_logger
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['migrate']:
        migrate()
        print('Schema aktuell, Rechte der Laufzeitrolle gesetzt.')
        return
    if argv[:1] == ['audit-verify']:  # only the database, no sign-in settings (restore drill)
        database, password = database_from_env(os.environ)
        text, code = audit_check(runtime_store({'database': database, 'db_password': password}), argv[1:])
        print(text)
        raise SystemExit(code)
    settings = settings_from_env()
    configure_ollama(os.environ)
    store = runtime_store(settings)
    if argv[:1] == ['account-add']:
        print(add_account(store, argv[1:]))
        return
    logger = create_operational_logger(Path(os.environ.get('WR_LOG_DIR') or Path(__file__).resolve().parent),
                                       'server', known_secrets=settings['secrets'])
    app = create_app(store, settings['provider'], settings['base_url'], settings['development'], logger,
                     ai=settings['ai'], secret_expires=settings['secret_expires'], export_dir=settings['export_dir'],
                     graph_model=settings['graph_model'])
    # Behind Traefik the client address comes from X-Forwarded-For, trusted only from the proxy.
    config = uvicorn.Config(app, host=os.environ.get('WR_HOST', '127.0.0.1'),
                            port=int(os.environ.get('WR_PORT', '8000')),
                            proxy_headers=True, forwarded_allow_ips=os.environ.get('WR_FORWARDED_ALLOW_IPS', '127.0.0.1'),
                            ssl_certfile=os.environ.get('WR_TLS_CERT_FILE') or None,
                            ssl_keyfile=os.environ.get('WR_TLS_KEY_FILE') or None)
    quiet_access_log()  # after Config, which sets up uvicorn's loggers
    uvicorn.Server(config).run()


if __name__ == '__main__':
    main()
