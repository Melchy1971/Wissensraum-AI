"""Web pages (Release 3.0, step 10.7): end-to-end tests with two accounts.

Sign-in, documents, search, document view, metadata with revision, upload, questions with sources
and own history, collections and administration of accounts and grants, all through the pages of
``web.py`` with session, CSRF header and Origin like htmx in a browser.
"""
import hashlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import server
import test_rights
from test_rights import COMMON, SECRET

TENANT = '00000000-0000-0000-0000-00000000aaaa'
HTMX_SHA256 = 'd6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717'  # htmx.org 2.0.11


def check_history_contract(case, store):
    """The AI history is separate per account; the desktop (no account) sees all; both backends."""
    one = accounts.account(store, accounts.create_account(store, TENANT, 'oid-h1', 'reader'))
    two = accounts.account(store, accounts.create_account(store, TENANT, 'oid-h2', 'reader'))
    store.as_viewer(one).log('question', 'Frage eins', 'Antwort', {'format': 2, 'sources': []}, 'm')
    store.as_viewer(two).log('question', 'Frage zwei', 'Antwort', {'format': 2, 'sources': []}, 'm')
    store.log('question', 'Frage Desktop', 'Antwort', {'format': 2, 'sources': []}, 'm')
    case.assertEqual([r['question'] for r in store.as_viewer(one).history()], ['Frage eins'])
    case.assertEqual([r['question'] for r in store.as_viewer(two).history()], ['Frage zwei'])
    case.assertEqual([(r['question'], r['account_id']) for r in store.history()],
                     [('Frage Desktop', None), ('Frage zwei', two['id']), ('Frage eins', one['id'])])


