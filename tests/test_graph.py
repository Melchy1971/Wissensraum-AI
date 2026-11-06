"""Knowledge graph data model (Release 2.3, step 8B.1)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store

TEXT = 'Anna Berg leitet den Einkauf bei der Nordwerk GmbH. Der Einkauf nutzt das System SAP.'


def check_graph_contract(case, store):
    """Same behaviour on SQLite and PostgreSQL; ``store`` must be freshly migrated."""
    case.assertEqual([row['name'] for row in store.entity_types()],
                     ['Entscheidung', 'Organisation', 'Person', 'Prozess', 'System'])
    case.assertEqual(len(store.relation_types()), 5)
    types = {row['name']: row['id'] for row in store.entity_types()}
    rtypes = {row['name']: row['id'] for row in store.relation_types()}
    doc, _ = store.add('Organigramm', TEXT)
    before = store.get(doc)

    anna = store.create_entity(types['Person'], ' Anna   Berg ', doc, 'Anna Berg')
    firm = store.create_entity(types['Organisation'], 'Nordwerk GmbH', doc, 'Nordwerk GmbH')
    sap = store.create_entity(types['System'], 'SAP', doc, 'SAP', start=TEXT.rindex('SAP'))
    for bad in [(types['Person'], 'anna berg', doc, 'Anna Berg'),       # duplicate name per type
                (types['Person'], '', doc, 'Anna'), (10**9, 'X', doc, 'Anna'),
                (types['Person'], 'Bert', doc, 'steht nicht im Text'),  # evidence must be verbatim
                (types['Person'], 'Bert', doc, 'Anna', 5), (types['Person'], 'Bert', 10**9, 'Anna'),
                (types['Person'], 'Bert', doc, '   ')]:
        with case.assertRaises(ValueError):
            store.create_entity(*bad)
    works = store.relate_entities(anna, firm, rtypes['arbeitet für'], doc,
                                  'Anna Berg leitet den Einkauf bei der Nordwerk GmbH')
    for bad in [(anna, anna, rtypes['nutzt'], doc, 'Anna'), (anna, sap, rtypes['nutzt'], doc, 'erfunden'),
                (anna, firm, rtypes['arbeitet für'], doc, 'Anna Berg leitet den Einkauf bei der Nordwerk GmbH'),
                (anna, 10**9, rtypes['nutzt'], doc, 'Anna'), (anna, sap, 10**9, doc, 'Anna')]:
        with case.assertRaises(ValueError):
            store.relate_entities(*bad)

    detail = store.entity(anna)
    case.assertEqual((detail['name'], detail['type'], detail['status']), ('Anna Berg', 'Person', 'confirmed'))
    case.assertEqual([(m['start_offset'], m['end_offset'], m['quote']) for m in detail['mentions']],
                     [(0, 9, 'Anna Berg')])
    relation = detail['relations'][0]
    case.assertEqual((relation['source'], relation['type'], relation['target'], relation['document_id']),
                     ('Anna Berg', 'arbeitet für', 'Nordwerk GmbH', doc))
    case.assertEqual(TEXT[relation['start_offset']:relation['end_offset']], relation['quote'])
    case.assertEqual([(m['entity'], m['type']) for m in store.document_graph(doc)['mentions']],
                     [('Anna Berg', 'Person'), ('Nordwerk GmbH', 'Organisation'), ('SAP', 'System')])
    case.assertEqual([row['name'] for row in store.entities(query='ber')], ['Anna Berg'])
    case.assertEqual([row['name'] for row in store.entities(type_id=types['System'])], ['SAP'])
    with case.assertRaises(ValueError):
        store.add_mention(sap, doc, 'SAP')  # the same span twice
    store.add_mention(sap, doc, 'System SAP')
    case.assertEqual(len(store.entity(sap)['mentions']), 2)

    # Every change is logged and can be reverted once.
    store.rename_entity(anna, 'Anna Berg-Meier')
    store.delete_graph_relation(works)
    case.assertEqual(store.entity(anna)['relations'], [])
    store.delete_entity(firm)
    with case.assertRaises(ValueError):
        store.entity(firm)
    changes = store.graph_changes()
    case.assertEqual([(c['action'], c['object_type']) for c in changes[:3]],
                     [('delete', 'entity'), ('delete', 'relation'), ('rename', 'entity')])
    store.revert_graph_change(changes[0]['id'])  # entity back
    store.revert_graph_change(changes[1]['id'])  # relation back
    store.revert_graph_change(changes[2]['id'])  # old name back
    case.assertEqual(store.entity(anna)['name'], 'Anna Berg')
    case.assertEqual([r['id'] for r in store.entity(anna)['relations']], [works])
    with case.assertRaises(ValueError):
        store.revert_graph_change(changes[0]['id'])  # only once
    reverts = [c for c in store.graph_changes() if c['action'] == 'revert']
    case.assertEqual(len(reverts), 3)
    with case.assertRaises(ValueError):
        store.revert_graph_change(reverts[0]['id'])
    created = next(c for c in store.graph_changes(500) if c['action'] == 'create' and c['object_type'] == 'relation')
    store.revert_graph_change(created['id'])  # undoing a creation removes the relation
    case.assertEqual(store.entity(anna)['relations'], [])

    # Configurable types: add, rename, delete only when unused, revert.
    role = store.add_entity_type('Rolle')
    store.rename_entity_type(role, 'Funktion')
    with case.assertRaises(ValueError):
        store.add_entity_type('funktion')
    with case.assertRaises(ValueError):
        store.delete_entity_type(types['Person'])  # in use
    store.delete_entity_type(role)
    deleted = store.graph_changes()[0]
    store.revert_graph_change(deleted['id'])
    case.assertIn('Funktion', [row['name'] for row in store.entity_types()])
    uses = store.add_relation_type('berichtet an')
    store.rename_relation_type(uses, 'berichtet an (fachlich)')
    case.assertIn('berichtet an (fachlich)', [row['name'] for row in store.relation_types()])
    store.delete_relation_type(uses)

    # The document never changes; deleting it removes evidence and every relation it supports.
    case.assertEqual(store.get(doc), before)
    store.relate_entities(anna, sap, rtypes['nutzt'], doc, 'Der Einkauf nutzt das System SAP')
    store.delete(doc)
    case.assertEqual(store.entity(anna)['relations'], [])
    case.assertEqual(store.entity(anna)['mentions'], [])


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_graph_contract(self, self.store)

    def test_cli(self):
        import cli
        doc, _ = self.store.add('Organigramm', TEXT)

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', self.folder), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--db', str(self.folder / 'wissen.db'), *argv]), 0)
            return json.loads(out.getvalue())
        types = {row['name']: row['id'] for row in run('entity-types')}
        anna = run('entity-create', str(types['Person']), 'Anna Berg', str(doc), 'Anna Berg')['id']
        firm = run('entity-create', str(types['Organisation']), 'Nordwerk GmbH', str(doc), 'Nordwerk GmbH')['id']
        rtypes = {row['name']: row['id'] for row in run('relation-types')}
        relation = run('entity-relate', str(anna), str(firm), str(rtypes['arbeitet für']), str(doc), 'Anna Berg')['id']
        self.assertEqual(run('entity', str(anna))['relations'][0]['id'], relation)
        self.assertEqual([row['name'] for row in run('entities', '--query', 'nord')], ['Nordwerk GmbH'])
        self.assertEqual(run('entity-type-add', 'Rolle')['id'] > 0, True)
        change = run('graph-changes')[0]
        self.assertEqual((change['action'], change['object_type']), ('create', 'entity_type'))
        run('graph-revert', str(change['id']))
        self.assertNotIn('Rolle', [row['name'] for row in run('entity-types')])


if __name__ == '__main__':
    unittest.main()
