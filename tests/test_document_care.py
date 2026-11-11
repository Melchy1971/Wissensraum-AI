"""Parity roadmap phase 4a: document care on the server.

Metadata with tags and history, deleting (owner or administrator, decision 11.11.2026), due reviews,
tag filters, notes, AI history with sources and the switch for retired documents. Stock and accounts
of ``test_rights``; every check also asks what an account without access sees (I1, I3, E1).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
from core import Store
import test_rights
from test_rights import COMMON, SECRET, fill, people


def check_care_contract(case, store):
    """Due reviews and tag counts follow the rights on both backends."""
    ids = fill(store)
    admin, editor, reader = people(store, ids)
    store.update_metadata(ids['granted'], review_date='2020-01-01', add_tags='Pumpe, Wartung')
    store.update_metadata(ids['secret'], review_date='2020-01-02', add_tags=f'Pumpe, {SECRET}')
    as_reader = store.as_viewer(accounts.account(store, reader))
    case.assertEqual([r['id'] for r in as_reader.due_reviews()], [ids['granted']])
    case.assertEqual([r['id'] for r in store.due_reviews()], [ids['granted'], ids['secret']])  # desktop: all
    case.assertEqual(as_reader.visible_tags(), [('Pumpe', 1), ('Wartung', 1)])
    case.assertEqual(dict(store.as_viewer(accounts.account(store, admin)).visible_tags()),
                     {'Pumpe': 2, SECRET: 1, 'Wartung': 1})
    case.assertEqual(as_reader.document_tags(ids['granted']), ['Pumpe', 'Wartung'])


class ContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_care_contract(self, Store(Path(folder) / 'wissen.db'))


class DocumentCareTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        accounts.grant(self.store, self.editor, 'write', document_id=self.ids['granted'], end_sessions=False)

    def revision(self, doc_id):
        return str(self.store.get(doc_id)['revision'])

    def test_all_metadata_with_tags_and_history(self):
        editor, doc = self.clients['oid-editor'], self.ids['granted']
        path = f'/dokument/{doc}/metadaten'
        saved = editor.post(path, data={'title': 'Freigegeben', 'category': 'Technik', 'status': 'reviewed',
                                        'owner': 'Erik Nord', 'review_date': '2030-05-01', 'tags': 'Pumpe, Wartung',
                                        'revision': self.revision(doc)})
        self.assertEqual(saved.status_code, 200)
        self.assertIn('Gespeichert.', saved.text)
        row = self.store.get(doc)
        self.assertEqual((row['category'], row['status'], row['owner'], row['review_date']),
                         ('Technik', 'reviewed', 'Erik Nord', '2030-05-01'))
        self.assertEqual(self.store.document_tags(doc), ['Pumpe', 'Wartung'])
        editor.post(path, data={'tags': 'wartung, Prüfung', 'revision': self.revision(doc)})
        self.assertEqual(self.store.document_tags(doc), ['Prüfung', 'Wartung'])  # case kept, Pumpe removed
        page = editor.get(f'/dokument/{doc}').text
        for text in ('Verlauf der Metadaten', 'geprüft', 'Erik Nord', 'Prüfung'):
            self.assertIn(text, page)
        details = [json.loads(e['details']) for e in audit.entries(self.store) if e['event'] == 'document_changed']
        self.assertEqual(details[0]['fields'], 'tag')
        self.assertNotIn('Prüfung', str(audit.entries(self.store)))  # values never go into the audit
        self.assertEqual(editor.post(path, data={'status': 'falsch', 'revision': self.revision(doc)}).status_code, 422)
        self.assertEqual(self.clients['oid-reader'].post(path, data={'status': 'new', 'revision': '1'}).status_code, 403)
        api = editor.patch(f'/api/documents/{doc}', json={'tags': ['Kessel'], 'review_date': '',
                                                          'revision': int(self.revision(doc))})
        self.assertEqual(api.status_code, 200)
        self.assertEqual((self.store.document_tags(doc), self.store.get(doc)['review_date']), (['Kessel'], None))

    def test_history_hides_what_the_reader_must_not_know(self):
        doc = self.ids['granted']  # in the hidden collection and related to the secret document
        self.store.set_favorite(doc)
        reader, admin = self.clients['oid-reader'].get(f'/dokument/{doc}').text, self.clients['oid-admin'].get(f'/dokument/{doc}').text
        for text in ('Projekt Geheim', f'related:{self.ids["secret"]}', 'Favorit'):
            self.assertNotIn(text, reader)
        self.assertIn('Projekt Geheim', admin)
        self.assertNotIn('Favorit', admin)  # another account's action

    def test_delete_only_by_owner_or_administrator(self):
        editor, reader, admin = self.clients['oid-editor'], self.clients['oid-reader'], self.clients['oid-admin']
        granted, secret = self.ids['granted'], self.ids['secret']
        refused = editor.post(f'/dokument/{granted}/loeschen', data={'revision': self.revision(granted)})
        self.assertEqual(refused.status_code, 403)  # write access is not enough
        self.assertNotIn('Dokument löschen', editor.get(f'/dokument/{granted}').text)
        self.assertEqual(reader.post(f'/dokument/{secret}/loeschen', data={'revision': '1'}).status_code, 404)
        self.assertEqual(reader.post('/dokument/999999/loeschen', data={'revision': '1'}).status_code, 404)
        created = editor.post('/notiz', data={'title': 'Eigene Notiz', 'content': 'Eigener Text zum Löschen.',
                                              'category': 'Allgemein'})
        own = int(created.headers['HX-Redirect'].rsplit('/', 1)[1])
        self.assertIn('Dokument löschen', editor.get(f'/dokument/{own}').text)
        stale = editor.post(f'/dokument/{own}/loeschen', data={'revision': str(int(self.revision(own)) + 5)})
        self.assertEqual(stale.status_code, 409)
        done = editor.post(f'/dokument/{own}/loeschen', data={'revision': self.revision(own)})
        self.assertEqual((done.status_code, done.headers['HX-Redirect']), (200, '/'))
        self.assertEqual(editor.get(f'/dokument/{own}').status_code, 404)
        self.assertNotIn(own, [g['document_id'] for g in accounts.grants(self.store, self.editor)])
        self.assertEqual(admin.delete(f'/api/documents/{secret}', params={'revision': self.revision(secret)}).status_code, 200)
        self.assertEqual([e['event'] for e in audit.entries(self.store, 2)], ['document_deleted', 'document_deleted'])
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_due_reviews_tags_and_filters_follow_the_rights(self):
        self.store.update_metadata(self.ids['granted'], review_date='2020-01-01', add_tags='Pumpe')
        self.store.update_metadata(self.ids['secret'], review_date='2020-01-02', add_tags=f'Pumpe, {SECRET}')
        reader = self.clients['oid-reader']
        reviews = reader.get('/wiedervorlagen').text
        self.assertIn('Freigegeben', reviews)
        self.assertNotIn('Geheim', reviews)
        listing = reader.get('/').text
        self.assertIn('Pumpe (1)', listing)
        self.assertNotIn(SECRET, listing)
        self.assertIn('1 Dokumente mit Tag Pumpe', reader.get('/', params={'tag': 'Pumpe'}).text)
        self.assertEqual([d['id'] for d in reader.get('/api/documents', params={'tag': SECRET}).json()['documents']], [])
        hits = reader.get('/api/search', params={'q': COMMON, 'tag': 'Pumpe'}).json()['results']
        self.assertEqual([h['document_id'] for h in hits], [self.ids['granted']])
        self.assertIn('gefunden über Volltext', reader.get('/suche', params={'q': COMMON}).text)

    def test_notes_history_sources_and_retired_switch(self):
        editor, reader, admin = self.clients['oid-editor'], self.clients['oid-reader'], self.clients['oid-admin']
        self.assertEqual(reader.get('/notiz').status_code, 403)
        note = {'title': 'Notiz', 'content': 'Notiz zur Lüftung der Halle.', 'category': 'Allgemein'}
        self.assertEqual(editor.post('/notiz', data=note).status_code, 201)
        self.assertEqual(editor.post('/notiz', data=note).status_code, 409)
        self.assertEqual(editor.post('/notiz', data={**note, 'content': ' '}).status_code, 422)
        reader.post('/fragen', data={'question': f'Was gilt für {COMMON}?'})
        history = reader.get('/fragen').text
        self.assertIn(f'href="/dokument/{self.ids["granted"]}"', history)  # sources of the own history
        self.assertEqual(reader.post('/verwaltung/veraltete-dokumente', data={'enabled': '0'}).status_code, 403)
        self.assertTrue(self.store.exclude_retired())
        self.assertEqual(admin.post('/verwaltung/veraltete-dokumente', data={'enabled': '0'}).status_code, 303)
        self.assertFalse(self.store.exclude_retired())
        self.assertIn('berücksichtigt', admin.get('/verwaltung').text)
        self.assertEqual(audit.entries(self.store, 1)[0]['target'], 'settings:exclude_retired')


if __name__ == '__main__':
    unittest.main()