class HistoryTests(unittest.TestCase):
    def test_contract(self):
        from core import Store
        with tempfile.TemporaryDirectory() as folder:
            check_history_contract(self, Store(Path(folder) / 'wissen.db'))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.case = test_rights.EndpointTests('test_i1_reads_show_only_visible_documents')
        self.case.setUp()
        self.store, self.ids = self.case.store, self.case.ids
        self.reader = self.case.clients['oid-reader']
        self.editor = self.case.clients['oid-editor']
        self.admin = self.case.clients['oid-admin']

    def tearDown(self):
        self.case.tearDown()

    def form(self, client):
        """Plain form posts carry the token as field; htmx requests send the header (set by sign_in)."""
        return client.headers[server.CSRF_HEADER]

    def test_pages_show_only_what_the_account_may_see(self):
        start = self.reader.get('/')
        self.assertEqual(start.status_code, 200)
        self.assertIn('Freigegeben', start.text)
        self.assertNotIn('Geheim', start.text)
        self.assertIn('Angemeldet als Leser (reader)', start.text)
        self.assertNotIn('/verwaltung', start.text)  # no administration link for a reader
        self.assertNotIn('/hochladen', start.text)
        self.assertIn('Geheim', self.admin.get('/').text)
        found = self.reader.get('/suche', params={'q': COMMON}).text
        self.assertIn(f'/dokument/{self.ids["granted"]}', found)
        hidden = self.reader.get('/suche', params={'q': SECRET}).text
        self.assertIn('Keine Treffer.', hidden)
        self.assertNotIn('/dokument/', hidden.split('<main>')[1])
        page = self.reader.get(f'/dokument/{self.ids["granted"]}').text
        self.assertIn('Nordhalle', page)
        self.assertNotIn('Projekt Geheim', page)  # its invisible collection is not named
        self.assertNotIn('/metadaten', page)  # a reader gets no edit form
        self.assertNotIn('/beziehung"', page)  # nor a relation form
        foreign, missing = self.reader.get(f'/dokument/{self.ids["secret"]}'), self.reader.get('/dokument/999999')
        self.assertEqual((foreign.status_code, foreign.text), (404, missing.text))
        self.assertIn('Projekt Süd', self.reader.get('/sammlungen').text)
        self.assertNotIn('Projekt Geheim', self.reader.get('/sammlungen').text)

    def test_anonymous_visitors_see_only_the_sign_in(self):
        from fastapi.testclient import TestClient
        visitor = TestClient(self.case.app, base_url='https://wissen.test', follow_redirects=False)
        self.assertIn('/login', visitor.get('/').text)
        for path in ('/suche?q=Pumpe', '/fragen', '/sammlungen', f'/dokument/{self.ids["granted"]}', '/verwaltung'):
            with self.subTest(path):
                response = visitor.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('Nordhalle', response.text)

    def test_content_is_escaped_and_pages_need_no_inline_code(self):
        doc, _ = self.store.add('<script>alert(1)</script>', 'Text mit <img src=x onerror=alert(2)> darin.')
        page = self.admin.get(f'/dokument/{doc}').text
        self.assertNotIn('<script>alert', page)
        self.assertNotIn('<img src=x', page)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
        for path in ('/', f'/dokument/{doc}', '/suche', '/fragen', '/sammlungen', '/hochladen', '/verwaltung'):
            with self.subTest(path):
                html = self.admin.get(path).text
                self.assertEqual(re.findall(r'<script(?![^>]*\bsrc=)[^>]*>', html), [])  # only external scripts
                tags = ' '.join(re.findall(r'<[^>]+>', html))  # escaped content cannot form a tag
                self.assertIsNone(re.search(r'\son[a-z]+=|\sstyle=|javascript:', tags))
        response = self.admin.get('/')
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        htmx = self.admin.get('/static/htmx.min.js')
        self.assertEqual(hashlib.sha256(htmx.content).hexdigest(), HTMX_SHA256)
        self.assertEqual(htmx.headers['X-Content-Type-Options'], 'nosniff')

    def test_metadata_form_with_two_editors(self):
        second = accounts.create_account(self.store, TENANT, 'oid-editor-2', 'editor', 'Zweite')
        for account in (self.case.editor, second):
            accounts.grant(self.store, account, 'write', document_id=self.ids['granted'])
        first, other = self.case.sign_in('oid-editor'), self.case.sign_in('oid-editor-2')
        path = f'/dokument/{self.ids["granted"]}'
        self.assertIn('hx-post="/dokument/', first.get(path).text)
        revision = str(self.store.get(self.ids['granted'])['revision'])
        saved = first.post(path + '/metadaten', data={'title': 'Titel Eins', 'category': 'Technik', 'revision': revision})
        self.assertEqual(saved.status_code, 200)
        self.assertIn('Gespeichert.', saved.text)
        stale = other.post(path + '/metadaten', data={'title': 'Titel Zwei', 'category': 'Technik', 'revision': revision})
        self.assertEqual(stale.status_code, 409)
        self.assertIn('value="Titel Eins"', stale.text)  # the current state to decide again
        self.assertEqual(self.store.get(self.ids['granted'])['title'], 'Titel Eins')
        self.assertEqual(self.reader.post(path + '/metadaten', data={'title': 'x', 'revision': '9'}).status_code, 403)
        no_token = self.case.sign_in('oid-editor')
        del no_token.headers[server.CSRF_HEADER]
        self.assertEqual(no_token.post(path + '/metadaten', data={'title': 'x', 'revision': '9'}).status_code, 403)

    def test_upload(self):
        """Parity roadmap phase 3: an upload becomes a job, the worker imports it, the page shows the result."""
        import threading
        import worker
        from import_jobs import ImportJobs
        heartbeat = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'alive'
        files = {'file': ('notiz.txt', 'Kühlwasser prüfen im Kesselhaus.'.encode(), 'text/plain')}
        self.assertEqual(self.reader.post('/hochladen', files=files).status_code, 403)
        self.assertEqual(self.reader.get('/hochladen').status_code, 403)
        self.assertEqual(self.reader.get('/importe').status_code, 403)
        started = self.editor.post('/hochladen', files=files, data={'category': 'Technik'})
        self.assertEqual(started.status_code, 202)
        job_id = int(started.headers['HX-Redirect'].rsplit('/', 1)[1])
        waiting = self.editor.get(f'/importe/{job_id}').text
        self.assertIn('hx-trigger="every 2s"', waiting)  # the page polls while the job is open
        self.assertEqual(worker.import_once(self.store, threading.Event(), heartbeat)['imported'], 1)
        done = self.editor.get(f'/importe/{job_id}/stand').text
        self.assertNotIn('hx-trigger', done)
        self.assertIn('1 importiert', done)
        doc_id = ImportJobs(self.store).items(job_id)[0]['document_id']
        row = self.store.get(doc_id)
        self.assertEqual((row['title'], row['category'], bytes(row['original'])),
                         ('notiz', 'Technik', 'Kühlwasser prüfen im Kesselhaus.'.encode()))
        self.assertIn('Kühlwasser', self.editor.get(f'/dokument/{doc_id}').text)
        self.assertEqual(self.reader.get(f'/dokument/{doc_id}').status_code, 404)
        again = int(self.editor.post('/hochladen', files=files).headers['HX-Redirect'].rsplit('/', 1)[1])
        bad = int(self.editor.post('/hochladen', files={'file': ('programm.exe', b'MZ', 'application/octet-stream')})
                  .headers['HX-Redirect'].rsplit('/', 1)[1])
        worker.import_once(self.store, threading.Event(), heartbeat)
        worker.import_once(self.store, threading.Event(), heartbeat)
        (dup,), (quarantined,) = ImportJobs(self.store).items(again), ImportJobs(self.store).items(bad)
        self.assertEqual((dup['status'], dup['document_id']), ('duplicate', None))  # no access to the existing one
        self.assertEqual((quarantined['status'], quarantined['reason']), ('quarantined', 'unsupported'))
        self.assertIn('Dateityp nicht unterstützt', self.editor.get(f'/importe/{bad}').text)
        large = self.editor.post('/hochladen', files=files, headers={'Content-Length': str(120 * 1024 * 1024)})
        self.assertEqual(large.status_code, 413)
        self.assertEqual(self.editor.post('/hochladen', data={'category': 'Technik'}).status_code, 422)
        with self.store.connect() as db:  # bytes are not kept once an item is decided
            self.assertEqual(db.execute('SELECT count(*) AS n FROM import_items WHERE upload IS NOT NULL')
                             .fetchone()['n'], 0)
        events = [(e['event'], e['source']) for e in audit.entries(self.store, 10)]
        self.assertIn(('document_created', 'worker'), events)
        self.assertIn('import_started', [e for e, _ in events])
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_questions_with_sources_and_own_history(self):
        answer = self.reader.post('/fragen', data={'question': f'Was gilt für {COMMON}?'})
        self.assertEqual(answer.status_code, 200)
        self.assertIn('Der Wartungsplan gilt für die Pumpe.', answer.text)
        self.assertNotIn(SECRET, self.case.payloads[-1])
        linked = {int(i) for i in re.findall(r'href="/dokument/(\d+)"', answer.text)}
        self.assertTrue(linked and linked <= {self.ids['granted'], self.ids['in_collection']})
        self.admin.post('/fragen', data={'question': 'Frage nur der Administration?'})
        mine = self.reader.get('/fragen').text
        self.assertIn(f'Was gilt für {COMMON}?', mine)
        self.assertNotIn('Frage nur der Administration?', mine)  # the history is per account
        theirs = self.admin.get('/fragen').text
        self.assertNotIn(f'Was gilt für {COMMON}?', theirs)
        self.assertEqual({r['account_id'] for r in self.store.history()}, {self.case.reader, self.case.admin})
        self.assertEqual(self.reader.post('/fragen', data={'question': ''}).status_code, 422)

    def test_administration(self):
        self.assertEqual(self.reader.get('/verwaltung').status_code, 403)
        self.assertEqual(self.editor.get('/verwaltung').status_code, 403)
        token = self.form(self.admin)
        self.assertEqual(self.editor.post('/verwaltung/konten', data={
            'csrf_token': self.form(self.editor), 'tenant_id': TENANT, 'object_id': 'oid-x', 'role': 'admin'}).status_code, 403)
        created = self.admin.post('/verwaltung/konten', data={
            'csrf_token': token, 'tenant_id': TENANT, 'object_id': 'oid-neu', 'display_name': 'Neu', 'role': 'reader'})
        self.assertEqual(created.status_code, 303)
        new = accounts.by_identity(self.store, TENANT, 'oid-neu')
        granted = self.admin.post('/verwaltung/freigaben', data={
            'csrf_token': token, 'account_id': str(new['id']), 'target': f'document:{self.ids["secret"]}', 'access': 'read'})
        self.assertEqual(granted.status_code, 303)
        newcomer = self.case.sign_in('oid-neu')
        self.assertEqual(newcomer.get(f'/dokument/{self.ids["secret"]}').status_code, 200)
        page = self.admin.get('/verwaltung').text
        self.assertIn('Dokument: Geheim', page)
        revision = str(accounts.grants(self.store, new['id'])[0]['revision'])
        self.assertEqual(self.admin.post('/verwaltung/freigaben/entfernen', data={
            'csrf_token': token, 'account_id': str(new['id']), 'target': f'document:{self.ids["secret"]}',
            'revision': '99'}).status_code, 409)
        self.assertEqual(self.admin.post('/verwaltung/freigaben/entfernen', data={
            'csrf_token': token, 'account_id': str(new['id']), 'target': f'document:{self.ids["secret"]}',
            'revision': revision}).status_code, 303)
        newcomer = self.case.sign_in('oid-neu')
        self.assertEqual(newcomer.get(f'/dokument/{self.ids["secret"]}').status_code, 404)
        locked = self.admin.post(f'/verwaltung/konten/{new["id"]}', data={
            'csrf_token': token, 'version': str(new['version']), 'role': 'reader', 'disabled': '1'})
        self.assertEqual(locked.status_code, 303)
        self.assertNotIn('Angemeldet als', newcomer.get('/').text)  # locking ended its session
        stale = self.admin.post(f'/verwaltung/konten/{new["id"]}', data={
            'csrf_token': token, 'version': str(new['version']), 'role': 'editor'})
        self.assertEqual(stale.status_code, 409)
        own = accounts.account(self.store, self.case.admin)
        refused = self.admin.post(f'/verwaltung/konten/{own["id"]}', data={
            'csrf_token': token, 'version': str(own['version']), 'role': 'reader'})
        self.assertEqual(refused.status_code, 422)  # the last active administrator stays
        events = [e['event'] for e in reversed(audit.entries(self.store, 100))]
        for event in ('account_created', 'grant_created', 'grant_revoked', 'account_changed'):
            self.assertIn(event, events)
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_api_errors_stay_json(self):
        response = self.reader.get(f'/api/documents/{self.ids["secret"]}')
        self.assertEqual((response.status_code, response.json()), (404, {'detail': 'Nicht gefunden.'}))


if __name__ == '__main__':
    unittest.main()
