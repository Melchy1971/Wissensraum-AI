"""Parity roadmap phase 1: one transaction for change, owner grant and audit; 503 for AI failures; rate limits.

Endpoints run against the stock and accounts of ``test_rights``; the AI is a local fake. Every failure
is injected, nothing contacts a real service.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import server
import services
from core import AI, ProviderError, Store
from oidc import Provider
from test_rights import COMMON, EndpointTests
from test_server import AUTHORITY, BASE, CLIENT_ID, CLIENT_SECRET, TENANT


def check_transaction_contract(case, store, other):
    """``store.transaction()`` on both backends; ``other`` is a store on another database."""
    before = len(store.list(all_versions=True))
    with store.transaction() as outer:
        with store.connect() as inner:
            case.assertIs(inner, outer)  # nested calls join the open connection
        with store.as_viewer({'id': 1, 'role': 'admin', 'disabled': 0}).connect() as viewer_db:
            case.assertIs(viewer_db, outer)  # a viewer copy works on the same database
        with other.connect() as foreign:
            case.assertIsNot(foreign, outer)  # another database never joins
        store.add('Eins', 'Erster Text in einer Transaktion.')
        store.add('Zwei', 'Zweiter Text in einer Transaktion.')
    case.assertEqual(len(store.list(all_versions=True)), before + 2)
    with case.assertRaisesRegex(RuntimeError, 'Abbruch'):
        with store.transaction():
            store.add('Drei', 'Dritter Text, der zurückgerollt wird.')
            store.create_collection('Verworfen')
            raise RuntimeError('Abbruch')
    case.assertEqual(len(store.list(all_versions=True)), before + 2)
    case.assertNotIn('Verworfen', [c['name'] for c in store.collections()])
    doc, created = store.add('Drei', 'Dritter Text, der zurückgerollt wird.')
    case.assertTrue(created)  # nothing of the rolled-back attempt remained
    with store.connect() as db:  # outside a transaction every connect is its own again
        with store.connect() as second:
            case.assertIsNot(db, second)


class TransactionTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_transaction_contract(self, Store(Path(folder) / 'a.db'), Store(Path(folder) / 'b.db'))


class LimitsTests(unittest.TestCase):
    def test_window_per_account_and_kind(self):
        now = [0.0]
        limits = services.Limits({'question': (2, 60)}, clock=lambda: now[0])
        self.assertEqual([limits.allow('question', 1) for _ in range(3)], [True, True, False])
        self.assertTrue(limits.allow('question', 2))  # another account counts separately
        self.assertTrue(limits.allow('search', 1))  # another kind too
        now[0] = 61.0
        self.assertTrue(limits.allow('question', 1))  # the window moved on


def failing_ai(kind):
    def build(scoped):
        engine = AI(scoped, 'test-key')
        engine.generate = mock.Mock(side_effect=ProviderError('KI-Dienst nicht erreichbar. Später erneut versuchen.',
                                                              kind))
        return engine
    return build


class ServiceEndpointTests(EndpointTests):
    """Atomicity (S-07), AI failures (S-08) and rate limits (S-06) through API and pages."""
    # Reuses setUp and sign-in of EndpointTests; its own tests run there, not twice.
    locals().update({name: None for name in dir(EndpointTests) if name.startswith('test_')})

    def documents(self):
        return len(self.store.list(all_versions=True))

    def owner_grants(self):
        with self.store.connect() as db:
            return db.execute("SELECT count(*) AS n FROM grants WHERE access='owner'").fetchone()['n']

    def test_s07_document_audit_and_grant_are_one_transaction(self):
        editor, note = self.clients['oid-editor'], {'title': 'Notiz', 'content': 'Notiz zur Lüftung der Halle.'}
        before, grants, entries = self.documents(), self.owner_grants(), audit.verify(self.store)['entries']
        with mock.patch('audit.record', side_effect=RuntimeError('Audit ausgefallen')):
            with self.assertRaisesRegex(RuntimeError, 'Audit ausgefallen'):
                editor.post('/api/documents', json=note)
        self.assertEqual((self.documents(), self.owner_grants()), (before, grants))
        with mock.patch('accounts.grant', side_effect=RuntimeError('Freigabe ausgefallen')):
            with self.assertRaisesRegex(RuntimeError, 'Freigabe ausgefallen'):
                editor.post('/api/documents', json=note)
        self.assertEqual(self.documents(), before)  # no document without owner
        created = editor.post('/api/documents', json=note)
        self.assertEqual(created.status_code, 201)
        self.assertEqual((self.documents(), self.owner_grants()), (before + 1, grants + 1))
        self.assertEqual(audit.verify(self.store)['entries'], entries + 1)
        upload = editor.post('/hochladen', files={'file': ('neu.txt', 'Neuer Text für den Upload.'.encode(), 'text/plain')},
                             data={'category': 'Allgemein'}, headers={'HX-Request': 'true'})
        self.assertEqual(upload.status_code, 201)
        self.assertEqual(self.owner_grants(), grants + 2)
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_s07_metadata_and_grant_changes_roll_back_without_audit(self):
        admin, doc = self.clients['oid-admin'], self.ids['granted']
        revision = self.store.get(doc)['revision']
        with mock.patch('audit.record', side_effect=RuntimeError('Audit ausgefallen')):
            with self.assertRaises(RuntimeError):
                admin.patch(f'/api/documents/{doc}', json={'title': 'Ohne Audit', 'revision': revision})
            with self.assertRaises(RuntimeError):
                admin.post('/api/grants', json={'account_id': self.reader, 'access': 'read',
                                                'document_id': self.ids['secret']})
        row = self.store.get(doc)
        self.assertEqual((row['title'], row['revision']), ('Freigegeben', revision))
        self.assertNotIn(self.ids['secret'], [g['document_id'] for g in accounts.grants(self.store, self.reader)])
        self.assertEqual(admin.patch(f'/api/documents/{doc}', json={'title': 'Mit Audit', 'revision': revision})
                         .status_code, 200)

    def test_s08_ai_failure_answers_503_with_audit(self):
        provider = Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback', http=self.idp.http)
        self.app = server.create_app(self.store, provider, BASE, ai=failing_ai('unreachable'))
        reader = self.sign_in('oid-reader')
        api = reader.post('/api/questions', json={'question': f'Was gilt für {COMMON}?'})
        self.assertEqual((api.status_code, api.json()['detail']), (503, 'KI-Dienst nicht erreichbar. Später erneut versuchen.'))
        page = reader.post('/fragen', data={'question': f'Was gilt für {COMMON}?'}, headers={'HX-Request': 'true'})
        self.assertEqual(page.status_code, 503)
        self.assertIn('KI-Dienst nicht erreichbar', page.text)
        self.assertEqual((page.headers['HX-Retarget'], page.headers['HX-Reswap']), ('#meldung', 'innerHTML'))
        self.assertNotIn('<html', page.text)  # a fragment for the message area, not a whole page
        entries = [e for e in audit.entries(self.store) if e['event'] == 'ai_question'][:2]
        self.assertEqual([e['details'] for e in entries], ['{"kind":"unreachable","status":"unavailable"}'] * 2)
        self.assertNotIn(COMMON, str(audit.entries(self.store)))

    def test_s06_rate_limits_per_account(self):
        provider = Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback', http=self.idp.http)
        self.app = server.create_app(self.store, provider, BASE, ai=failing_ai('unreachable'),
                                     limits=services.Limits({'question': (2, 600), 'search': (3, 60), 'upload': (1, 600)}))
        reader, editor = self.sign_in('oid-reader'), self.sign_in('oid-editor')
        codes = [reader.post('/api/questions', json={'question': 'Pumpe'}).status_code for _ in range(3)]
        self.assertEqual(codes, [503, 503, 429])
        self.assertEqual(reader.post('/fragen', data={'question': 'Pumpe'}).status_code, 429)  # pages count too
        self.assertEqual(editor.post('/api/questions', json={'question': 'Pumpe'}).status_code, 503)  # own window
        searches = [reader.get('/api/search', params={'q': 'Pumpe'}).status_code for _ in range(3)]
        self.assertEqual(searches + [reader.get('/suche', params={'q': 'Pumpe'}).status_code], [200, 200, 200, 429])
        self.assertEqual(reader.get('/suche').status_code, 200)  # the empty search page costs nothing
        note = {'title': 'Notiz', 'content': 'Notiz zur Lüftung der Halle.'}
        self.assertEqual(editor.post('/api/documents', json=note).status_code, 201)
        self.assertEqual(editor.post('/api/documents', json={**note, 'content': 'Zweite Notiz.'}).status_code, 429)


if __name__ == '__main__':
    unittest.main()
