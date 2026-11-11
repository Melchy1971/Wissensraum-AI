"""Parity roadmap phase 7: the knowledge graph on the server.

Decision 11.11.2026: who may write the evidence document creates, confirms and deletes what rests on it and
requests AI suggestions; renaming, merging, splitting, reverting and the types stay with administrators.
Stock and accounts of ``test_rights``: Erik is mentioned in the granted and the secret document, Vera only
in the secret one, and their relation rests on the secret document (I1, I3, E1).
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
import graph_jobs
import server
import services
import worker
from core import AI, Store
import test_rights
from test_rights import AUTHORITY, BASE, CLIENT_ID, CLIENT_SECRET, SECRET, TENANT, fill, people


def check_graph_contract(case, store):
    """Rights on graph items and the suggestion queue alike on both backends."""
    ids = fill(store)
    admin, editor, reader = people(store, ids)
    accounts.grant(store, editor, 'write', document_id=ids['granted'], end_sessions=False)
    as_editor = accounts.account(store, editor)
    case.assertEqual(services.graph_evidence(store, 'entity', ids['erik']), sorted([ids['granted'], ids['secret']]))
    case.assertFalse(services.may_change_graph_item(store, as_editor, 'entity', ids['erik']))  # also rests on secret
    mention = next(m['id'] for m in store.entity(ids['erik'])['mentions'] if m['document_id'] == ids['granted'])
    case.assertTrue(services.may_change_graph_item(store, as_editor, 'mention', mention))
    case.assertTrue(services.may_change_graph_item(store, accounts.account(store, admin), 'entity', ids['vera']))
    graph_jobs.request(store, editor, ids['granted'], 100)
    with case.assertRaises(ValueError):
        graph_jobs.request(store, editor, ids['granted'], 101)
    case.assertEqual(graph_jobs.pending(store), [ids['granted']])
    with store.connect() as db:  # a stale state never wins between server and worker
        case.assertFalse(graph_jobs._swap(db, ids['granted'], {'status': 'done'}, {'status': 'failed'}))
    ran = graph_jobs.run_next(store, 102, 'm', suggest=lambda s, d, m: {'entitaeten': 2, 'relationen': 1})
    case.assertEqual((ran[0], ran[1]['status'], ran[1]['counts']['entitaeten']), (ids['granted'], 'done', 2))
    case.assertIsNone(graph_jobs.run_next(store, 103, 'm', suggest=lambda *a: {}))
    graph_jobs.request(store, editor, ids['granted'], 104)  # a finished run may be requested again
    accounts.grant(store, editor, 'read', document_id=ids['granted'], end_sessions=False)  # write access gone
    ran = graph_jobs.run_next(store, 105, 'm', suggest=lambda *a: case.fail('ran without the right'))
    case.assertEqual((ran[1]['status'], ran[1]['error']), ('failed', 'PermissionError'))


class ContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_graph_contract(self, Store(Path(folder) / 'wissen.db'))


class GraphPageTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        accounts.grant(self.store, self.editor, 'write', document_id=self.ids['granted'], end_sessions=False)
        self.person = next(t['id'] for t in self.store.entity_types() if t['name'] == 'Person')
        self.works = self.store.relation_types()[0]['id']

    def rebuild(self, **options):
        provider = test_rights.Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback',
                                        http=self.idp.http)
        ai = lambda scoped: AI(scoped, 'test-key', transport=test_rights.answer(self.payloads))  # noqa: E731
        self.app = server.create_app(self.store, provider, BASE, ai=ai, **options)
        self.clients = {oid: self.sign_in(oid) for oid in ('oid-admin', 'oid-editor', 'oid-reader')}

    def actions(self):
        return [json.loads(e['details']).get('action') for e in reversed(audit.entries(self.store, 1000))
                if e['event'] == 'graph_changed']

    def test_reading_shows_only_visible_evidence(self):
        reader = self.clients['oid-reader']
        page = reader.get('/graph').text
        self.assertIn('Erik Nord', page)
        for text in ('Vera', SECRET):
            self.assertNotIn(text, page)
        erik = reader.get(f'/graph/entitaet/{self.ids["erik"]}').text
        self.assertIn('Freigegeben', erik)
        self.assertNotIn('Vera', erik)
        self.assertNotIn('Geheim', erik)
        hidden, missing = reader.get(f'/graph/entitaet/{self.ids["vera"]}'), reader.get('/graph/entitaet/999999')
        self.assertEqual((hidden.status_code, hidden.text), (404, missing.text))
        self.assertEqual(reader.get('/graph', params={'fokus': self.ids['vera']}).status_code, 404)
        self.assertIn('<svg class="graph"', reader.get('/graph', params={'fokus': self.ids['erik']}).text)
        self.assertNotIn('Eintrag anlegen', reader.get(f'/dokument/{self.ids["granted"]}').text)

    def test_writers_of_the_evidence_change_what_rests_on_it(self):
        editor, reader, doc = self.clients['oid-editor'], self.clients['oid-reader'], self.ids['granted']
        self.assertIn('Eintrag anlegen', editor.get(f'/dokument/{doc}').text)
        new = {'type_id': self.person, 'name': 'Pumpenwart', 'quote': 'Wartungsplan Pumpe'}
        self.assertEqual(reader.post(f'/dokument/{doc}/graph/entitaet', data=new).status_code, 403)
        self.assertEqual(editor.post(f'/dokument/{self.ids["in_collection"]}/graph/entitaet',
                                     data={**new, 'quote': 'Südhalle'}).status_code, 403)
        self.assertEqual(editor.post(f'/dokument/{self.ids["secret"]}/graph/entitaet', data=new).status_code, 404)
        self.assertEqual(editor.post(f'/dokument/{doc}/graph/entitaet', data={**new, 'quote': 'steht nicht drin'}).status_code, 422)
        self.assertEqual(editor.post(f'/dokument/{doc}/graph/entitaet', data=new).status_code, 303)
        created = next(e['id'] for e in self.store.entities() if e['name'] == 'Pumpenwart')
        relation = {'source_id': created, 'target_id': self.ids['erik'], 'type_id': self.works, 'quote': 'Ansprechpartner Erik Nord'}
        self.assertEqual(editor.post(f'/dokument/{doc}/graph/relation', data={**relation, 'target_id': self.ids['vera']}).status_code, 404)
        made = editor.post(f'/dokument/{doc}/graph/relation', data=relation)
        self.assertEqual(made.status_code, 303, made.text[-600:])
        self.assertIn('Pumpenwart', editor.get(f'/dokument/{doc}').text)
        # Erik also rests on the secret document: only an administrator deletes him.
        self.assertEqual(editor.post(f'/graph/entitaet/{self.ids["erik"]}/loeschen').status_code, 403)
        secret_mention = next(m['id'] for m in self.store.entity(self.ids['erik'])['mentions'] if m['document_id'] == self.ids['secret'])
        self.assertEqual(editor.post(f'/graph/erwaehnung/{secret_mention}/loeschen').status_code, 404)
        own = next(m['id'] for m in self.store.entity(self.ids['erik'])['mentions'] if m['document_id'] == doc)
        self.assertEqual(reader.post(f'/graph/erwaehnung/{own}/loeschen').status_code, 403)
        gone = editor.post(f'/graph/erwaehnung/{own}/loeschen')
        self.assertEqual(gone.status_code, 303, gone.text[gone.text.find('meldung'):][:300])
        revision = self.store.entity(created)['revision']
        self.assertEqual(editor.post(f'/graph/entitaet/{created}/loeschen', data={'revision': revision}).status_code, 303)
        self.assertEqual(self.actions(), ['entity_created', 'relation_created', 'deleted', 'deleted'])

    def test_cross_document_changes_only_by_administrators(self):
        editor, admin, erik = self.clients['oid-editor'], self.clients['oid-admin'], self.ids['erik']
        revision = self.store.entity(erik)['revision']
        posts = [(f'/graph/entitaet/{erik}/umbenennen', {'name': 'Erik N.', 'revision': revision}),
                 (f'/graph/entitaet/{erik}/zusammenfuehren', {'other_id': self.ids['vera']}),
                 (f'/graph/entitaet/{erik}/trennen', {'name': 'X', 'mention': 1}),
                 ('/verwaltung/graph/art', {'kind': 'entity', 'name': 'Anlage'}),
                 ('/verwaltung/graph/zuruecknehmen', {'change_id': 1}), ('/verwaltung/graph/kontext', {'enabled': '1'})]
        for path, data in posts:
            with self.subTest(path):
                self.assertEqual(editor.post(path, data=data).status_code, 403)
        self.assertEqual(editor.get('/verwaltung/graph').status_code, 403)
        self.assertNotIn('Umbenennen', editor.get(f'/graph/entitaet/{erik}').text)
        self.assertEqual(admin.post(posts[0][0], data=posts[0][1]).status_code, 303)
        self.assertEqual(self.store.entity(erik)['name'], 'Erik N.')
        self.assertEqual(admin.post(posts[0][0], data=posts[0][1]).status_code, 409)  # stale revision
        self.assertEqual(admin.post(posts[3][0], data=posts[3][1]).status_code, 303)
        self.assertIn('Anlage', [t['name'] for t in self.store.entity_types()])
        rename = next(c['id'] for c in self.store.graph_changes() if c['action'] == 'rename')
        self.assertEqual(admin.post('/verwaltung/graph/zuruecknehmen', data={'change_id': rename}).status_code, 303)
        self.assertEqual(self.store.entity(erik)['name'], 'Erik Nord')
        self.assertEqual(admin.post('/verwaltung/graph/kontext', data={'enabled': '1'}).status_code, 303)
        self.assertTrue(self.store.graph_context_enabled())
        self.assertIn(f'<option value="{self.ids["vera"]}">Vera Geheim</option>', admin.get(f'/graph/entitaet/{erik}').text)
        self.assertEqual(admin.post(f'/graph/entitaet/{erik}/zusammenfuehren', data={'other_id': self.ids['vera']}).status_code, 303)
        self.assertNotIn(self.ids['vera'], [e['id'] for e in self.store.entities()])
        self.assertEqual(self.actions(), ['renamed', 'type_created', 'reverted', 'merged'])
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_ai_suggestions_through_the_worker(self):
        editor, doc = self.clients['oid-editor'], self.ids['granted']
        self.assertEqual(editor.post(f'/dokument/{doc}/graph/vorschlaege').status_code, 503)  # no Ollama model
        self.assertNotIn('KI-Vorschläge anfordern', editor.get(f'/dokument/{doc}').text)
        self.rebuild(graph_model='qwen3:4b')
        editor = self.clients['oid-editor']
        self.assertIn('KI-Vorschläge anfordern', editor.get(f'/dokument/{doc}').text)
        self.assertEqual(self.clients['oid-reader'].post(f'/dokument/{doc}/graph/vorschlaege').status_code, 403)
        self.assertEqual(editor.post(f'/dokument/{doc}/graph/vorschlaege').status_code, 303)
        self.assertEqual(editor.post(f'/dokument/{doc}/graph/vorschlaege').status_code, 409)
        self.assertIn('warten auf den Worker', editor.get(f'/dokument/{doc}').text)
        seen = []

        def suggest(store, document_id, model):
            seen.append((document_id, model))
            store.propose_entity(self.person, 'Pumpe', document_id, 'Pumpe', None)
            return {'abschnitte': 1, 'entitaeten': 1, 'erwaehnungen': 1, 'relationen': 0, 'verworfen': 0, 'fehler': 0}
        worker.graph_once(self.store, 'qwen3:4b', suggest=suggest)
        self.assertEqual(seen, [(doc, 'qwen3:4b')])
        page = editor.get(f'/dokument/{doc}').text
        self.assertIn('1 Entitäten', page)
        self.assertIn('(Vorschlag)', page)
        proposed = next(e['id'] for e in self.store.entities() if e['name'] == 'Pumpe')
        self.assertEqual(editor.post(f'/graph/entitaet/{proposed}/bestaetigen').status_code, 303)
        self.assertEqual(self.store.entity(proposed)['status'], 'confirmed')
        self.assertEqual(self.actions(), ['suggest_requested', 'suggested', 'confirmed'])
        self.assertIsNone(worker.graph_once(self.store, None))


if __name__ == '__main__':
    unittest.main()
