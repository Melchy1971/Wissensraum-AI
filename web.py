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

import html

import accounts
import services
from core import Conflict

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 50
STATUS_LABELS = {'new': 'neu', 'reviewed': 'geprüft', 'outdated': 'veraltet', 'archived': 'archiviert'}  # app.py
FIELD_LABELS = {'title': 'Titel', 'category': 'Bereich', 'tag': 'Tag', 'status': 'Status', 'owner': 'Verantwortlich',
                'review_date': 'Wiedervorlage', 'relation': 'Beziehung', 'collection': 'Sammlung', 'favorite': 'Favorit'}
METHOD_LABELS = {'fulltext': 'Volltext', 'vector': 'Bedeutung', 'hybrid': 'Volltext und Bedeutung'}
MAX_UPLOAD = 50 * 1024 * 1024  # core.MAX_DOCUMENT_BYTES, per file
MAX_REQUEST = 100 * 1024 * 1024  # all files of one upload together
MAX_FILES = 200
FORM_LIMITS = {'max_files': 1, 'max_fields': 20, 'max_part_size': 64 * 1024}
STATUS_TEXT = {401: 'Bitte zuerst anmelden.', 403: 'Dafür fehlt die Berechtigung.',
               404: 'Nicht gefunden.', 413: 'Die Datei ist zu groß.', 422: 'Die Eingabe ist ungültig.'}
