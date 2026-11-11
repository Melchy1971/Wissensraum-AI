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
import audit
import services
from core import Conflict


def score_text(hit):
    """RRF score with the ranks in both lists, as on the desktop (B05, ``app.score_text``, D-019)."""
    ranks = ', '.join(f'{name} {hit[key]}' for name, key in (('Volltext', 'fulltext_rank'), ('Vektor', 'vector_rank'))
                      if hit.get(key))
    return f'{hit["rrf_score"]:.4f} ({ranks})' if 'rrf_score' in hit else ranks

ROOT = Path(__file__).resolve().parent
THEME_COOKIE = 'wr_theme'
THEMES = {'auto': 'wie System', 'light': 'hell', 'dark': 'dunkel'}
LOGO_SETTING = 'branding.logo'
MAIL_COOKIE = '__Host-wr_mail_connect'
MAIL_CALLBACK = '/postfaecher/microsoft/rueckruf'
PAGE_SIZE = 50
STATUS_LABELS = {'new': 'neu', 'reviewed': 'geprüft', 'outdated': 'veraltet', 'archived': 'archiviert'}  # app.py
FIELD_LABELS = {'title': 'Titel', 'category': 'Bereich', 'tag': 'Tag', 'status': 'Status', 'owner': 'Verantwortlich',
                'review_date': 'Wiedervorlage', 'relation': 'Beziehung', 'collection': 'Sammlung', 'favorite': 'Favorit'}
RELATION_LABELS = {('references', 'out'): 'verweist auf', ('references', 'in'): 'wird verwiesen von',
                   ('supersedes', 'out'): 'ersetzt', ('supersedes', 'in'): 'ersetzt durch',
                   ('related', 'both'): 'gehört zu'}  # app.py
