"""Web pages of the multi-user server (Release 3.0, step 10.7, docs/sicherheit-3.0.md).

Server-rendered with Jinja2 (autoescaping on) and htmx 2.0.11 from ``static/`` (no inline scripts or
styles, so the Content-Security-Policy of step 10.3 stays strict). Every page reads through
``store.as_viewer(account)`` like the API of step 10.4; changes name the revision they read (10.6)
and go to the audit trail (10.5). State-changing requests carry the CSRF token: htmx sends it as a
header for every request of the page, plain forms as the field ``csrf_token``.
"""
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

import accounts
from core import Conflict

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 50
MAX_UPLOAD = 50 * 1024 * 1024  # core.MAX_DOCUMENT_BYTES
FORM_LIMITS = {'max_files': 1, 'max_fields': 20, 'max_part_size': 64 * 1024}
STATUS_TEXT = {401: 'Bitte zuerst anmelden.', 403: 'Dafür fehlt die Berechtigung.',
               404: 'Nicht gefunden.', 413: 'Die Datei ist zu groß.', 422: 'Die Eingabe ist ungültig.'}


def register(app, ctx):
    """Add the pages to ``app``; ``ctx`` holds the helpers of ``server.create_app``."""
    store = ctx.store
    templates = Jinja2Templates(directory=str(ROOT / 'templates'))
    app.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')

    def render(request, name, title, status=200, **values):
        account, session = request.state.account, request.state.session
        return templates.TemplateResponse(request, name, {
            'title': title, 'account': account, 'csrf': session['csrf_token'] if session else '', **values},
            status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def errors(request: Request, error: StarletteHTTPException):
        if request.url.path.startswith('/api/'):
            from fastapi.exception_handlers import http_exception_handler
            return await http_exception_handler(request, error)
        # Same page for missing and invisible objects (I3); details only for input errors.
        message = error.detail if error.status_code == 422 else STATUS_TEXT.get(error.status_code, 'Fehler.')
        return render(request, 'message.html', 'Hinweis', error.status_code, message=message,
                      link='/', link_text='Zur Startseite')

    async def form(request):
        try:
            return await request.form(**FORM_LIMITS)
        except Exception:  # malformed or over the limits
            raise HTTPException(422, 'Das Formular konnte nicht gelesen werden.') from None

    def number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise HTTPException(422, 'Ungültige Angabe.') from None

    @app.get('/', response_class=HTMLResponse)
    def start(request: Request, seite: int = 1):
        if request.state.account is None:
            return render(request, 'message.html', 'Wissensraum', message='Bitte melde dich an.',
                          link='/login', link_text='Anmelden')
        rows = ctx.viewer(request).list()
        page = max(1, seite)
        return render(request, 'documents.html', 'Dokumente', documents=rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
                      total=len(rows), page=page, more=len(rows) > page * PAGE_SIZE)

    @app.get('/suche', response_class=HTMLResponse)
    def search(request: Request, q: str = ''):
        from retrieval import Retriever, snippet
        scoped, q = ctx.viewer(request), q.strip()[:500]
        results = []
        if q:
            results = [{'document_id': r['document_id'], 'title': r['title'], 'ordinal': r['ordinal'],
                        'snippet': snippet(r['content'], q)} for r in Retriever(scoped).search(q, limit=20).results]
        return render(request, 'search.html', 'Suche', q=q, results=results)

    @app.get('/dokument/{doc_id}', response_class=HTMLResponse)
    def document(request: Request, doc_id: int):
        scoped = ctx.viewer(request)
        doc = ctx.document(scoped, doc_id)
        return render(request, 'document.html', doc['title'], doc=doc, collections=scoped.document_collections(doc_id),
                      writable=accounts.can_write(store, request.state.account, doc_id))

    @app.post('/dokument/{doc_id}/metadaten', response_class=HTMLResponse)
    async def metadata(request: Request, doc_id: int):
        account = ctx.signed_in(request)
        ctx.document(ctx.viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_write(store, account, doc_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        values = await form(request)
        changes = {key: values.get(key, '').strip() for key in ('title', 'category') if values.get(key, '').strip()}
        status, notice = 200, 'Gespeichert.'
        try:
            store.update_metadata(doc_id, revision=number(values.get('revision')), **changes)
            ctx.audited(request, 'document_changed', account['id'], f'document:{doc_id}',
                        fields=','.join(sorted(changes)))
        except Conflict:
            status, notice = 409, ('Jemand hat das Dokument inzwischen geändert. Angezeigt ist der aktuelle Stand; '
                                   'bitte die Änderung erneut eintragen.')
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return render(request, 'metadata_result.html', '', status, doc=store.get(doc_id), notice=notice)

    @app.get('/hochladen', response_class=HTMLResponse)
    def upload_page(request: Request):
        ctx.require_role(request, 'admin', 'editor')
        return render(request, 'upload.html', 'Hochladen')

    @app.post('/hochladen', response_class=HTMLResponse)
    async def upload(request: Request):
        from core import ImportRejected, OcrDeferred, extract_document
        account = ctx.require_role(request, 'admin', 'editor')
        if int(request.headers.get('content-length') or 0) > MAX_UPLOAD + 1024 * 1024:
            raise HTTPException(413, 'Die Datei ist zu groß.')
        values = await form(request)
        upload = values.get('file')
        if upload is None or not hasattr(upload, 'read'):
            raise HTTPException(422, 'Bitte eine Datei auswählen.')
        try:
            data = await upload.read(MAX_UPLOAD + 1)
        finally:
            await values.close()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, 'Die Datei ist zu groß.')
        try:
            title, text, name, original = extract_document(upload.filename or 'Dokument', data, allow_ocr=False)
            doc_id, created = store.add(title, text, (values.get('category') or 'Allgemein').strip()[:100] or 'Allgemein',
                                        filename=name, original=original)
        except OcrDeferred:
            raise HTTPException(422, 'Bilder brauchen die Texterkennung; der Server nimmt sie noch nicht an.') from None
        except (ImportRejected, ValueError) as error:
            raise HTTPException(422, str(error)) from None
        if not created:
            raise HTTPException(409, 'Dieser Inhalt ist bereits vorhanden.')
        accounts.grant(store, account['id'], 'owner', document_id=doc_id, end_sessions=False)
        ctx.audited(request, 'document_created', account['id'], f'document:{doc_id}', upload=True)
        response = HTMLResponse(f'<p>Gespeichert. <a href="/dokument/{doc_id}">Zum Dokument</a></p>', 201)
        response.headers['HX-Redirect'] = f'/dokument/{doc_id}'
        return response

    @app.get('/fragen', response_class=HTMLResponse)
    def questions(request: Request):
        return render(request, 'questions.html', 'Fragen', history=ctx.viewer(request).history())

    @app.post('/fragen', response_class=HTMLResponse)
    async def ask(request: Request):
        scoped = ctx.viewer(request)
        question = (await form(request)).get('question', '').strip()
        if not 0 < len(question) <= 4000:
            raise HTTPException(422, 'Bitte eine Frage mit bis zu 4000 Zeichen eingeben.')
        if ctx.ai is None:
            raise HTTPException(503, 'Für den Server ist kein KI-Anbieter eingerichtet.')
        result = ctx.ai(scoped).ask(question)
        sources = [s for s in result.sources if s.get('document_id') is not None]
        ctx.audited(request, 'ai_question', request.state.account['id'], status=result.status,
                    sources=sorted({s['document_id'] for s in sources}))
        return render(request, 'answer.html', '', answer=result.text, sources=sources)

    @app.get('/sammlungen', response_class=HTMLResponse)
    def collection_page(request: Request):
        return render(request, 'collections.html', 'Sammlungen', collections=ctx.viewer(request).collections())

    # --- administration --------------------------------------------------------------------
    def admin_page(request, notice='', status=200):
        names = {a['id']: a['display_name'] or a['object_id'] for a in accounts.accounts(store)}
        documents = {d['id']: d['title'] for d in store.list(all_versions=True)}
        collections = {c['id']: c['name'] for c in store.collections()}
        grants = []
        for account_id, name in names.items():
            for g in accounts.grants(store, account_id):
                target = f"document:{g['document_id']}" if g['document_id'] is not None else f"collection:{g['collection_id']}"
                label = (f"Dokument: {documents.get(g['document_id'], g['document_id'])}" if g['document_id'] is not None
                         else f"Sammlung: {collections.get(g['collection_id'], g['collection_id'])}")
                grants.append({**g, 'account': name, 'account_id': account_id, 'target': target, 'target_name': label})
        return render(request, 'admin.html', 'Verwaltung', status, notice=notice, accounts=accounts.accounts(store),
                      roles=accounts.ROLES, access=accounts.ACCESS, grants=grants,
                      documents=[{'id': k, 'title': v} for k, v in documents.items()],
                      collections=[{'id': k, 'name': v} for k, v in collections.items()])

    def target_of(value):
        kind, _, raw = str(value or '').partition(':')
        if kind not in ('document', 'collection'):
            raise HTTPException(422, 'Ungültiges Ziel.')
        return {'document_id': number(raw)} if kind == 'document' else {'collection_id': number(raw)}

    @app.get('/verwaltung', response_class=HTMLResponse)
    def admin(request: Request):
        ctx.require_role(request, 'admin')
        return admin_page(request)

    @app.post('/verwaltung/konten', response_class=HTMLResponse)
    async def admin_create_account(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        try:
            created = accounts.create_account(store, values.get('tenant_id', ''), values.get('object_id', ''),
                                              values.get('role', ''), values.get('display_name', ''))
        except ValueError as error:
            return admin_page(request, str(error), 422)
        ctx.audited(request, 'account_created', admin_account['id'], f'account:{created}', role=values.get('role'))
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/konten/{account_id}', response_class=HTMLResponse)
    async def admin_change_account(request: Request, account_id: int):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        disabled = values.get('disabled') == '1'
        try:
            changed = accounts.update_account(store, account_id, number(values.get('version')),
                                              role=values.get('role'), disabled=disabled)
        except Conflict:
            return admin_page(request, 'Das Konto wurde inzwischen geändert. Angezeigt ist der aktuelle Stand.', 409)
        except ValueError as error:
            return admin_page(request, str(error), 422)
        ctx.audited(request, 'account_changed', admin_account['id'], f'account:{account_id}',
                    role=changed['role'], disabled=bool(changed['disabled']))
        if account_id == admin_account['id']:
            return RedirectResponse('/', status_code=303)  # own role or lock changed: signed out
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/freigaben', response_class=HTMLResponse)
    async def admin_grant(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        target, account_id = target_of(values.get('target')), number(values.get('account_id'))
        try:
            accounts.grant(store, account_id, values.get('access', ''), **target)
        except ValueError as error:
            return admin_page(request, str(error), 422)
        ctx.audited(request, 'grant_created', admin_account['id'], values.get('target'), account=account_id,
                    access=values.get('access'))
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/freigaben/entfernen', response_class=HTMLResponse)
    async def admin_revoke(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        target, account_id = target_of(values.get('target')), number(values.get('account_id'))
        try:
            accounts.revoke(store, account_id, revision=number(values.get('revision')), **target)
        except Conflict:
            return admin_page(request, 'Die Freigabe wurde inzwischen geändert. Angezeigt ist der aktuelle Stand.', 409)
        ctx.audited(request, 'grant_revoked', admin_account['id'], values.get('target'), account=account_id)
        return RedirectResponse('/verwaltung', status_code=303)