SHOWN = {409, 422, 429, 503}  # our own messages; for 401, 403 and 404 the fixed text (I3)


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
        # Same page for missing and invisible objects (I3); details only for our own messages.
        message = error.detail if error.status_code in SHOWN else STATUS_TEXT.get(error.status_code, 'Fehler.')
        if request.headers.get('hx-request') == 'true':  # into the htmx target, not a whole page
            return HTMLResponse(f'<p class="notice">{html.escape(str(message))}</p>', status_code=error.status_code,
                                headers={'HX-Retarget': '#meldung', 'HX-Reswap': 'innerHTML'})
        return render(request, 'message.html', 'Hinweis', error.status_code, message=message,
                      link='/', link_text='Zur Startseite')

    async def form(request, **limits):
        try:
            return await request.form(**{**FORM_LIMITS, **limits})
        except Exception:  # malformed or over the limits
            raise HTTPException(422, 'Das Formular konnte nicht gelesen werden.') from None

    def number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise HTTPException(422, 'Ungültige Angabe.') from None

    @app.get('/', response_class=HTMLResponse)
    def start(request: Request, seite: int = 1, tag: str = ''):
        if request.state.account is None:
            return render(request, 'message.html', 'Wissensraum', message='Bitte melde dich an.',
                          link='/login', link_text='Anmelden')
        scoped, tag = ctx.viewer(request), tag.strip()[:100]
        rows = scoped.list(tag=tag or None)
        page = max(1, seite)
        return render(request, 'documents.html', 'Dokumente', documents=rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
                      total=len(rows), page=page, more=len(rows) > page * PAGE_SIZE, tag=tag,
                      tags=scoped.visible_tags(), labels=STATUS_LABELS)

    @app.get('/suche', response_class=HTMLResponse)
    def search(request: Request, q: str = '', tag: str = ''):
        from retrieval import Retriever, snippet
        scoped, q, tag = ctx.viewer(request), q.strip()[:500], tag.strip()[:100]
        results, search_status = [], ''
        if q:
            ctx.limited(request, 'search')
            found = Retriever(scoped).search(q, limit=20, tag=tag or None)
            results = [{'document_id': r['document_id'], 'title': r['title'], 'ordinal': r['ordinal'],
                        'snippet': snippet(r['content'], q), 'method': METHOD_LABELS.get(r.get('retrieval_method'), '')}
                       for r in found.results]
            search_status = found.status
        return render(request, 'search.html', 'Suche', q=q, results=results, search_status=search_status, tag=tag,
                      tags=scoped.visible_tags())

    @app.get('/dokument/{doc_id}', response_class=HTMLResponse)
    def document(request: Request, doc_id: int):
        scoped = ctx.viewer(request)
        doc = ctx.document(scoped, doc_id)
        return render(request, 'document.html', doc['title'], **document_view(scoped, doc))

    def document_view(scoped, doc, notice=''):
        account = scoped.viewer
        # The history also logs collections, relations and favorites. Names of collections and ids of related
        # documents may be invisible to the reader, favorites are another account's action: administrators see
        # collections and relations, nobody sees favorite entries on the server.
        hidden = {'favorite'} if account['role'] == 'admin' else {'favorite', 'collection', 'relation'}
        history = [{**h, 'label': FIELD_LABELS.get(h['field'], h['field']),
                    'old_value': STATUS_LABELS.get(h['old_value'], h['old_value']) if h['field'] == 'status' else h['old_value'],
                    'new_value': STATUS_LABELS.get(h['new_value'], h['new_value']) if h['field'] == 'status' else h['new_value']}
                   for h in scoped.metadata_history(doc['id']) if h['field'] not in hidden]
        return {'doc': doc, 'collections': scoped.document_collections(doc['id']), 'tags': scoped.document_tags(doc['id']),
                'history': history, 'labels': STATUS_LABELS, 'notice': notice,
                'writable': accounts.can_write(store, account, doc['id']),
                'deletable': accounts.can_delete(store, account, doc['id'])}

    @app.post('/dokument/{doc_id}/metadaten', response_class=HTMLResponse)
    async def metadata(request: Request, doc_id: int):
        account = ctx.signed_in(request)
        ctx.document(ctx.viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_write(store, account, doc_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        values = await form(request)
        fields = {key: values.get(key) for key in ('title', 'category', 'status', 'owner', 'review_date', 'tags')
                  if values.get(key) is not None}
        status, notice = 200, 'Gespeichert.'
        try:
            services.change_document(store, ctx.recorder(request), account, doc_id, number(values.get('revision')),
                                     **fields)
        except Conflict:
            status, notice = 409, ('Jemand hat das Dokument inzwischen geändert. Angezeigt ist der aktuelle Stand; '
                                   'bitte die Änderung erneut eintragen.')
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return render(request, 'metadata_result.html', '', status, **document_view(ctx.viewer(request),
                                                                                    store.get(doc_id), notice))

    @app.post('/dokument/{doc_id}/loeschen', response_class=HTMLResponse)
    async def delete(request: Request, doc_id: int):
        account = ctx.signed_in(request)
        ctx.document(ctx.viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_delete(store, account, doc_id):
            raise HTTPException(403, 'Löschen dürfen nur der Besitzer des Dokuments und Administratoren.')
        values = await form(request)
        try:
            services.delete_document(store, ctx.recorder(request), account, doc_id, number(values.get('revision')))
        except Conflict:
            raise HTTPException(409, 'Jemand hat das Dokument inzwischen geändert. Bitte die Seite neu laden.') from None
        response = HTMLResponse('<p>Gelöscht.</p>')
        response.headers['HX-Redirect'] = '/'
        return response

    @app.get('/wiedervorlagen', response_class=HTMLResponse)
    def reviews(request: Request):
        ctx.signed_in(request)
        return render(request, 'reviews.html', 'Fällige Wiedervorlagen', rows=ctx.viewer(request).due_reviews(),
                      labels=STATUS_LABELS)

    @app.get('/notiz', response_class=HTMLResponse)
    def note_page(request: Request):
        ctx.require_role(request, 'admin', 'editor')
        return render(request, 'note.html', 'Notiz erfassen')

    @app.post('/notiz', response_class=HTMLResponse)
    async def note(request: Request):
        account = ctx.require_role(request, 'admin', 'editor')
        ctx.limited(request, 'upload')
        values = await form(request, max_part_size=1024 * 1024)
        title, content = ' '.join((values.get('title') or '').split()), (values.get('content') or '').strip()
        if not title or not content:
            raise HTTPException(422, 'Titel und Text angeben.')
        try:
            doc_id = services.create_document(store, ctx.recorder(request), account, title[:300], content,
                                              (values.get('category') or 'Allgemein').strip()[:100] or 'Allgemein')
        except services.Duplicate as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        response = HTMLResponse('<p>Gespeichert.</p>', 201)
        response.headers['HX-Redirect'] = f'/dokument/{doc_id}'
        return response

    @app.get('/hochladen', response_class=HTMLResponse)
    def upload_page(request: Request):
        ctx.require_role(request, 'admin', 'editor')
        return render(request, 'upload.html', 'Hochladen')

    @app.post('/hochladen', response_class=HTMLResponse)
    async def upload(request: Request):
        """Several files or ZIP archives become one import job; the worker imports them (roadmap phase 3)."""
        from import_jobs import ImportJobs
        account = ctx.require_role(request, 'admin', 'editor')
        ctx.limited(request, 'upload')
        if int(request.headers.get('content-length') or 0) > MAX_REQUEST + 1024 * 1024:
            raise HTTPException(413, 'Die Dateien sind zusammen zu groß (höchstens 100 MB je Upload).')
        values = await form(request, max_files=MAX_FILES)
        files, total = [], 0
        try:
            for upload in values.getlist('file'):
                if not hasattr(upload, 'read'):
                    continue
                data = await upload.read(MAX_UPLOAD + 1)
                if len(data) > MAX_UPLOAD:
                    raise HTTPException(413, f'Die Datei {upload.filename} ist zu groß (höchstens 50 MB).')
                total += len(data)
                if total > MAX_REQUEST:
                    raise HTTPException(413, 'Die Dateien sind zusammen zu groß (höchstens 100 MB je Upload).')
                files.append((upload.filename or 'Datei', data))
        finally:
            await values.close()
        if not files:
            raise HTTPException(422, 'Bitte mindestens eine Datei auswählen.')
        category = (values.get('category') or 'Allgemein').strip()[:100] or 'Allgemein'
        try:
            with store.transaction():
                job_id = ImportJobs(store).create_uploads(account['id'], files, category)
                ctx.audited(request, 'import_started', account['id'], f'import:{job_id}', files=len(files))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        response = HTMLResponse(f'<p>Auftrag angelegt. <a href="/importe/{job_id}">Zum Fortschritt</a></p>', 202)
        response.headers['HX-Redirect'] = f'/importe/{job_id}'
        return response

    def own_job(request, job_id):
        """The job if the signed-in account may see it (its own, or any for administrators), else 404."""
        from import_jobs import UPLOAD, ImportJobs
        account = ctx.require_role(request, 'admin', 'editor')
        jobs = ImportJobs(store)
        try:
            job = jobs.job(job_id)
        except ValueError:
            raise HTTPException(404, 'Nicht gefunden.') from None
        if job['kind'] != UPLOAD or (account['role'] != 'admin' and job['account_id'] != account['id']):
            raise HTTPException(404, 'Nicht gefunden.')
        return jobs, job

    def job_view(jobs, job):
        from import_jobs import REASON_LABELS, STATUS_LABELS
        summary = jobs.summary(job['id'])
        items = [{**item, 'status_text': STATUS_LABELS[item['status']],
                  'reason_text': REASON_LABELS.get(item['reason'], item['reason'])} for item in jobs.items(job['id'])]
        return {'job': job, 'summary': summary, 'items': items, 'open': job['status'] in ('pending', 'running'),
                'labels': STATUS_LABELS}

    @app.get('/importe', response_class=HTMLResponse)
    def imports(request: Request):
        from import_jobs import STATUS_LABELS, ImportJobs
        account = ctx.require_role(request, 'admin', 'editor')
        rows = ImportJobs(store).upload_jobs(None if account['role'] == 'admin' else account['id'])
        return render(request, 'imports.html', 'Importe', jobs=rows, labels=STATUS_LABELS)

    @app.get('/importe/{job_id}', response_class=HTMLResponse)
    def import_page(request: Request, job_id: int):
        jobs, job = own_job(request, job_id)
        return render(request, 'import.html', f'Import {job_id}', **job_view(jobs, job))

    @app.get('/importe/{job_id}/stand', response_class=HTMLResponse)
    def import_state(request: Request, job_id: int):
        jobs, job = own_job(request, job_id)
        return render(request, 'import_state.html', '', **job_view(jobs, job))

    @app.post('/importe/{job_id}/{action}', response_class=HTMLResponse)
    def import_action(request: Request, job_id: int, action: str):
        jobs, job = own_job(request, job_id)
        status = {'abbrechen': 'cancelled', 'fortsetzen': 'pending'}.get(action)
        if status is None:
            raise HTTPException(404, 'Nicht gefunden.')
        if not jobs.set_status(job_id, status):
            raise HTTPException(409, 'Der Auftrag ist abgeschlossen oder bereits in diesem Zustand.')
        return RedirectResponse(f'/importe/{job_id}', status_code=303)

    @app.get('/importe/{job_id}/bericht')
    def import_report(request: Request, job_id: int):
        import tempfile
        from fastapi.responses import Response
        jobs, job = own_job(request, job_id)
        with tempfile.TemporaryDirectory() as folder:
            data = jobs.write_report(job_id, Path(folder) / 'bericht.csv').read_bytes()
        return Response(data, media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="import-{job_id}.csv"'})

    @app.get('/fragen', response_class=HTMLResponse)
    def questions(request: Request):
        from core import run_sources
        history = []
        for run in ctx.viewer(request).history():
            try:
                cited, _ = run_sources(run.get('sources_json') or '[]')
            except (ValueError, AttributeError):
                cited = []
            # Titles and numbers as recorded with the answer; the link answers 404 if access was revoked since.
            sources = [s for s in cited if isinstance(s, dict) and s.get('document_id') is not None]
            history.append({**run, 'sources': sources})
        return render(request, 'questions.html', 'Fragen', history=history)

    @app.post('/fragen', response_class=HTMLResponse)
    async def ask(request: Request):
        scoped = ctx.viewer(request)
        question = (await form(request)).get('question', '').strip()
        if not 0 < len(question) <= 4000:
            raise HTTPException(422, 'Bitte eine Frage mit bis zu 4000 Zeichen eingeben.')
        if ctx.ai is None:
            raise HTTPException(503, 'Für den Server ist kein KI-Anbieter eingerichtet.')
        ctx.limited(request, 'question')
        try:
            result = services.ask(scoped, ctx.ai, ctx.recorder(request), request.state.account, question)
        except services.Unavailable as error:
            raise HTTPException(503, str(error)) from None
        from evidence import INSUFFICIENT, UNAVAILABLE
        sources = [s for s in result.sources if s.get('document_id') is not None]
        # Without an answer the hits are search hints, never sources (as on the desktop, step 7.4).
        return render(request, 'answer.html', '', answer=result.text, sources=sources, label=result.label,
                      hints=result.state in (INSUFFICIENT, UNAVAILABLE))

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
        from vector_sync import VectorSync
        sync = VectorSync(store)
        vectors = {**sync.config(), 'progress': sync.progress()}
        return render(request, 'admin.html', 'Verwaltung', status, notice=notice, vectors=vectors,
                      exclude_retired=store.exclude_retired(),
                      accounts=accounts.accounts(store),
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
            services.create_account(store, ctx.recorder(request), admin_account, values.get('tenant_id', ''),
                                    values.get('object_id', ''), values.get('role', ''), values.get('display_name', ''))
        except ValueError as error:
            return admin_page(request, str(error), 422)
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/konten/{account_id}', response_class=HTMLResponse)
    async def admin_change_account(request: Request, account_id: int):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        disabled = values.get('disabled') == '1'
        try:
            with store.transaction():
                changed = accounts.update_account(store, account_id, number(values.get('version')),
                                                  role=values.get('role'), disabled=disabled)
                ctx.audited(request, 'account_changed', admin_account['id'], f'account:{account_id}',
                            role=changed['role'], disabled=bool(changed['disabled']))
        except Conflict:
            return admin_page(request, 'Das Konto wurde inzwischen geändert. Angezeigt ist der aktuelle Stand.', 409)
        except ValueError as error:
            return admin_page(request, str(error), 422)
        if account_id == admin_account['id']:
            return RedirectResponse('/', status_code=303)  # own role or lock changed: signed out
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/freigaben', response_class=HTMLResponse)
    async def admin_grant(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        target, account_id = target_of(values.get('target')), number(values.get('account_id'))
        try:
            services.grant(store, ctx.recorder(request), admin_account, account_id, values.get('access', ''), **target)
        except ValueError as error:
            return admin_page(request, str(error), 422)
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/veraltete-dokumente', response_class=HTMLResponse)
    async def admin_exclude_retired(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        services.set_exclude_retired(store, ctx.recorder(request), admin_account, values.get('enabled') == '1')
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/semantische-suche', response_class=HTMLResponse)
    async def admin_semantic_search(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        try:
            services.set_semantic_search(store, ctx.recorder(request), admin_account, values.get('enabled') == '1')
        except ValueError as error:  # Ollama or the embedding model missing: nothing changed
            return admin_page(request, str(error), 422)
        return RedirectResponse('/verwaltung', status_code=303)

    @app.post('/verwaltung/freigaben/entfernen', response_class=HTMLResponse)
    async def admin_revoke(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        target, account_id = target_of(values.get('target')), number(values.get('account_id'))
        try:
            with store.transaction():
                accounts.revoke(store, account_id, revision=number(values.get('revision')), **target)
                ctx.audited(request, 'grant_revoked', admin_account['id'], values.get('target'), account=account_id)
        except Conflict:
            return admin_page(request, 'Die Freigabe wurde inzwischen geändert. Angezeigt ist der aktuelle Stand.', 409)
        return RedirectResponse('/verwaltung', status_code=303)