RELATION_CHOICES = {'references': 'verweist auf', 'supersedes': 'ersetzt', 'related': 'gehört zu'}
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
        theme = request.cookies.get(THEME_COOKIE)
        return templates.TemplateResponse(request, name, {
            'title': title, 'account': account, 'csrf': session['csrf_token'] if session else '',
            'theme': theme if theme in THEMES else 'auto', 'themes': THEMES, 'has_logo': logo() is not None,
            'path': request.url.path, **values}, status_code=status)

    def logo():
        """PNG bytes of the installation's logo (H03), normalized on upload, or None."""
        import base64
        with store.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE name=?', (LOGO_SETTING,)).fetchone()
        return base64.b64decode(row['value']) if row else None

    @app.get('/branding/logo.png')
    def logo_file():
        from fastapi.responses import Response
        data = logo()
        if data is None:
            raise HTTPException(404, 'Nicht gefunden.')
        return Response(data, media_type='image/png')

    @app.post('/darstellung', response_class=HTMLResponse)
    async def appearance(request: Request):
        """Light, dark or the system setting (H02); a cookie of this browser, no account setting."""
        ctx.signed_in(request)
        values = await form(request)
        theme = values.get('theme', 'auto')
        if theme not in THEMES:
            raise HTTPException(422, 'Darstellung: auto, light oder dark.')
        back = values.get('back', '/')
        back = back if back.startswith('/') and not back.startswith('//') else '/'
        response = RedirectResponse(back, status_code=303)
        if theme == 'auto':
            response.delete_cookie(THEME_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        else:
            response.set_cookie(THEME_COOKIE, theme, max_age=365 * 86400, path='/', secure=True, httponly=True,
                                samesite='lax')
        return response

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
                      link='/', link_text='Zur Startseite', path='/')  # path='/': same page for every id (I3)

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
    def start(request: Request, seite: int = 1, tag: str = '', favoriten: int = 0):
        if request.state.account is None:
            return render(request, 'message.html', 'Wissensraum', message='Bitte melde dich an.',
                          link='/login', link_text='Anmelden')
        scoped, tag = ctx.viewer(request), tag.strip()[:100]
        rows = scoped.list(tag=tag or None, favorites=bool(favoriten))
        page = max(1, seite)
        return render(request, 'documents.html', 'Dokumente', documents=rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
                      total=len(rows), page=page, more=len(rows) > page * PAGE_SIZE, tag=tag,
                      tags=scoped.visible_tags(), labels=STATUS_LABELS, favorites=bool(favoriten))

    @app.get('/suche', response_class=HTMLResponse)
    def search(request: Request, q: str = '', tag: str = ''):
        from retrieval import Retriever, snippet
        scoped, q, tag = ctx.viewer(request), q.strip()[:500], tag.strip()[:100]
        results, search_status = [], ''
        if q:
            ctx.limited(request, 'search')
            found = Retriever(scoped).search(q, limit=20, tag=tag or None)
            results = [{'document_id': r['document_id'], 'title': r['title'], 'ordinal': r['ordinal'],
                        'snippet': snippet(r['content'], q), 'method': METHOD_LABELS.get(r.get('retrieval_method'), ''),
                        'score': score_text(r)}
                       for r in found.results]
            search_status = found.status
        return render(request, 'search.html', 'Suche', q=q, results=results, search_status=search_status, tag=tag,
                      tags=scoped.visible_tags(), saved=scoped.saved_searches())

    @app.post('/suche/speichern', response_class=HTMLResponse)
    async def save_search(request: Request):
        ctx.signed_in(request)
        values = await form(request)
        try:  # per account (decision 11.11.2026); the same name replaces the own earlier search
            ctx.viewer(request).save_search(values.get('name', ''), values.get('q', ''), tag=values.get('tag') or None)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/suche', status_code=303)

    @app.post('/suche/gespeichert/{search_id}/loeschen', response_class=HTMLResponse)
    def delete_search(request: Request, search_id: int):
        ctx.signed_in(request)
        try:
            ctx.viewer(request).delete_saved_search(search_id)
        except ValueError:  # another account's search looks like a missing one (I3)
            raise HTTPException(404, 'Nicht gefunden.') from None
        return RedirectResponse('/suche', status_code=303)

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
        writable = accounts.can_write(store, account, doc['id'])
        inside = {c['id'] for c in scoped.document_collections(doc['id'])}
        targets = [c for c in scoped.collections() if c['id'] not in inside
                   and accounts.can_write_collection(store, account, c['id'])] if writable else []
        relations = [{**r, 'label': RELATION_LABELS[(r['kind'], r['direction'])]} for r in scoped.relations(doc['id'])]
        return {'doc': doc, 'collections': scoped.document_collections(doc['id']), 'tags': scoped.document_tags(doc['id']),
                'history': history, 'labels': STATUS_LABELS, 'notice': notice, 'writable': writable,
                'deletable': accounts.can_delete(store, account, doc['id']), 'favorite': scoped.is_favorite(doc['id']),
                'relations': relations, 'relation_choices': RELATION_CHOICES, 'versions': scoped.versions(doc['id']),
                'targets': targets, 'summary_available': ctx.ai is not None, **graph_section(scoped, doc['id'], writable)}

    def graph_section(scoped, doc_id, writable):
        """Mentions and relations resting on the document; forms when the account may write it (phase 7)."""
        import graph_jobs
        section = {'graph': scoped.document_graph(doc_id), 'graph_writable': writable,
                   'suggest_available': ctx.graph_model is not None, 'suggest_state': graph_jobs.state(store, doc_id)}
        if writable:
            section.update(entity_types=store.entity_types(), relation_types=store.relation_types(),
                           known_entities=scoped.entities()[:500])
        return section

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
        server_job = job['kind'] == UPLOAD or job['kind'] in ('imap', 'graph') and job['account_id'] is not None
        if not server_job or (account['role'] != 'admin' and job['account_id'] != account['id']):
            raise HTTPException(404, 'Nicht gefunden.')
        return jobs, job

    def job_view(jobs, job):
        from import_jobs import REASON_LABELS, STATUS_LABELS
        summary = jobs.summary(job['id'])
        items = [{**item, 'status_text': STATUS_LABELS[item['status']],
                  'reason_text': REASON_LABELS.get(item['reason'], item['reason'])} for item in jobs.items(job['id'])]
        kept = sum(1 for item in items if item['kept'] and item['status'] == 'quarantined')
        return {'job': job, 'summary': summary, 'items': items, 'open': job['status'] in ('pending', 'running'),
                'labels': STATUS_LABELS, 'kept': kept}

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
        if action == 'erneut':  # D05: items without readable text whose bytes are still kept (30 days)
            with store.transaction():
                count = jobs.retry_uploads(job_id)
                if count:
                    ctx.audited(request, 'import_started', request.state.account['id'], f'import:{job_id}', retry=count)
            if not count:
                raise HTTPException(409, 'Keine Dateien zum erneuten Verarbeiten vorhanden.')
            return RedirectResponse(f'/importe/{job_id}', status_code=303)
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
        # S-10: answers citing documents the account no longer sees are withheld (decision 11.11.2026).
        return render(request, 'questions.html', 'Fragen', history=ctx.viewer(request).visible_history())

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

    def own_collection(request, collection_id):
        """The collection if visible to the signed-in account, else 404 (I3)."""
        ctx.signed_in(request)
        found = next((c for c in ctx.viewer(request).collections() if c['id'] == collection_id), None)
        if found is None:
            raise HTTPException(404, 'Nicht gefunden.')
        return found

    def collection_writer(request, collection_id):
        collection = own_collection(request, collection_id)
        if not accounts.can_write_collection(store, request.state.account, collection_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        return collection

    @app.post('/sammlungen', response_class=HTMLResponse)
    async def create_collection(request: Request):
        account = ctx.require_role(request, 'admin', 'editor')
        values = await form(request)
        try:
            collection_id = services.create_collection(store, ctx.recorder(request), account, values.get('name', ''))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse(f'/sammlungen/{collection_id}', status_code=303)

    @app.get('/sammlungen/{collection_id}', response_class=HTMLResponse)
    def collection_detail(request: Request, collection_id: int):
        collection = own_collection(request, collection_id)
        account = request.state.account
        return render(request, 'collection.html', collection['name'], collection=collection,
                      documents=ctx.viewer(request).list(collection=collection_id),
                      writable=accounts.can_write_collection(store, account, collection_id),
                      deletable=accounts.can_delete_collection(store, account, collection_id))

    @app.post('/sammlungen/{collection_id}/{action}', response_class=HTMLResponse)
    async def collection_action(request: Request, collection_id: int, action: str):
        if action not in ('umbenennen', 'entfernen', 'loeschen'):
            raise HTTPException(404, 'Nicht gefunden.')
        collection = collection_writer(request, collection_id)
        account, values = request.state.account, await form(request)
        revision = number(values.get('revision'))
        try:
            if action == 'loeschen':
                if not accounts.can_delete_collection(store, account, collection_id):
                    raise HTTPException(403, 'Löschen dürfen nur der Besitzer der Sammlung und Administratoren.')
                services.delete_collection(store, ctx.recorder(request), account, collection_id, revision)
                return RedirectResponse('/sammlungen', status_code=303)
            if action == 'umbenennen':
                services.change_collection(store, ctx.recorder(request), account, collection_id, revision,
                                           name=values.get('name', ''))
            else:
                doc_id = number(values.get('document_id'))
                ctx.document(ctx.viewer(request), doc_id)
                services.change_collection(store, ctx.recorder(request), account, collection_id, revision,
                                           remove=[doc_id])
        except Conflict:
            raise HTTPException(409, f'Die Sammlung {collection["name"]} wurde inzwischen geändert. '
                                     'Bitte die Seite neu laden.') from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse(f'/sammlungen/{collection_id}', status_code=303)

    # --- document actions of phase 4b ------------------------------------------------------------
    def visible_and_writable(request, doc_id):
        account = ctx.signed_in(request)
        ctx.document(ctx.viewer(request), doc_id)  # not visible: 404 before any role answer
        if not accounts.can_write(store, account, doc_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        return account

    @app.post('/dokument/{doc_id}/favorit', response_class=HTMLResponse)
    async def favorite(request: Request, doc_id: int):
        ctx.signed_in(request)
        ctx.document(ctx.viewer(request), doc_id)
        values = await form(request)
        ctx.viewer(request).set_favorite(doc_id, values.get('enabled') == '1')
        return RedirectResponse(f'/dokument/{doc_id}', status_code=303)

    @app.post('/dokument/{doc_id}/sammlung', response_class=HTMLResponse)
    async def add_to_collection(request: Request, doc_id: int):
        account = visible_and_writable(request, doc_id)  # decision 11.11.2026: write access to the document
        values = await form(request)
        collection = collection_writer(request, number(values.get('collection_id')))
        try:
            services.change_collection(store, ctx.recorder(request), account, collection['id'], collection['revision'],
                                       add=[doc_id])
        except Conflict:
            raise HTTPException(409, 'Die Sammlung wurde inzwischen geändert. Bitte erneut versuchen.') from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse(f'/dokument/{doc_id}', status_code=303)

    @app.post('/dokument/{doc_id}/beziehung', response_class=HTMLResponse)
    async def relate(request: Request, doc_id: int):
        account = visible_and_writable(request, doc_id)
        values = await form(request)
        target = number(values.get('target_id'))
        ctx.document(ctx.viewer(request), target)  # only to documents the account sees
        try:
            services.relate(store, ctx.recorder(request), account, doc_id, target, values.get('kind', ''),
                            values.get('note', ''))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse(f'/dokument/{doc_id}', status_code=303)

    @app.post('/dokument/{doc_id}/beziehung/{relation_id}/loeschen', response_class=HTMLResponse)
    def unrelate(request: Request, doc_id: int, relation_id: int):
        account = visible_and_writable(request, doc_id)
        if relation_id not in {r['id'] for r in ctx.viewer(request).relations(doc_id)}:
            raise HTTPException(404, 'Nicht gefunden.')
        services.unrelate(store, ctx.recorder(request), account, doc_id, relation_id)
        return RedirectResponse(f'/dokument/{doc_id}', status_code=303)

    @app.get('/dokument/{doc_id}/vergleich/{other_id}', response_class=HTMLResponse)
    def compare(request: Request, doc_id: int, other_id: int):
        scoped = ctx.viewer(request)
        doc, other = ctx.document(scoped, doc_id), ctx.document(scoped, other_id)
        result = scoped.compare(other_id, doc_id)
        return render(request, 'compare.html', f'Vergleich: {other["title"]} und {doc["title"]}', result=result,
                      doc=doc, other=other)

    @app.post('/dokument/{doc_id}/zusammenfassung', response_class=HTMLResponse)
    def summary(request: Request, doc_id: int):
        scoped = ctx.viewer(request)
        ctx.document(scoped, doc_id)
        if ctx.ai is None:
            raise HTTPException(503, 'Für den Server ist kein KI-Anbieter eingerichtet.')
        ctx.limited(request, 'question')
        try:
            text = services.summarize(scoped, ctx.ai, ctx.recorder(request), request.state.account, doc_id)
        except services.Unavailable as error:
            raise HTTPException(503, str(error)) from None
        except ValueError as error:  # too long for a summary
            raise HTTPException(422, str(error)) from None
        return HTMLResponse(f'<div class="answer"><p>{html.escape(text)}</p></div>')

    # --- mailboxes of phase 8 (D-030) ------------------------------------------------------------
    def mail_tools(request, owner=True):
        """Mailboxes and importer of the signed-in account; 503 without a mail key on the server."""
        from importer import Importer
        from mail_vault import MailKeyMissing, MailVault
        from server_mail import GRAPH_SCOPE, ServerMailboxes
        account = ctx.require_role(request, 'admin', 'editor')  # importing creates documents
        current, previous = ctx.mail_keys
        try:
            vault = MailVault(store, current, previous, account['id'])
        except MailKeyMissing as error:
            raise HTTPException(503, str(error)) from None
        boxes = ServerMailboxes(store, vault, account['id'], refresh=lambda token: ctx.provider.refresh(token, GRAPH_SCOPE))
        return account, boxes, Importer(store, boxes)

    def own_mailbox(boxes, mailbox_id):
        try:
            return boxes.get(mailbox_id)
        except ValueError:  # a foreign mailbox looks like a missing one (I3)
            raise HTTPException(404, 'Nicht gefunden.') from None

    def mail_sync(boxes, importer):
        from import_jobs import ImportJobs
        from mail_sync import MailSync
        return MailSync(store, importer, ImportJobs(store, importer))

    @app.get('/postfaecher', response_class=HTMLResponse)
    def mailboxes_page(request: Request, notice: str = ''):
        from mail_sync import INTERVALS, interval_label
        if ctx.mail_keys[0] is None:
            ctx.require_role(request, 'admin', 'editor')
            return render(request, 'mailboxes.html', 'Postfächer', mailboxes=[], ready=False)
        _, boxes, importer = mail_tools(request)
        sync = mail_sync(boxes, importer)
        rows = [{**box, 'sync': sync.config(box['id'])} for box in boxes.list()]
        return render(request, 'mailboxes.html', 'Postfächer', mailboxes=rows, ready=True,
                      intervals=[(i, interval_label(i)) for i in INTERVALS])

    @app.post('/postfaecher', response_class=HTMLResponse)
    async def mailbox_add(request: Request):
        account, boxes, _ = mail_tools(request)
        values = await form(request)
        try:
            with store.transaction():
                mailbox_id = boxes.save_imap({key: values.get(key, '') for key in
                                              ('provider', 'label', 'email', 'host', 'port', 'username')},
                                             values.get('secret', ''))
                ctx.audited(request, 'mailbox_changed', account['id'], f'mailbox:{mailbox_id}', change='created',
                            provider=values.get('provider', ''))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse(f'/postfaecher/{mailbox_id}', status_code=303)

    @app.get('/postfaecher/microsoft/verbinden')
    def mailbox_connect(request: Request):
        """Consent to Mail.Read for the own mailbox through the server's Entra app (D-030)."""
        import sessions
        from server_mail import GRAPH_SCOPE
        mail_tools(request)
        state, nonce, verifier = sessions.start_login(store, ctx.clock())
        try:
            target = ctx.provider.authorize_url(state, nonce, verifier, 'openid profile ' + GRAPH_SCOPE,
                                                ctx.base + MAIL_CALLBACK)
        except Exception:  # discovery unreachable
            raise HTTPException(503, 'Der Anmeldedienst ist nicht erreichbar.') from None
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(MAIL_COOKIE, state, max_age=sessions.LOGIN_SECONDS, path='/', secure=True, httponly=True,
                            samesite='lax')
        return response

    @app.get(MAIL_CALLBACK)
    def mailbox_connected(request: Request):
        import secrets as random
        import sessions
        from oidc import SignInError
        account, boxes, _ = mail_tools(request)
        params = request.query_params
        state, bound = params.get('state', ''), request.cookies.get(MAIL_COOKIE, '')
        pending = sessions.finish_login(store, state, ctx.clock()) if 'error' not in params else None
        if pending is None or not bound or not random.compare_digest(bound, state):
            raise HTTPException(422, 'Die Verbindung mit Microsoft wurde abgebrochen. Bitte erneut verbinden.')
        nonce_digest, verifier = pending
        try:
            tokens = ctx.provider.redeem_tokens(params.get('code', ''), verifier, ctx.base + MAIL_CALLBACK)
            claims = ctx.provider.verify(tokens['id_token'], nonce_digest)
        except SignInError:
            raise HTTPException(422, 'Microsoft hat die Verbindung nicht bestätigt. Bitte erneut verbinden.') from None
        # Only the own mailbox: the consent must come from the signed-in person (tid and oid), not another one.
        if (claims['tid'], claims['oid']) != (account['tenant_id'], account['object_id']):
            raise HTTPException(422, 'Bitte mit demselben Microsoft-Konto zustimmen, mit dem du angemeldet bist.')
        refresh_token, address = tokens.get('refresh_token'), claims.get('preferred_username') or claims.get('email')
        if not isinstance(refresh_token, str) or not isinstance(address, str):
            raise HTTPException(422, 'Microsoft hat keinen dauerhaften Zugriff erteilt (offline_access fehlt).')
        try:
            with store.transaction():
                mailbox_id = boxes.save_microsoft(address, 'Microsoft 365 · ' + address, refresh_token)
                ctx.audited(request, 'mailbox_changed', account['id'], f'mailbox:{mailbox_id}', change='connected',
                            provider='microsoft')
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        response = RedirectResponse(f'/postfaecher/{mailbox_id}', status_code=303)
        response.delete_cookie(MAIL_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    @app.get('/postfaecher/{mailbox_id}', response_class=HTMLResponse)
    def mailbox_page(request: Request, mailbox_id: str, ordner: str = 'INBOX', anzahl: int = 50, vorschau: int = 0):
        """Headers of the newest messages after an explicit request; no message text, no attachment content."""
        from importer import MailLoginError
        _, boxes, importer = mail_tools(request)
        mailbox = own_mailbox(boxes, mailbox_id)
        folder = ' '.join(ordner.split())[:200] or 'INBOX'
        items, problem = [], ''
        if vorschau:
            ctx.limited(request, 'search')
            try:
                items = importer.preview_account(mailbox_id, folder, max(1, min(anzahl, 200)))
            except (MailLoginError, ValueError) as error:
                problem = str(error)
        return render(request, 'mailbox.html', mailbox['label'], mailbox=mailbox, items=items, folder=folder,
                      count=anzahl, previewed=bool(vorschau), problem=problem,
                      sync=mail_sync(boxes, importer).config(mailbox_id))

    @app.post('/postfaecher/{mailbox_id}/{action}', response_class=HTMLResponse)
    async def mailbox_action(request: Request, mailbox_id: str, action: str):
        from import_jobs import ImportJobs
        from importer import MailLoginError
        account, boxes, importer = mail_tools(request)
        own_mailbox(boxes, mailbox_id)
        values = await form(request)
        if action == 'loeschen':
            with store.transaction():
                boxes.delete(mailbox_id)
                ctx.audited(request, 'mailbox_changed', account['id'], f'mailbox:{mailbox_id}', change='deleted')
            return RedirectResponse('/postfaecher', status_code=303)
        if action == 'import':  # D07, D08: only the chosen messages and released attachments
            ctx.limited(request, 'upload')
            chosen = [v for v in values.getlist('message') if isinstance(v, str)][:500]
            items = [{'id': v, 'subject': ' '.join(str(values.get(f'subject:{v}', '')).split())[:200]} for v in chosen]
            released = {}
            for value in values.getlist('attachment')[:2000]:
                message, _, part = str(value).partition('#')
                if message in chosen and part:
                    released.setdefault(message, []).append(part)
            if not items:
                raise HTTPException(422, 'Bitte mindestens eine Nachricht auswählen.')
            try:
                with store.transaction():
                    job_id = importer.create_mail_job(ImportJobs(store, importer), mailbox_id,
                                                      values.get('folder', 'INBOX'), items, released)
                    ctx.audited(request, 'import_started', account['id'], f'import:{job_id}', files=len(items),
                                mail=True)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            return RedirectResponse(f'/importe/{job_id}', status_code=303)
        if action == 'synchronisation':  # D09: only after the account switched it on
            sync = mail_sync(boxes, importer)
            try:
                if values.get('enabled') == '1':
                    sync.activate(mailbox_id, number(values.get('interval')), values.get('folder', 'INBOX'))
                else:
                    sync.deactivate(mailbox_id)
                ctx.audited(request, 'mailbox_changed', account['id'], f'mailbox:{mailbox_id}',
                            change='sync_on' if values.get('enabled') == '1' else 'sync_off')
            except (MailLoginError, ValueError) as error:
                raise HTTPException(422, str(error)) from None
            return RedirectResponse(f'/postfaecher/{mailbox_id}', status_code=303)
        raise HTTPException(404, 'Nicht gefunden.')

    # --- knowledge graph of phase 7 (decision 11.11.2026) ----------------------------------------
    def graph_fail(error):
        """Our own refusal as 422, a stale revision as 409."""
        if isinstance(error, Conflict):
            return HTTPException(409, 'Der Eintrag wurde inzwischen geändert. Bitte die Seite neu laden.')
        return HTTPException(422, str(error))

    def graph_run(action):
        try:
            return action()
        except (Conflict, ValueError) as error:
            raise graph_fail(error) from None

    def visible_entity(scoped, entity_id):
        try:
            return scoped.entity(entity_id)
        except ValueError:
            raise HTTPException(404, 'Nicht gefunden.') from None

    def visible_item(request, kind, object_id):
        """A mention or relation whose document the account sees, else 404; then the right to change it."""
        account = ctx.signed_in(request)
        try:
            (document_id,) = services.graph_evidence(store, kind, object_id)
        except ValueError:
            raise HTTPException(404, 'Nicht gefunden.') from None
        ctx.document(ctx.viewer(request), document_id)
        if not services.may_change_graph_item(store, account, kind, object_id):
            raise HTTPException(403, 'Keine Berechtigung.')
        return account, document_id

    @app.get('/graph', response_class=HTMLResponse)
    def graph_page(request: Request, q: str = '', typ: str = '', fokus: str = '', vorschlaege: int = 0):
        from graph_layout import TYPE_COLOURS, layout
        scoped = ctx.viewer(request)
        types = store.entity_types()
        chosen = [number(typ)] if typ else None
        focus = number(fokus) if fokus else None
        try:
            data = scoped.graph_data(entity_types=chosen, include_proposed=bool(vorschlaege), query=q.strip()[:100] or None,
                                     focus=focus)
        except ValueError:  # the focus is not visible with these filters
            raise HTTPException(404, 'Nicht gefunden.') from None
        width, height = 900, 560
        positions = layout(data['nodes'], data['edges'], width, height, focus)
        colours = {t['id']: TYPE_COLOURS[i % len(TYPE_COLOURS)] for i, t in enumerate(types)}
        nodes = [{**n, 'x': round(positions[n['id']][0], 1), 'y': round(positions[n['id']][1], 1),
                  'colour': colours.get(n['type_id'], '#6b6b6b')} for n in data['nodes']]
        edges = [{**e, 'x1': round(positions[e['source_id']][0], 1), 'y1': round(positions[e['source_id']][1], 1),
                  'x2': round(positions[e['target_id']][0], 1), 'y2': round(positions[e['target_id']][1], 1)}
                 for e in data['edges']]
        return render(request, 'graph.html', 'Wissensgraph', nodes=nodes, edges=edges, truncated=data['truncated'],
                      types=types, colours=colours, q=q, typ=chosen[0] if chosen else None, focus=focus,
                      proposed=bool(vorschlaege), width=width, height=height,
                      entities=scoped.entities(type_id=chosen[0] if chosen else None, query=q.strip()[:100] or None)[:300])

    @app.get('/graph/entitaet/{entity_id}', response_class=HTMLResponse)
    def entity_page(request: Request, entity_id: int):
        scoped = ctx.viewer(request)
        entity = visible_entity(scoped, entity_id)
        account = request.state.account
        admin = account['role'] == 'admin'
        same_type = [e for e in scoped.entities(type_id=entity['type_id']) if e['id'] != entity_id] if admin else []
        return render(request, 'entity.html', entity['name'], entity=entity, admin=admin, same_type=same_type,
                      changeable=services.may_change_graph_item(store, account, 'entity', entity_id))

    @app.post('/graph/entitaet/{entity_id}/{action}', response_class=HTMLResponse)
    async def entity_action(request: Request, entity_id: int, action: str):
        account = ctx.signed_in(request)
        scoped = ctx.viewer(request)
        visible_entity(scoped, entity_id)  # 404 before any role answer
        values = await form(request)
        record, target = ctx.recorder(request), f'entity:{entity_id}'
        if action in ('bestaetigen', 'loeschen'):
            if not services.may_change_graph_item(store, account, 'entity', entity_id):
                raise HTTPException(403, 'Keine Berechtigung.')
            if action == 'bestaetigen':
                graph_run(lambda: services.graph_confirm(store, record, account, 'entity', entity_id))
                return RedirectResponse(f'/graph/entitaet/{entity_id}', status_code=303)
            graph_run(lambda: services.graph_delete(store, record, account, 'entity', entity_id,
                                                     number(values.get('revision'))))
            return RedirectResponse('/graph', status_code=303)
        if action not in ('umbenennen', 'zusammenfuehren', 'trennen'):
            raise HTTPException(404, 'Nicht gefunden.')
        admin = ctx.require_role(request, 'admin')
        if action == 'umbenennen':
            graph_run(lambda: services.graph_admin(store, record, admin, target, 'renamed', lambda: store.rename_entity(
                entity_id, values.get('name', ''), number(values.get('revision')))))
            return RedirectResponse(f'/graph/entitaet/{entity_id}', status_code=303)
        if action == 'zusammenfuehren':
            other = number(values.get('other_id'))
            visible_entity(scoped, other)
            graph_run(lambda: services.graph_admin(store, record, admin, target, 'merged',
                                                   lambda: store.merge_entities(entity_id, other)))
            return RedirectResponse(f'/graph/entitaet/{entity_id}', status_code=303)
        mentions = [number(v) for v in values.getlist('mention')]
        relations = [number(v) for v in values.getlist('relation')]
        new_id = graph_run(lambda: services.graph_admin(store, record, admin, target, 'split', lambda: store.split_entity(
            entity_id, values.get('name', ''), mentions, relations)))
        return RedirectResponse(f'/graph/entitaet/{new_id}', status_code=303)

    @app.post('/graph/{kind}/{object_id}/{action}', response_class=HTMLResponse)
    async def graph_item_action(request: Request, kind: str, object_id: int, action: str):
        kinds = {'erwaehnung': 'mention', 'relation': 'relation'}
        if kind not in kinds or action not in ('bestaetigen', 'loeschen'):
            raise HTTPException(404, 'Nicht gefunden.')
        account, document_id = visible_item(request, kinds[kind], object_id)
        values = await form(request)
        record = ctx.recorder(request)
        if action == 'bestaetigen':
            graph_run(lambda: services.graph_confirm(store, record, account, kinds[kind], object_id))
        else:
            graph_run(lambda: services.graph_delete(store, record, account, kinds[kind], object_id,
                                                     number(values['revision']) if values.get('revision') else None))
        back = values.get('zurueck', '')
        return RedirectResponse(back if back.startswith('/graph/entitaet/') else f'/dokument/{document_id}', status_code=303)

    @app.post('/dokument/{doc_id}/graph/{action}', response_class=HTMLResponse)
    async def document_graph_action(request: Request, doc_id: int, action: str):
        account = visible_and_writable(request, doc_id)
        scoped, values, record = ctx.viewer(request), await form(request), ctx.recorder(request)
        quote = values.get('quote', '')
        if action == 'entitaet':
            graph_run(lambda: services.graph_create_entity(store, record, account, number(values.get('type_id')),
                                                           values.get('name', ''), doc_id, quote))
        elif action == 'erwaehnung':
            entity_id = number(values.get('entity_id'))
            visible_entity(scoped, entity_id)
            graph_run(lambda: services.graph_add_mention(store, record, account, entity_id, doc_id, quote))
        elif action == 'relation':
            source, target = number(values.get('source_id')), number(values.get('target_id'))
            visible_entity(scoped, source)
            visible_entity(scoped, target)
            graph_run(lambda: services.graph_relate(store, record, account, source, target,
                                                    number(values.get('type_id')), doc_id, quote))
        elif action == 'vorschlaege':
            import graph_jobs
            if ctx.graph_model is None:
                raise HTTPException(503, 'KI-Vorschläge gibt es nur mit dem lokalen Ollama (WR_AI_PROVIDER=ollama).')
            try:
                with store.transaction():
                    graph_jobs.request(store, account['id'], doc_id, ctx.clock())
                    ctx.audited(request, 'graph_changed', account['id'], f'document:{doc_id}', action='suggest_requested')
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
        else:
            raise HTTPException(404, 'Nicht gefunden.')
        return RedirectResponse(f'/dokument/{doc_id}', status_code=303)

    def graph_admin_page(request, notice='', status=200):
        return render(request, 'graph_admin.html', 'Wissensgraph verwalten', status, notice=notice,
                      entity_types=store.entity_types(), relation_types=store.relation_types(),
                      changes=store.graph_changes(50), context=store.graph_context_enabled())

    @app.get('/verwaltung/graph', response_class=HTMLResponse)
    def graph_admin_view(request: Request):
        ctx.require_role(request, 'admin')
        return graph_admin_page(request)

    @app.post('/verwaltung/graph/{action}', response_class=HTMLResponse)
    async def graph_admin_action(request: Request, action: str):
        admin = ctx.require_role(request, 'admin')
        values, record = await form(request), ctx.recorder(request)
        kind = values.get('kind', '')
        if kind not in ('entity', 'relation') and action in ('art', 'art-umbenennen', 'art-loeschen'):
            raise HTTPException(422, 'Art: entity oder relation.')
        adders = {'entity': store.add_entity_type, 'relation': store.add_relation_type}
        renamers = {'entity': store.rename_entity_type, 'relation': store.rename_relation_type}
        deleters = {'entity': store.delete_entity_type, 'relation': store.delete_relation_type}
        try:
            if action == 'art':
                services.graph_admin(store, record, admin, f'{kind}_type', 'type_created',
                                     lambda: adders[kind](values.get('name', '')))
            elif action == 'art-umbenennen':
                type_id = number(values.get('type_id'))
                services.graph_admin(store, record, admin, f'{kind}_type:{type_id}', 'type_renamed',
                                     lambda: renamers[kind](type_id, values.get('name', '')))
            elif action == 'art-loeschen':
                type_id = number(values.get('type_id'))
                services.graph_admin(store, record, admin, f'{kind}_type:{type_id}', 'type_deleted',
                                     lambda: deleters[kind](type_id))
            elif action == 'zuruecknehmen':
                change_id = number(values.get('change_id'))
                services.graph_admin(store, record, admin, f'graph_change:{change_id}', 'reverted',
                                     lambda: store.revert_graph_change(change_id))
            elif action == 'kontext':
                with store.transaction():
                    store.set_graph_context(values.get('enabled') == '1')
                    record('settings_changed', admin['id'], 'settings:graph_context', enabled=values.get('enabled') == '1')
            else:
                raise HTTPException(404, 'Nicht gefunden.')
        except (Conflict, ValueError) as error:
            return graph_admin_page(request, str(error), 409 if isinstance(error, Conflict) else 422)
        return RedirectResponse('/verwaltung/graph', status_code=303)

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
                      secret=secret_notice(),
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

    def secret_notice():
        import datetime
        import server
        return server.secret_warning(ctx.secret_expires, datetime.date.fromtimestamp(ctx.clock()))

    def operations_page(request, check=None, days=30):
        """Overview without content (counts, states, codes), diagnosis and the last integrity check (G01, G02, G07)."""
        import diagnostics
        import operations
        from vector_sync import VectorSync
        health = diagnostics.check_store_health(store)
        sync = VectorSync(store)
        diagnosis = [(health.status, health.message),
                     ('PASS' if ctx.ai is not None else 'WARN',
                      'KI-Anbieter eingerichtet.' if ctx.ai is not None else 'Kein KI-Anbieter eingerichtet, Fragen antworten 503.'),
                     ('PASS' if sync.config().get('enabled') else 'INFO',
                      'Semantische Suche eingeschaltet.' if sync.config().get('enabled') else 'Semantische Suche ausgeschaltet.')]
        warning = secret_notice()
        diagnosis.append(('PASS', 'Client-Geheimnis gültig bis ' + ctx.secret_expires.strftime('%d.%m.%Y') + '.')
                         if warning is None else ('WARN' if warning[0] != 'abgelaufen' else 'FAIL', warning[1]))
        overview = operations.report(operations.overview(store, days)).split('\n')
        return render(request, 'operations.html', 'Betrieb', overview=overview, diagnosis=diagnosis, check=check,
                      days=days)

    @app.get('/verwaltung/betrieb', response_class=HTMLResponse)
    def operations_view(request: Request, tage: int = 30):
        ctx.require_role(request, 'admin')
        return operations_page(request, days=max(1, min(tage, 3650)))

    @app.post('/verwaltung/betrieb/pruefen', response_class=HTMLResponse)
    def operations_check(request: Request):
        """Integrity of schema and document checksums plus the audit chain; reads only."""
        ctx.require_role(request, 'admin')
        import backups
        result = backups.integrity(store)
        chain = audit.verify(store)
        problems = list(result['probleme'])
        if not chain['ok']:
            problems.append(f"Audit-Kette unterbrochen bei Eintrag {chain['id']} ({chain['reason']})")
        check = {'ok': not problems, 'problems': problems, 'documents': result['dokumente'],
                 'entries': chain['entries'], 'head': chain['head'][:16], 'seconds': result['sekunden']}
        return operations_page(request, check)

    def settings_page(request, notice='', status=200):
        """Budgets (C05), retention (E04), import rules (D11) and section lengths (D13)."""
        import budgets
        import retention
        import rules
        from core import DEFAULT_CHUNKING
        from settings import PROVIDERS
        with store.connect() as db:
            rule_rows, preview, runs = rules.list_rules(db), rules.preview(db), rules.runs(db)
        return render(request, 'settings.html', 'Einstellungen', status, notice=notice,
                      budgets=[{**budgets.status(store, key), 'name': value['name']} for key, value in PROVIDERS.items()],
                      retention=[{'key': key, 'label': label, 'days': retention.rules(store)[key]}
                                 for key, (label, _) in retention.CATEGORIES.items()],
                      retention_preview=retention.report(retention.preview(store)),
                      default_chunking=DEFAULT_CHUNKING, chunking=store.chunk_strategies(),
                      rules=rule_rows, rule_preview=preview, rule_runs=runs[:20], sources=rules.SOURCE_LABELS)

    def settings_action(request, action):
        """Run ``action`` and show the page again; a refused value answers 422 with its message."""
        try:
            notice = action() or 'Gespeichert.'
        except ValueError as error:
            return settings_page(request, str(error), 422)
        return settings_page(request, notice)

    @app.get('/verwaltung/einstellungen', response_class=HTMLResponse)
    def settings_view(request: Request):
        ctx.require_role(request, 'admin')
        return settings_page(request)

    @app.post('/verwaltung/einstellungen/logo', response_class=HTMLResponse)
    async def settings_logo(request: Request):
        """Logo of the installation (H03): normalized to a fresh PNG, kept in the settings, with audit."""
        import base64
        import design
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request, max_part_size=6 * 1024 * 1024)
        upload, remove = values.get('logo'), values.get('remove') == '1'

        async def change():
            data = None
            if not remove:
                if not hasattr(upload, 'read'):
                    raise ValueError('Bitte eine Bilddatei auswählen.')
                data = design.normalize_logo(await upload.read(design.DesignSettings.MAX_LOGO_BYTES + 1))
            with store.transaction():
                with store.connect() as db:
                    db.execute('DELETE FROM settings WHERE name=?', (LOGO_SETTING,))
                    if data is not None:
                        db.execute('INSERT INTO settings(name,value) VALUES(?,?)',
                                   (LOGO_SETTING, base64.b64encode(data).decode('ascii')))
                ctx.audited(request, 'settings_changed', admin_account['id'], 'settings:logo', removed=remove)
        try:
            await change()
        except ValueError as error:
            return settings_page(request, str(error), 422)
        finally:
            await values.close()
        return settings_page(request, 'Logo entfernt.' if remove else 'Logo gespeichert.')

    @app.post('/verwaltung/einstellungen/budget', response_class=HTMLResponse)
    async def settings_budget(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        def change():
            fields = {}
            for field in ('tokens', 'eur', 'price_in', 'price_out'):
                raw = (values.get(field) or '').strip().replace(',', '.')
                if raw:
                    try:
                        fields[field] = float(raw)
                    except ValueError:
                        raise ValueError('Budget und Preise als Zahl angeben.') from None
            services.set_budget(store, ctx.recorder(request), admin_account, values.get('provider', ''), **fields)
        return settings_action(request, change)

    @app.post('/verwaltung/einstellungen/aufbewahrung', response_class=HTMLResponse)
    async def settings_retention(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        return settings_action(request, lambda: services.set_retention(
            store, ctx.recorder(request), admin_account, values.get('category', ''), number(values.get('days'))) and None)

    @app.post('/verwaltung/einstellungen/aufbewahrung/anwenden', response_class=HTMLResponse)
    async def settings_retention_apply(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        import retention
        return settings_action(request, lambda: 'Entfernt: ' + retention.report(
            services.apply_retention(store, ctx.recorder(request), admin_account)))

    @app.post('/verwaltung/einstellungen/abschnitte', response_class=HTMLResponse)
    async def settings_chunking(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        reset = values.get('reset') == '1'
        def change():
            size = None if reset else number(values.get('size'))
            changed = services.set_chunk_strategy(store, ctx.recorder(request), admin_account, values.get('type', ''),
                                                  size, None if reset else number(values.get('overlap') or '0'))
            return f'Gespeichert. {changed} Dokumente neu zerlegt.'
        return settings_action(request, change)

    @app.post('/verwaltung/einstellungen/regeln', response_class=HTMLResponse)
    async def settings_rule(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        data = {key: values.get(key, '') for key in ('name', 'source_type', 'path_pattern', 'sender_domain',
                                                     'extension', 'keyword', 'category', 'tags')}
        data['enabled'] = values.get('enabled') == '1'
        rule_id = number(values.get('rule_id')) if values.get('rule_id') else None
        return settings_action(request, lambda: services.save_rule(store, ctx.recorder(request), admin_account,
                                                                   data, rule_id) and None)

    @app.post('/verwaltung/einstellungen/regeln/{rule_id}/{action}', response_class=HTMLResponse)
    def settings_rule_action(request: Request, rule_id: int, action: str):
        admin_account = ctx.require_role(request, 'admin')
        actions = {'loeschen': 'delete', 'hoch': 'up', 'runter': 'down'}
        if action not in actions:
            raise HTTPException(404, 'Nicht gefunden.')
        return settings_action(request, lambda: services.change_rule(store, ctx.recorder(request), admin_account,
                                                                     rule_id, actions[action]))

    @app.post('/verwaltung/einstellungen/regeln-anwenden', response_class=HTMLResponse)
    def settings_rules_apply(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        def run():
            result = services.apply_rules(store, ctx.recorder(request), admin_account)
            return f"{result['changed']} Dokumente geändert, {result['skipped']} übersprungen."
        return settings_action(request, run)

    @app.post('/verwaltung/einstellungen/regellauf/{run_id}/zuruecknehmen', response_class=HTMLResponse)
    def settings_rules_revert(request: Request, run_id: int):
        admin_account = ctx.require_role(request, 'admin')
        def run():
            result = services.revert_rules(store, ctx.recorder(request), admin_account, run_id)
            return f"{result['restored']} Dokumente zurückgesetzt, {result['conflicts']} Bereiche inzwischen geändert."
        return settings_action(request, run)

    def privacy_page(request, search='', selected=(), notice='', status=200):
        """Selective erasure (E03) and installation export, administrators only (decision 11.11.2026)."""
        import privacy
        found, impact = [], None
        if search:
            try:
                found = privacy.find(store, search)
            except ValueError as error:
                notice, status = str(error), 422
        chosen = []
        if selected:
            marks = ','.join('?' * len(set(selected)))
            with store.connect() as db:
                rows = [dict(r) for r in db.execute(f'SELECT id,title,category FROM documents WHERE id IN ({marks}) '
                                                    'ORDER BY id', tuple(sorted(set(selected))))]
            chosen = [r['id'] for r in rows]
            known = {d['id'] for d in found}
            found += [r for r in rows if r['id'] not in known]
        if chosen:
            impact = privacy.impact(store, chosen)
        return render(request, 'privacy.html', 'Datenschutz', status, notice=notice, search=search, found=found,
                      chosen=chosen, impact=impact, export=privacy.export_state(store),
                      export_ready=ctx.export_dir is not None, confirmation=services.ERASE_CONFIRMATION)

    @app.get('/verwaltung/datenschutz', response_class=HTMLResponse)
    def privacy_view(request: Request, suche: str = ''):
        ctx.require_role(request, 'admin')
        selected = [number(v) for v in request.query_params.getlist('id')]
        return privacy_page(request, suche.strip()[:200], selected)

    @app.post('/verwaltung/datenschutz/loeschen', response_class=HTMLResponse)
    async def privacy_erase(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        values = await form(request)
        ids = [number(v) for v in values.getlist('id')]
        if not ids:
            raise HTTPException(422, 'Keine Dokumente ausgewählt.')
        try:
            counts = services.erase_documents(store, ctx.recorder(request), admin_account, ids,
                                              values.get('confirmation', ''))
        except ValueError as error:
            return privacy_page(request, values.get('suche', ''), ids, str(error), 422)
        notice = (f"Gelöscht: {counts['dokumente']} Dokumente, {counts['ki_verlauf']} KI-Antworten, "
                  f"{counts['erwaehnungen']} Erwähnungen, {counts['relationen']} Relationen.")
        if counts.get('sicherungen_mit_daten'):
            notice += f" {counts['sicherungen_mit_daten']} Sicherungen enthalten die Daten weiter."
        return privacy_page(request, notice=notice + ' Sicherungen außerhalb des Servers enthalten sie weiter.')

    @app.post('/verwaltung/datenschutz/export', response_class=HTMLResponse)
    def privacy_export(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        if ctx.export_dir is None:
            raise HTTPException(503, 'Kein Exportordner eingerichtet (WR_EXPORT_DIR).')
        try:
            services.request_export(store, ctx.recorder(request), admin_account, ctx.clock())
        except ValueError as error:
            return privacy_page(request, notice=str(error), status=409)
        return privacy_page(request, notice='Export angefordert. Der Worker erstellt ihn im Hintergrund.')

    @app.get('/verwaltung/datenschutz/export/datei')
    def privacy_export_file(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        import privacy
        from fastapi.responses import FileResponse
        path = privacy.export_file(store, ctx.export_dir) if ctx.export_dir else None
        if path is None:
            raise HTTPException(404, 'Nicht gefunden.')
        ctx.audited(request, 'privacy_export', admin_account['id'], 'installation', status='downloaded')
        return FileResponse(path, media_type='application/zip', filename=path.name)

    @app.post('/verwaltung/datenschutz/export/loeschen', response_class=HTMLResponse)
    def privacy_export_discard(request: Request):
        admin_account = ctx.require_role(request, 'admin')
        import privacy
        try:
            with store.transaction():
                privacy.discard_export(store, ctx.export_dir or '.')
                ctx.audited(request, 'privacy_export', admin_account['id'], 'installation', status='discarded')
        except ValueError as error:
            return privacy_page(request, notice=str(error), status=409)
        return privacy_page(request, notice='Export gelöscht.')

    @app.get('/konto/auskunft')
    def disclosure(request: Request):
        """What the server stores about the signed-in account, as JSON (decision 11.11.2026)."""
        account = ctx.signed_in(request)
        import json
        import privacy
        from fastapi.responses import Response
        report = privacy.account_report(store, account)
        ctx.audited(request, 'privacy_export', account['id'], f"account:{account['id']}", status='disclosure')
        return Response(json.dumps(report, ensure_ascii=False, indent=1, default=str), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="auskunft.json"',
                                 'Cache-Control': 'no-store'})

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
