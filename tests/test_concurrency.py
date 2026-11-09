"""Concurrent editing (Release 3.0, step 10.6): negative test T2 with two editors.

Every change names the revision it read. A stale revision changes nothing and raises ``Conflict``
(the server answers 409 with the current state), also when two changes really run at the same time.
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
from core import Conflict, Store

TENANT = '00000000-0000-0000-0000-00000000aaaa'


def _race(case, change):
    """Run ``change`` twice at the same moment with the same revision; returns the outcomes."""
    start, outcomes = threading.Barrier(2), []

    def run():
        start.wait()
        try:
            change()
            outcomes.append('ok')
        except Conflict:
            outcomes.append('conflict')
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    case.assertEqual(sorted(outcomes), ['conflict', 'ok'])


def check_revision_contract(case, store):
    """Revisions of documents, collections, entities, relations and grants; same on both backends."""
    doc, _ = store.add('Plan', 'Wartungsplan der Pumpe mit Erik Nord und Vera Süd.')
    case.assertEqual(store.get(doc)['revision'], 1)
    store.update_metadata(doc, title='Plan A', revision=1)
    with case.assertRaises(Conflict):
        store.update_metadata(doc, title='Plan B', revision=1)  # the second editor read revision 1 as well
    case.assertEqual((store.get(doc)['title'], store.get(doc)['revision']), ('Plan A', 2))
    store.update_metadata(doc, category='Technik')  # desktop: no revision given, still counted
    case.assertEqual(store.get(doc)['revision'], 3)
    store.update_metadata(doc, title='Plan A')  # nothing changed: same revision
    case.assertEqual(store.get(doc)['revision'], 3)
    _race(case, lambda: store.update_metadata(doc, title='Gleichzeitig', revision=3))
    case.assertEqual(store.get(doc)['revision'], 4)

    collection = store.create_collection('Projekt')
    store.rename_collection(collection, 'Projekt Nord', revision=1)
    with case.assertRaises(Conflict):
        store.add_to_collection(collection, [doc], revision=1)
    case.assertEqual(store.collections()[0]['documents'], 0)  # nothing added by the stale request
    store.add_to_collection(collection, [doc], revision=2)
    with case.assertRaises(Conflict):
        store.delete_collection(collection, revision=2)
    case.assertEqual([(c['name'], c['revision'], c['documents']) for c in store.collections()], [('Projekt Nord', 3, 1)])
    _race(case, lambda: store.remove_from_collection(collection, [doc], revision=3))

    person = next(t['id'] for t in store.entity_types() if t['name'] == 'Person')
    erik = store.create_entity(person, 'Erik Nord', doc, 'Erik Nord')
    vera = store.create_entity(person, 'Vera Süd', doc, 'Vera Süd')
    store.rename_entity(erik, 'Erik N. Nord', revision=1)
    with case.assertRaises(Conflict):
        store.rename_entity(erik, 'Erik Nordmann', revision=1)
    case.assertEqual(store.entity(erik)['name'], 'Erik N. Nord')
    with case.assertRaises(Conflict):
        store.delete_entity(erik, revision=1)
    relation = store.relate_entities(erik, vera, store.relation_types()[0]['id'], doc, 'Erik Nord')
    with case.assertRaises(Conflict):
        store.delete_graph_relation(relation, revision=7)
    case.assertEqual([r['revision'] for r in store.entity(erik)['relations']], [1])
    store.delete_graph_relation(relation, revision=1)
    store.merge_entities(erik, vera)
    with case.assertRaises(Conflict):
        store.rename_entity(erik, 'Erik', revision=2)  # the merge changed it after it was read
    case.assertEqual(store.entity(erik)['revision'], 3)

    account = accounts.create_account(store, TENANT, 'oid-x', 'editor')
    accounts.grant(store, account, 'read', document_id=doc, revision=0)
    with case.assertRaises(Conflict):
        accounts.grant(store, account, 'write', document_id=doc, revision=0)  # someone granted first
    accounts.grant(store, account, 'write', document_id=doc, revision=1)
    with case.assertRaises(Conflict):
        accounts.grant(store, account, 'owner', document_id=doc, revision=1)
    with case.assertRaises(Conflict):
        accounts.revoke(store, account, document_id=doc, revision=1)
    case.assertEqual([(g['access'], g['revision']) for g in accounts.grants(store, account)], [('write', 2)])
    accounts.revoke(store, account, document_id=doc, revision=2)
    with case.assertRaises(Conflict):
        accounts.grant(store, account, 'read', document_id=doc, revision=2)  # it was revoked meanwhile
    case.assertEqual(accounts.grants(store, account), [])


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_revision_contract(self, self.store)


class TwoEditorsTests(unittest.TestCase):
    """T2 over the server: both read the same state, the second write answers 409 and changes nothing."""

    def setUp(self):
        from test_rights import EndpointTests
        self.case = EndpointTests('test_i1_reads_show_only_visible_documents')
        self.case.setUp()
        self.store, self.ids = self.case.store, self.case.ids
        self.second = accounts.create_account(self.store, TENANT, 'oid-editor-2', 'editor', 'Zweite')
        for account in (self.case.editor, self.second):
            accounts.grant(self.store, account, 'write', document_id=self.ids['granted'])
            accounts.grant(self.store, account, 'write', collection_id=self.ids['shared_collection'])
        self.first = self.case.sign_in('oid-editor')
        self.other = self.case.sign_in('oid-editor-2')
        self.admin = self.case.clients['oid-admin']

    def tearDown(self):
        self.case.tearDown()

    def test_t2_document_metadata(self):
        path = f'/api/documents/{self.ids["granted"]}'
        read_first, read_other = self.first.get(path).json(), self.other.get(path).json()
        self.assertEqual(read_first['revision'], read_other['revision'])
        saved = self.first.patch(path, json={'title': 'Fassung Eins', 'revision': read_first['revision']})
        self.assertEqual(saved.status_code, 200)
        stale = self.other.patch(path, json={'title': 'Fassung Zwei', 'revision': read_other['revision']})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['current']['title'], 'Fassung Eins')
        self.assertEqual(self.store.get(self.ids['granted'])['title'], 'Fassung Eins')  # not overwritten
        again = self.other.patch(path, json={'title': 'Fassung Zwei', 'revision': stale.json()['current']['revision']})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.other.patch(path, json={'title': 'x'}).status_code, 422)  # a revision is required

    def test_t2_collection(self):
        collection = self.ids['shared_collection']
        revision = next(c for c in self.first.get('/api/collections').json() if c['id'] == collection)['revision']
        path = f'/api/collections/{collection}'
        self.assertEqual(self.first.patch(path, json={'name': 'Projekt Süd neu', 'revision': revision}).status_code, 200)
        stale = self.other.patch(path, json={'add_documents': [self.ids['granted']], 'revision': revision})
        self.assertEqual((stale.status_code, stale.json()['current']['name']), (409, 'Projekt Süd neu'))
        self.assertEqual(stale.json()['current']['documents'], 1)  # the stale request added nothing
        both = self.first.patch(path, json={'name': 'X', 'add_documents': [1], 'revision': revision + 1})
        self.assertEqual(both.status_code, 422)  # one change per request, applied whole
        reader = self.case.clients['oid-reader']
        self.assertEqual(reader.patch(path, json={'name': 'Y', 'revision': revision + 1}).status_code, 403)
        hidden = f'/api/collections/{self.ids["hidden_collection"]}'
        self.assertEqual(self.first.patch(hidden, json={'name': 'Y', 'revision': 1}).status_code, 404)
        secret = self.first.patch(path, json={'add_documents': [self.ids['secret']], 'revision': revision + 1})
        self.assertEqual(secret.status_code, 404)  # cannot pull an invisible document into its collection

    def test_t2_entity_and_grant(self):
        path = f'/api/entities/{self.ids["erik"]}'
        revision = self.admin.get(path).json()['revision']
        self.assertEqual(self.admin.patch(path, json={'name': 'Erik A. Nord', 'revision': revision}).status_code, 200)
        stale = self.admin.patch(path, json={'name': 'Erik B. Nord', 'revision': revision})
        self.assertEqual((stale.status_code, stale.json()['current']['name']), (409, 'Erik A. Nord'))
        self.assertEqual(self.first.patch(path, json={'name': 'Z', 'revision': revision + 1}).status_code, 403)
        grant = {'account_id': self.case.reader, 'access': 'write', 'document_id': self.ids['granted'], 'revision': 1}
        self.assertEqual(self.admin.post('/api/grants', json=grant).status_code, 201)
        stale = self.admin.post('/api/grants', json={**grant, 'access': 'owner'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual([(g['access'], g['revision']) for g in stale.json()['current']], [('write', 2)])


if __name__ == '__main__':
    unittest.main()
