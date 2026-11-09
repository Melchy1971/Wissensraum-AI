"""Rights everywhere (Release 3.0, step 10.4): negative tests I1 to I3 and E1.

A reader, an editor and an administrator work on the same stock. ``Zebrafisch`` occurs only in the
document nobody was given; it must never come back through any read path. All ids are synthetic.
"""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import accounts
import server
from core import AI, Store
from oidc import Provider
from test_server import AUTHORITY, BASE, CLIENT_ID, CLIENT_SECRET, TENANT, TestIdentityProvider

SECRET = 'Zebrafisch'
COMMON = 'Wartungsplan Pumpe'


def fill(store):
    """Stock with one document per way of being visible, plus graph, vectors and a relation."""
    ids = {}
    ids['granted'], _ = store.add('Freigegeben', f'{COMMON} Nordhalle. Ansprechpartner Erik Nord.')
    ids['in_collection'], _ = store.add('Sammlungsdokument', f'{COMMON} Südhalle.')
    ids['secret'], _ = store.add('Geheim', f'{COMMON} {SECRET} Erik Nord arbeitet mit Vera Geheim.',
                                 filename='geheim.txt', original=b'Originalbytes geheim')
    ids['old'], _ = store.add('Fassung', 'Fassung eins Kesselhaus.', filename='fassung.txt', versioned=True)
    ids['new'], _ = store.add('Fassung', f'Fassung zwei Kesselhaus {SECRET}.', filename='fassung.txt', versioned=True)
    ids['shared_collection'] = store.create_collection('Projekt Süd')
    store.add_to_collection(ids['shared_collection'], [ids['in_collection']])
    ids['hidden_collection'] = store.create_collection('Projekt Geheim')
    store.add_to_collection(ids['hidden_collection'], [ids['secret'], ids['granted']])
    store.relate(ids['granted'], ids['secret'], 'related')
    person = next(t['id'] for t in store.entity_types() if t['name'] == 'Person')
    works = store.relation_types()[0]['id']
    ids['erik'] = store.create_entity(person, 'Erik Nord', ids['granted'], 'Erik Nord')
    store.add_mention(ids['erik'], ids['secret'], 'Erik Nord')
    ids['vera'] = store.create_entity(person, 'Vera Geheim', ids['secret'], 'Vera Geheim')
    store.relate_entities(ids['erik'], ids['vera'], works, ids['secret'], 'Erik Nord arbeitet mit Vera Geheim')
    with store.connect() as db:
        chunks = {r['document_id']: r['id'] for r in db.execute('SELECT id,document_id FROM chunks')}
    # The secret chunk lies exactly on the query vector, so it would rank first without the filter.
    store.save_vectors('m', {chunks[ids['secret']]: [1.0, 0.0, 0.0], chunks[ids['granted']]: [0.6, 0.8, 0.0],
                             chunks[ids['in_collection']]: [0.0, 1.0, 0.0]})
    return ids


def people(store, ids):
    admin = accounts.create_account(store, TENANT, 'oid-admin', 'admin', 'Admin')
    editor = accounts.create_account(store, TENANT, 'oid-editor', 'editor', 'Bearbeiter')
    reader = accounts.create_account(store, TENANT, 'oid-reader', 'reader', 'Leser')
    for account in (editor, reader):
        accounts.grant(store, account, 'read', document_id=ids['granted'])
        accounts.grant(store, account, 'read', document_id=ids['old'])
        accounts.grant(store, account, 'read', collection_id=ids['shared_collection'])
    return admin, editor, reader


