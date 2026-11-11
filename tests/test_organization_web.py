"""Parity roadmap phase 4b: organising documents on the server.

Favorites and saved searches per account (decision 11.11.2026), collections (adding needs write access to
the document, decision 11.11.2026), relations, version comparison and AI summaries. Stock and accounts of
``test_rights``; every check also asks what an account without access sees (I1, I3, E1).
"""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import migrations
import server
import worker
from core import Store
from import_jobs import ImportJobs
import test_rights
from test_rights import AUTHORITY, BASE, CLIENT_ID, CLIENT_SECRET, COMMON, SECRET, TENANT, fill, people


def check_organization_contract(case, store):
    """Account favorites, account searches and inherited grants behave alike on both backends."""
    ids = fill(store)
    admin, editor, reader = people(store, ids)
    as_reader = store.as_viewer(accounts.account(store, reader))
    as_editor = store.as_viewer(accounts.account(store, editor))
    as_reader.set_favorite(ids['granted'])
    as_reader.set_favorite(ids['granted'])  # twice is fine
    case.assertTrue(as_reader.is_favorite(ids['granted']))
    case.assertFalse(as_editor.is_favorite(ids['granted']))
    case.assertEqual([r['id'] for r in as_reader.list(favorites=True)], [ids['granted']])
    case.assertEqual(as_editor.list(favorites=True), [])
    with case.assertRaises(ValueError):
        as_reader.set_favorite(ids['secret'])  # invisible looks like missing (I3)
    as_reader.set_favorite(ids['granted'], False)
    case.assertEqual(as_reader.list(favorites=True), [])
    search = as_reader.save_search('Pumpen', 'Pumpe', tag='Wartung')
    as_editor.save_search('Pumpen', 'Kessel')  # same name, other account: no clash
    case.assertEqual([(s['name'], s['query'], s['tag']) for s in as_reader.saved_searches()],
                     [('Pumpen', 'Pumpe', 'Wartung')])
    with case.assertRaises(ValueError):
        as_editor.delete_saved_search(search)
    as_reader.delete_saved_search(search)
    case.assertEqual(as_reader.saved_searches(), [])
    case.assertEqual(len(as_editor.saved_searches()), 1)
    # Versions: read and write grants go to the new version, owner grants do not.
    accounts.grant(store, editor, 'owner', document_id=ids['old'], end_sessions=False)
    accounts.inherit_grants(store, ids['old'], ids['new'])
    copied = {(g['access']) for g in accounts.grants(store, reader) if g['document_id'] == ids['new']}
    case.assertEqual(copied, {'read'})
    case.assertEqual([g for g in accounts.grants(store, editor) if g['document_id'] == ids['new']], [])  # was owner


class ContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_organization_contract(self, Store(Path(folder) / 'wissen.db'))

    def test_migration_25_gives_old_favorites_to_the_first_administrator(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'wissen.db')
            ids = fill(store)
            admin, editor, _ = people(store, ids)
            second = accounts.create_account(store, TENANT, 'oid-admin-2', 'admin', 'Zweiter Admin')
            with sqlite3.connect(store.path) as db:
                db.execute('INSERT INTO favorites(document_id) VALUES(?)', (ids['secret'],))
                db.execute("INSERT INTO saved_searches(name,normalized,query) VALUES('Alt','alt','Pumpe')")
                for sql in dict(migrations.SQLITE_MIGRATIONS)[25]:
                    if sql.lstrip().upper().startswith('INSERT'):
                        db.execute(sql)
                owners = {row[0] for row in db.execute('SELECT account_id FROM account_favorites')}
                searches = {row[0] for row in db.execute('SELECT account_id FROM account_saved_searches')}
            self.assertEqual((owners, searches), ({min(admin, second)}, {min(admin, second)}))


class OrganizationTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        accounts.grant(self.store, self.editor, 'write', document_id=self.ids['granted'], end_sessions=False)

    def events(self, name):
        return [json.loads(e['details']) | {'target': e['target']} for e in audit.entries(self.store) if e['event'] == name]

    def test_favorites_belong_to_the_account(self):
        reader, editor, doc = self.clients['oid-reader'], self.clients['oid-editor'], self.ids['granted']
        self.assertEqual(reader.post(f'/dokument/{doc}/favorit', data={'enabled': '1'}).status_code, 303)
        self.assertIn('Aus Favoriten entfernen', reader.get(f'/dokument/{doc}').text)
        self.assertIn('Zu Favoriten', editor.get(f'/dokument/{doc}').text)
        self.assertIn(f'/dokument/{doc}"', reader.get('/', params={'favoriten': 1}).text)
        self.assertNotIn(f'/dokument/{doc}"', editor.get('/', params={'favoriten': 1}).text)
        self.assertEqual([d['id'] for d in reader.get('/api/documents', params={'favorites': True}).json()['documents']], [doc])
        self.assertEqual(editor.get('/api/documents', params={'favorites': True}).json()['total'], 0)
        foreign, missing = (reader.post(f'/dokument/{i}/favorit', data={'enabled': '1'}) for i in (self.ids['secret'], 999999))
        self.assertEqual((foreign.status_code, foreign.text), (404, missing.text))
        self.assertNotIn('<td>Favorit</td>', reader.get(f'/dokument/{doc}').text)  # not in the shared history

    def test_saved_searches_belong_to_the_account(self):
        reader, editor = self.clients['oid-reader'], self.clients['oid-editor']
        self.assertEqual(reader.post('/suche/speichern', data={'name': 'Pumpen', 'q': COMMON, 'tag': ''}).status_code, 303)
        self.assertIn('Pumpen', reader.get('/suche').text)
        self.assertNotIn('Pumpen', editor.get('/suche').text)
        search = self.store.as_viewer(accounts.account(self.store, self.reader)).saved_searches()[0]['id']
        self.assertEqual(editor.post(f'/suche/gespeichert/{search}/loeschen').status_code, 404)
        self.assertEqual(reader.post(f'/suche/gespeichert/{search}/loeschen').status_code, 303)
        self.assertNotIn('Pumpen', reader.get('/suche').text)
        self.assertEqual(reader.post('/suche/speichern', data={'name': '', 'q': COMMON}).status_code, 422)

    def test_collections_follow_the_rights(self):
        editor, reader, admin = (self.clients[o] for o in ('oid-editor', 'oid-reader', 'oid-admin'))
        self.assertEqual(reader.post('/sammlungen', data={'name': 'Leserliste'}).status_code, 403)
        created = editor.post('/sammlungen', data={'name': 'Wartung Nord'})
        self.assertEqual(created.status_code, 303)
        own = int(created.headers['location'].rsplit('/', 1)[1])
        self.assertEqual(self.events('collection_changed'), [{'created': True, 'target': f'collection:{own}'}])
        # Adding needs write access to the document (decision 11.11.2026): granted yes, in_collection only read.
        self.assertEqual(editor.post(f'/dokument/{self.ids["granted"]}/sammlung', data={'collection_id': own}).status_code, 303)
        self.assertEqual(editor.post(f'/dokument/{self.ids["in_collection"]}/sammlung', data={'collection_id': own}).status_code, 403)
        self.assertEqual(editor.post(f'/dokument/{self.ids["secret"]}/sammlung', data={'collection_id': own}).status_code, 404)
        api = editor.patch(f'/api/collections/{own}', json={'add_documents': [self.ids['in_collection']], 'revision': 2})
        self.assertEqual(api.status_code, 403)
        page = editor.get(f'/sammlungen/{own}').text
        self.assertIn('Freigegeben', page)
        self.assertIn('Sammlung löschen', page)
        # A collection the editor may only read: no forms, no changes.
        shared = self.ids['shared_collection']
        self.assertNotIn('Umbenennen', editor.get(f'/sammlungen/{shared}').text)
        self.assertEqual(editor.post(f'/sammlungen/{shared}/umbenennen', data={'name': 'X', 'revision': 1}).status_code, 403)
        hidden = self.ids['hidden_collection']
        for path in (f'/sammlungen/{hidden}', f'/sammlungen/{own}'):
            with self.subTest(path):
                self.assertEqual(reader.get(path).status_code, 404)
                self.assertEqual(reader.get(path).text, reader.get('/sammlungen/999999').text)
        revision = self.store.collections()[-1]['revision']
        renamed = editor.post(f'/sammlungen/{own}/umbenennen', data={'name': 'Wartung Süd', 'revision': revision})
        self.assertEqual(renamed.status_code, 303)
        self.assertEqual(editor.post(f'/sammlungen/{own}/umbenennen', data={'name': 'Alt', 'revision': revision}).status_code, 409)
        revision = next(c['revision'] for c in self.store.collections() if c['id'] == own)
        removed = editor.post(f'/sammlungen/{own}/entfernen', data={'document_id': self.ids['granted'], 'revision': revision})
        self.assertEqual(removed.status_code, 303)
        self.assertNotIn('Freigegeben', editor.get(f'/sammlungen/{own}').text)
        # Deleting: owner or administrator only; documents stay.
        accounts.create_account(self.store, TENANT, 'oid-editor-2', 'editor', 'Zweite')
        second_id = next(a['id'] for a in accounts.accounts(self.store) if a['object_id'] == 'oid-editor-2')
        accounts.grant(self.store, second_id, 'write', collection_id=own, end_sessions=False)
        other = self.sign_in('oid-editor-2')
        revision = next(c['revision'] for c in self.store.collections() if c['id'] == own)
        self.assertEqual(other.post(f'/sammlungen/{own}/loeschen', data={'revision': revision}).status_code, 403)
        self.assertEqual(admin.post(f'/sammlungen/{own}/loeschen', data={'revision': revision}).status_code, 303)
        self.assertNotIn(own, [c['id'] for c in self.store.collections()])
        self.assertIsNotNone(self.store.get(self.ids['granted']))

    def test_relations_only_between_visible_documents(self):
        editor, reader, doc = self.clients['oid-editor'], self.clients['oid-reader'], self.ids['granted']
        page = editor.get(f'/dokument/{doc}').text
        self.assertNotIn('Geheim', page.split('<h2>Beziehungen</h2>')[1])  # the relation to the secret stays hidden
        hidden = next(r['id'] for r in self.store.relations(doc))
        self.assertEqual(editor.post(f'/dokument/{doc}/beziehung/{hidden}/loeschen').status_code, 404)
        self.assertEqual(len(self.store.relations(doc)), 1)
        bad = editor.post(f'/dokument/{doc}/beziehung', data={'target_id': self.ids['secret'], 'kind': 'related'})
        self.assertEqual(bad.status_code, 404)
        self.assertEqual(editor.post(f'/dokument/{doc}/beziehung', data={'target_id': self.ids['old'], 'kind': 'quatsch'}).status_code, 422)
        made = editor.post(f'/dokument/{doc}/beziehung', data={'target_id': self.ids['old'], 'kind': 'references', 'note': 'Plan'})
        self.assertEqual(made.status_code, 303)
        self.assertIn('verweist auf', editor.get(f'/dokument/{doc}').text)
        self.assertIn('Fassung', reader.get(f'/dokument/{doc}').text.split('<h2>Beziehungen</h2>')[1])
        self.assertEqual(reader.post(f'/dokument/{doc}/beziehung', data={'target_id': self.ids['old'], 'kind': 'related'}).status_code, 403)
        new = next(r['id'] for r in self.store.relations(doc) if r['other_id'] == self.ids['old'])
        self.assertEqual(reader.post(f'/dokument/{doc}/beziehung/{new}/loeschen').status_code, 403)
        self.assertEqual(editor.post(f'/dokument/{doc}/beziehung/{new}/loeschen').status_code, 303)
        self.assertEqual([e['fields'] for e in self.events('document_changed')], ['relation', 'relation'])

    def test_compare_versions_within_the_rights(self):
        reader, admin, old, new = self.clients['oid-reader'], self.clients['oid-admin'], self.ids['old'], self.ids['new']
        shown = admin.get(f'/dokument/{new}/vergleich/{old}')
        self.assertEqual(shown.status_code, 200)
        self.assertIn('+Fassung zwei', shown.text)
        self.assertIn(f'/dokument/{new}/vergleich/{old}', admin.get(f'/dokument/{new}').text)
        for path in (f'/dokument/{old}/vergleich/{new}', f'/dokument/{new}/vergleich/{old}'):
            with self.subTest(path):
                self.assertEqual(reader.get(path).status_code, 404)
                self.assertNotIn(SECRET, reader.get(path).text)
        self.assertNotIn('vergleich', reader.get(f'/dokument/{old}').text)  # the newer version is invisible

    def test_summary_is_audited_and_needs_a_provider(self):
        reader, doc = self.clients['oid-reader'], self.ids['granted']
        answer = reader.post(f'/dokument/{doc}/zusammenfassung')
        self.assertEqual(answer.status_code, 200)
        self.assertIn('Wartungsplan', answer.text)
        self.assertEqual(self.events('ai_question')[-1], {'status': 'summary', 'target': f'document:{doc}'})
        self.assertEqual(reader.post(f'/dokument/{self.ids["secret"]}/zusammenfassung').status_code, 404)
        self.app = server.create_app(self.store, test_rights.Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET,
                                                                      BASE + '/auth/callback', http=self.idp.http), BASE)
        without = self.sign_in('oid-reader')
        self.assertNotIn('Zusammenfassen', without.get(f'/dokument/{doc}').text)
        self.assertEqual(without.post(f'/dokument/{doc}/zusammenfassung').status_code, 503)

    def test_new_upload_version_keeps_read_and_write_grants(self):
        editor = self.clients['oid-editor']
        first = editor.post('/hochladen', files=[('file', ('plan.txt', b'Plan eins Kessel.', 'text/plain'))])
        self.assertEqual(first.status_code, 202)
        heartbeat = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'alive'
        worker.import_once(self.store, threading.Event(), heartbeat)
        v1 = max(d['id'] for d in self.store.list())
        accounts.grant(self.store, self.reader, 'read', document_id=v1, end_sessions=False)
        editor.post('/hochladen', files=[('file', ('plan.txt', b'Plan zwei Kessel.', 'text/plain'))])
        worker.import_once(self.store, threading.Event(), heartbeat)
        v2 = max(d['id'] for d in self.store.list())
        self.assertNotEqual(v1, v2)
        self.assertEqual([i['status'] for i in ImportJobs(self.store).items(2)], ['versioned'])
        self.assertEqual(self.clients['oid-reader'].get(f'/dokument/{v2}').status_code, 200)
        self.assertIn('Plan zwei', self.clients['oid-reader'].get(f'/dokument/{v2}/vergleich/{v1}').text)
        self.assertEqual({g['access'] for g in accounts.grants(self.store, self.editor) if g['document_id'] == v2}, {'owner'})


if __name__ == '__main__':
    unittest.main()