def check_rights_contract(case, store):
    """I1 for every read path of the store; same on both backends, ``store`` freshly migrated."""
    ids = fill(store)
    admin, _, reader = (accounts.account(store, i) for i in people(store, ids))
    seen = store.as_viewer(reader)
    visible = {ids['granted'], ids['in_collection']}

    case.assertEqual({r['id'] for r in seen.list()}, visible)  # current versions only: the old one is replaced
    case.assertEqual({r['id'] for r in seen.list(all_versions=True)}, visible | {ids['old']})
    case.assertEqual({r['id'] for r in store.list()}, visible | {ids['secret'], ids['new']})  # desktop unchanged
    for hidden in (ids['secret'], ids['new'], 999_999):
        with case.assertRaisesRegex(ValueError, 'nicht gefunden'):
            seen.get(hidden)
    case.assertEqual([r['id'] for r in seen.versions(ids['old'])], [ids['old']])
    case.assertEqual(seen.relations(ids['granted']), [])
    case.assertEqual(seen.adjacent_chunks(ids['secret'], 1), [])

    # filter before ordering and limit: the secret hit would be first
    case.assertEqual(seen.search(SECRET, all_versions=True), [])
    case.assertEqual({r['document_id'] for r in seen.search(COMMON, limit=1)} <= visible, True)
    case.assertEqual(len(seen.search(COMMON, limit=1)), 1)
    case.assertEqual(store.search(SECRET, limit=1)[0]['document_id'] in (ids['secret'], ids['new']), True)
    case.assertEqual(store.vector_search('m', [1.0, 0.0, 0.0], limit=1)[0]['document_id'], ids['secret'])
    case.assertEqual([r['document_id'] for r in seen.vector_search('m', [1.0, 0.0, 0.0], limit=1)], [ids['granted']])

    case.assertEqual([c['id'] for c in seen.collections()], [ids['shared_collection']])
    case.assertEqual(seen.document_collections(ids['granted']), [])
    with tempfile.TemporaryDirectory() as folder:
        with case.assertRaisesRegex(ValueError, 'nicht gefunden'):
            seen.export_collection(ids['hidden_collection'], Path(folder) / 'a.zip')
        report = seen.export_collection(ids['shared_collection'], Path(folder) / 'b.zip')
    case.assertEqual([d['id'] for d in report['documents']], [ids['in_collection']])

    case.assertEqual([e['name'] for e in seen.entities()], ['Erik Nord'])
    case.assertEqual(seen.entities()[0]['mentions'], 1)
    erik = seen.entity(ids['erik'])
    case.assertEqual(([m['document_id'] for m in erik['mentions']], erik['relations']), ([ids['granted']], []))
    with case.assertRaisesRegex(ValueError, 'nicht gefunden'):
        seen.entity(ids['vera'])
    graph = seen.graph_data()
    case.assertEqual(([n['name'] for n in graph['nodes']], graph['edges']), (['Erik Nord'], []))
    case.assertEqual(seen.document_graph(ids['secret']), {'mentions': [], 'relations': []})
    case.assertEqual(seen.graph_context([ids['secret'], ids['granted']]), [])
    case.assertEqual(len(store.as_viewer(admin).graph_data()['edges']), 1)

    locked = dict(reader, disabled=1)
    case.assertEqual((store.as_viewer(locked).list(), store.as_viewer(locked).collections()), ([], []))
    case.assertEqual(len(store.as_viewer(admin).list(all_versions=True)), 5)


def answer(payloads):
    def transport(request, timeout):
        payloads.append(request.data.decode('utf-8'))
        return io.BytesIO(json.dumps({'status': 'completed', 'output': [{'content': [
            {'type': 'output_text', 'text': 'Der Wartungsplan gilt für die Pumpe. [Q1]'}]}]}).encode())
    return transport


class RightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_rights_contract(self, self.store)


class EndpointTests(unittest.TestCase):
    """I1 to I3 and E1 per endpoint and role, through sign-in, session and CSRF of step 10.3."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')
        self.ids = fill(self.store)
        self.admin, self.editor, self.reader = people(self.store, self.ids)
        self.payloads = []
        self.idp = TestIdentityProvider()
        provider = Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback', http=self.idp.http)
        self.app = server.create_app(self.store, provider, BASE,
                                     ai=lambda scoped: AI(scoped, 'test-key', transport=answer(self.payloads)))
        self.clients = {oid: self.sign_in(oid) for oid in ('oid-admin', 'oid-editor', 'oid-reader')}

    def tearDown(self):
        self.temp.cleanup()

    def sign_in(self, oid):
        client = TestClient(self.app, base_url=BASE, follow_redirects=False)
        start = client.get('/login')
        code, state = self.idp.authorize(start.headers['location'], oid)
        self.assertEqual(client.get('/auth/callback', params={'code': code, 'state': state}).status_code, 303)
        token = client.get('/').text.split(f'name="{server.CSRF_FIELD}" value="')[1].split('"')[0]
        client.headers.update({server.CSRF_HEADER: token, 'Origin': BASE})
        return client

    @property
    def reader_client(self):
        return self.clients['oid-reader']

    def test_i1_reads_show_only_visible_documents(self):
        client, visible = self.reader_client, {self.ids['granted'], self.ids['in_collection']}
        listed = client.get('/api/documents').json()
        self.assertEqual(({d['id'] for d in listed['documents']}, listed['total']), (visible, 2))
        self.assertEqual(client.get('/api/search', params={'q': SECRET}).json()['results'], [])
        hits = client.get('/api/search', params={'q': COMMON, 'limit': 1}).json()['results']
        self.assertEqual(len(hits), 1)
        self.assertIn(hits[0]['document_id'], visible)
        graph = client.get('/api/graph').json()
        self.assertEqual(([n['name'] for n in graph['nodes']], graph['edges']), (['Erik Nord'], []))
        self.assertEqual([c['id'] for c in client.get('/api/collections').json()], [self.ids['shared_collection']])
        detail = client.get(f'/api/documents/{self.ids["granted"]}').json()
        self.assertEqual((detail['relations'], detail['collections'], detail['writable']), ([], [], False))
        self.assertEqual([m['document_id'] for m in client.get(f'/api/entities/{self.ids["erik"]}').json()['mentions']],
                         [self.ids['granted']])
        export = client.get(f'/api/collections/{self.ids["shared_collection"]}/export')
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            self.assertEqual(len(json.loads(archive.read('manifest.json'))['documents']), 1)
        for path in ('/api/documents', '/api/search?q=Pumpe', '/api/graph', '/api/collections'):
            with self.subTest(path):
                self.assertNotIn(SECRET, client.get(path).text)
        admin_list = self.clients['oid-admin'].get('/api/documents').json()
        self.assertEqual(admin_list['total'], 4)

    def test_i2_ai_context_only_from_visible_sections(self):
        response = self.reader_client.post('/api/questions', json={'question': f'Was gilt für {COMMON}?'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.payloads), 1)
        self.assertNotIn(SECRET, self.payloads[0])
        self.assertNotIn('Vera', self.payloads[0])
        self.assertTrue({s['document_id'] for s in response.json()['sources']}
                        <= {self.ids['granted'], self.ids['in_collection']})
        refused = self.reader_client.post('/api/questions', json={'question': f'Was ist {SECRET}?'})
        self.assertEqual(len(self.payloads), 1)  # nothing visible found: no AI request at all
        self.assertEqual(refused.json()['sources'], [])
        self.clients['oid-admin'].post('/api/questions', json={'question': f'Was ist {SECRET}?'})
        self.assertIn(SECRET, self.payloads[-1])  # the same question finds it for the administrator

    def test_i3_foreign_and_missing_ids_answer_alike(self):
        client = self.reader_client
        for template, foreign, missing in (
                ('/api/documents/{}', self.ids['secret'], 999_999),
                ('/api/documents/{}/original', self.ids['secret'], 999_999),
                ('/api/entities/{}', self.ids['vera'], 999_999),
                ('/api/collections/{}/export', self.ids['hidden_collection'], 999_999),
                ('/api/graph?focus={}', self.ids['vera'], 999_999)):
            with self.subTest(template):
                a, b = client.get(template.format(foreign)), client.get(template.format(missing))
                self.assertEqual((a.status_code, a.text), (404, b.text))
                self.assertEqual(b.status_code, 404)
        a = client.patch(f'/api/documents/{self.ids["secret"]}', json={'title': 'x', 'revision': 1})
        b = client.patch('/api/documents/999999', json={'title': 'x', 'revision': 1})
        self.assertEqual((a.status_code, a.text), (b.status_code, b.text))
        self.assertEqual(a.status_code, 404)
        own = self.clients['oid-admin'].get(f'/api/documents/{self.ids["secret"]}/original')
        self.assertEqual(own.content, b'Originalbytes geheim')

    def test_e1_roles_are_checked_on_the_server(self):
        reader, editor, admin = (self.clients[o] for o in ('oid-reader', 'oid-editor', 'oid-admin'))
        note = {'title': 'Notiz', 'content': 'Neue Notiz zur Lüftung.'}
        self.assertEqual(reader.post('/api/documents', json=note).status_code, 403)
        self.assertEqual(reader.patch(f'/api/documents/{self.ids["granted"]}', json={'title': 'x', 'revision': 1}).status_code, 403)
        self.assertEqual(editor.patch(f'/api/documents/{self.ids["granted"]}', json={'title': 'x', 'revision': 1}).status_code, 403)
        accounts.grant(self.store, self.editor, 'write', document_id=self.ids['granted'])
        editor = self.clients['oid-editor'] = self.sign_in('oid-editor')  # rights change ends the session
        self.assertEqual(editor.patch(f'/api/documents/{self.ids["granted"]}', json={'title': 'Neu', 'revision': 1}).status_code, 200)
        self.assertEqual(self.store.get(self.ids['granted'])['title'], 'Neu')
        accounts.grant(self.store, self.reader, 'write', document_id=self.ids['granted'])
        reader = self.sign_in('oid-reader')
        self.assertEqual(reader.patch(f'/api/documents/{self.ids["granted"]}', json={'title': 'y', 'revision': 1}).status_code, 403)
        created = editor.post('/api/documents', json=note)
        self.assertEqual(created.status_code, 201)
        self.assertIn(created.json()['id'], {d['id'] for d in editor.get('/api/documents').json()['documents']})
        self.assertNotIn(created.json()['id'], {d['id'] for d in reader.get('/api/documents').json()['documents']})
        self.assertEqual(editor.post('/api/documents', json=note).status_code, 409)
        account = {'tenant_id': TENANT, 'object_id': 'oid-neu', 'role': 'reader'}
        grant = {'account_id': self.reader, 'access': 'read', 'document_id': self.ids['secret']}
        for client in (reader, editor):
            self.assertEqual(client.post('/api/accounts', json=account).status_code, 403)
            self.assertEqual(client.post('/api/grants', json=grant).status_code, 403)
        self.assertEqual(admin.post('/api/accounts', json={**account, 'role': 'chef'}).status_code, 422)
        self.assertEqual(admin.post('/api/accounts', json=account).status_code, 201)
        self.assertEqual(admin.post('/api/grants', json=grant).status_code, 201)
        reader = self.sign_in('oid-reader')
        self.assertEqual(reader.get(f'/api/documents/{self.ids["secret"]}').status_code, 200)

    def test_api_needs_a_session_and_csrf(self):
        anonymous = TestClient(self.app, base_url=BASE, follow_redirects=False)
        self.assertEqual(anonymous.get('/api/documents').status_code, 401)
        self.assertEqual(anonymous.get(f'/api/documents/{self.ids["granted"]}').status_code, 401)
        reader = self.reader_client
        del reader.headers[server.CSRF_HEADER]
        self.assertEqual(reader.post('/api/questions', json={'question': 'Pumpe'}).status_code, 403)

    def test_questions_without_ai_provider(self):
        provider = Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback', http=self.idp.http)
        self.app = server.create_app(self.store, provider, BASE)
        client = self.sign_in('oid-reader')
        self.assertEqual(client.post('/api/questions', json={'question': 'Pumpe'}).status_code, 503)


class AiConfigurationTests(unittest.TestCase):
    ENV = {'WR_BASE_URL': 'https://wissen.heimnetz', 'WR_OIDC_TENANT_ID': TENANT, 'WR_OIDC_CLIENT_ID': CLIENT_ID,
           'WR_OIDC_CLIENT_SECRET': CLIENT_SECRET, 'DB_HOST': 'db', 'DB_DATABASE': 'wissensraum',
           'DB_USERNAME': 'wissensraum', 'DB_PASSWORD': 'db-test-kennwort'}

    def test_ai_provider_from_environment(self):
        self.assertIsNone(server.settings_from_env(self.ENV)['ai'])
        settings = server.settings_from_env({**self.ENV, 'WR_AI_PROVIDER': 'ollama', 'WR_AI_MODEL': 'qwen3:8b'})
        built = settings['ai'](Store.__new__(Store))
        self.assertEqual((built.provider, built.model), ('ollama', 'qwen3:8b'))
        with self.assertRaisesRegex(ValueError, 'unbekannt'):
            server.settings_from_env({**self.ENV, 'WR_AI_PROVIDER': 'irgendwer'})
        with self.assertRaisesRegex(ValueError, 'API-Schlüssel'):
            server.settings_from_env({**self.ENV, 'WR_AI_PROVIDER': 'openai'})


if __name__ == '__main__':
    unittest.main()
